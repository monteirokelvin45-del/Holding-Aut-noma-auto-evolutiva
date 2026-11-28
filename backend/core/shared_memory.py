"""Memória partilhada da holding: cemitério de negócios e busca semântica leve.

Requisito de negócio: *antes de produzir qualquer coisa, o Validador verifica
que a ideia não consta no ``post_mortem_memory.json``*.

Implementação sem dependências pesadas:
  * Embeddings por **hashing de n-gramas** (``hash-tf-v1``) com normalização
    L2 e IDF calculado sobre o corpus. É determinístico, versionável em JSON e
    suficiente para detectar repetições de nicho (não requer torch).
  * Busca por similaridade de cosseno com limiar configurável.
  * Cada entrada guarda causa de morte, lições, métricas e a assinatura
    lexical da ideia (para comparações futuras mesmo sem re-embedding).
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .clock import utcnow_iso
from .errors import NotFound, ValidationBlocked
from .logger import get_logger
from .vault_store import POST_MORTEM, vault

log = get_logger("core.memory")

EMBEDDING_DIM = 128
EMBEDDING_MODEL = "hash-tf-v2-stem"
DEFAULT_SIMILARITY_BLOCK = 0.40

# Palavras vazias (PT + EN) que não distinguem nichos.
STOPWORDS = frozenset(
    """a o e de da do das dos em no na nos nas para por com sem que um uma uns umas
    os as ao aos à às se mais como mas já ja nao não sim muito muita muitos muitas
    ser estar ter haver foi era são sao esta está este essa esse isso isto aquele
    sobre entre depois antes durante quando onde qual quais cujo cuja the and or of
    to in on for with is are was were be been it this that these those as at by
    from we you they he she his her its our their not no yes very much many""".split()
)

_SUFFIXES = (
    "amentos", "imentos", "amento", "imento", "ações", "acoes", "ção", "cao",
    "mentos", "mento", "ções", "coes", "ões", "oes", "ais", "eis", "ois",
    "ando", "endo", "indo", "aram", "eram", "iram", "ava", "ivo", "iva",
    "es", "as", "os", "s", "a", "o", "e",
)


def normalize_text(text: str) -> str:
    """Minúsculas, sem acentos, só letras/dígitos/espaços."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.lower()
    stripped = re.sub(r"[^a-z0-9\s\-]", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _stem(word: str) -> str:
    """Stemming leve (sufixos PT) sem dependências externas."""
    for suffix in _SUFFIXES:
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def words_of(text: str) -> List[str]:
    """Palavras de conteúdo (sem stopwords/números, com stemming leve)."""
    result: List[str] = []
    for raw in re.findall(r"[a-z0-9]+", normalize_text(text)):
        if len(raw) < 3 or raw in STOPWORDS or raw.isdigit():
            continue
        result.append(_stem(raw))
    return result


def _tokens(text: str, ngram_max: int = 2) -> List[str]:
    """Unigramas + bigramas de conteúdo (assinatura lexical)."""
    words = words_of(text)
    grams = list(words)
    for size in range(2, ngram_max + 1):
        grams.extend(
            "_".join(words[i : i + size]) for i in range(0, len(words) - size + 1)
        )
    return grams


def _hash_index(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % EMBEDDING_DIM


def embed(text: str) -> List[float]:
    """Embedding determinístico (hashing trick com pesos TF).

    Vector unitário L2 de 128 dimensões; tokens repetidos pesam
    logaritmicamente. Sem colisões negativas para manter a similaridade de
    cosseno interpretável em [0, 1].
    """
    vector = [0.0] * EMBEDDING_DIM
    counts = Counter(_tokens(text))
    if not counts:
        return vector
    for token, count in counts.items():
        vector[_hash_index(token)] += 1.0 + math.log(count)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0:
        vector = [round(value / norm, 6) for value in vector]
    return vector


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    va = list(a)
    vb = list(b)
    if not va or not vb or len(va) != len(vb):
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def lexical_scores(candidate: str, reference: str) -> Dict[str, float]:
    """Contenção, Jaccard e bigramas entre o candidato e a referência."""
    cand_words = set(words_of(candidate))
    ref_words = set(words_of(reference))
    if not cand_words or not ref_words:
        return {"containment": 0.0, "jaccard": 0.0, "bigram": 0.0}
    inter = cand_words & ref_words
    containment = len(inter) / len(cand_words)
    jaccard = len(inter) / len(cand_words | ref_words)
    cand_bi = {
        "_".join(pair)
        for pair in zip(
            [w for w in words_of(candidate)][:-1],
            [w for w in words_of(candidate)][1:],
        )
    }
    ref_bi = {
        "_".join(pair)
        for pair in zip(
            [w for w in words_of(reference)][:-1],
            [w for w in words_of(reference)][1:],
        )
    }
    bigram = len(cand_bi & ref_bi) / max(1, len(cand_bi)) if cand_bi else 0.0
    return {
        "containment": round(containment, 4),
        "jaccard": round(jaccard, 4),
        "bigram": round(bigram, 4),
    }


def similarity_score(candidate: str, reference: str) -> Dict[str, float]:
    """Pontuação híbrida final usada pelo cemitério de negócios.

    ``final = 0.5*containment + 0.2*jaccard + 0.3*bigram + 0.15*cosine``
    (limitada a 1.0). Calibrada para que uma reformulação do mesmo nicho
    ultrapasse 0.40 e um nicho diferente fique abaixo de 0.15.
    """
    lex = lexical_scores(candidate, reference)
    cos = cosine(embed(candidate), embed(reference))
    final = (
        0.50 * lex["containment"]
        + 0.20 * lex["jaccard"]
        + 0.30 * lex["bigram"]
        + 0.15 * cos
    )
    return {
        **lex,
        "cosine": round(cos, 4),
        "final": round(min(1.0, final), 4),
    }


@dataclass
class PostMortemEntry:
    """Registo imutável de um negócio/ideia morta ou vetada."""

    id: str
    title: str
    niche: str
    squad: str
    death_cause: str
    lessons: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)
    created_by: str = "accountant_auditor"
    embedding: List[float] = field(default_factory=list)
    signature: List[str] = field(default_factory=list)
    outcome: str = "killed"  # killed | rejected | abandoned

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["embedding"] = [round(v, 6) for v in self.embedding]
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PostMortemEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in raw.items() if k in known}
        return cls(**payload)  # type: ignore[arg-type]


class SharedMemory:
    """API de alto nível sobre ``post_mortem_memory.json``."""

    def __init__(self, similarity_block: float = DEFAULT_SIMILARITY_BLOCK) -> None:
        self._similarity_block = similarity_block

    # --------------------------------------------------------------- internos
    def _read(self) -> Dict[str, Any]:
        data = vault.read(POST_MORTEM)
        if not isinstance(data.get("entries"), list):
            data["entries"] = []
        return data

    def _entries(self) -> List[PostMortemEntry]:
        result: List[PostMortemEntry] = []
        for raw in self._read().get("entries", []):
            try:
                result.append(PostMortemEntry.from_dict(raw))
            except TypeError as exc:
                log.warning("Entrada post-mortem inválida ignorada: %s", exc)
        return result

    # ------------------------------------------------------------------ escrita
    def record_failure(
        self,
        *,
        title: str,
        niche: str,
        squad: str,
        death_cause: str,
        lessons: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        outcome: str = "killed",
        created_by: str = "accountant_auditor",
    ) -> PostMortemEntry:
        """Grava uma lição aprendida. Idempotente por (title, squad)."""
        title = (title or "sem-titulo").strip()[:200]
        niche = (niche or "geral").strip()[:160]
        squad = (squad or "desconhecido").strip()[:80]
        death_cause = (death_cause or "causa não documentada").strip()[:600]

        with vault.transaction(POST_MORTEM) as data:
            data.setdefault("entries", [])
            for raw in data["entries"]:
                if (
                    str(raw.get("title", "")).strip().lower() == title.lower()
                    and str(raw.get("squad", "")).strip().lower() == squad.lower()
                ):
                    log.info("Lição já registada para '%s' (%s) — ignorado.", title, squad)
                    return PostMortemEntry.from_dict(raw)

            text_for_embedding = f"{title} {niche} {death_cause} {' '.join(tags or [])}"
            entry_id = "pm_" + hashlib.sha1(
                f"{title}|{squad}|{utcnow_iso()}".encode("utf-8")
            ).hexdigest()[:12]
            entry = PostMortemEntry(
                id=entry_id,
                title=title,
                niche=niche,
                squad=squad,
                death_cause=death_cause,
                lessons=[str(item)[:400] for item in (lessons or [])][:20],
                tags=[str(tag)[:40] for tag in (tags or [])][:20],
                metrics={str(k): v for k, v in (metrics or {}).items()},
                outcome=outcome if outcome in {"killed", "rejected", "abandoned"} else "killed",
                created_by=created_by,
                embedding=embed(text_for_embedding),
                signature=_tokens(text_for_embedding)[:60],
            )
            data["entries"].append(entry.to_dict())
            data["embedding_model"] = EMBEDDING_MODEL
            data["embedding_dim"] = EMBEDDING_DIM
            count = len(data["entries"])
        log.warning(
            "Post-mortem registado: %s (%s) — causa: %s", title, squad, death_cause[:120]
        )
        log.info("Cemitério com %d entradas.", count)
        return entry

    def remove(self, entry_id: str) -> None:
        with vault.transaction(POST_MORTEM) as data:
            before = len(data.get("entries", []))
            data["entries"] = [
                raw for raw in data.get("entries", []) if raw.get("id") != entry_id
            ]
            removed = before - len(data["entries"])
        if removed == 0:
            raise NotFound(f"Entrada post-mortem {entry_id} não existe.")
        log.info("Entrada %s removida do cemitério.", entry_id)

    # ------------------------------------------------------------------ leitura
    def list_entries(self, limit: int = 200) -> List[Dict[str, Any]]:
        entries = [entry.to_dict() for entry in self._entries()]
        return entries[-limit:][::-1]

    def stats(self) -> Dict[str, Any]:
        entries = self._entries()
        squads: Counter = Counter(entry.squad for entry in entries)
        causes: Counter = Counter(
            entry.death_cause.split(":")[0].strip()[:60] for entry in entries
        )
        return {
            "total": len(entries),
            "by_squad": dict(squads),
            "top_causes": dict(causes.most_common(8)),
            "embedding_model": EMBEDDING_MODEL,
            "similarity_block": self._similarity_block,
        }

    def find_similar(
        self, text: str, threshold: float = 0.35, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Entradas mortas semanticamente parecidas com ``text``."""
        scored: List[Tuple[float, PostMortemEntry, Dict[str, float]]] = []
        for entry in self._entries():
            reference = " ".join(
                part
                for part in (
                    entry.title,
                    entry.niche,
                    entry.death_cause,
                    " ".join(entry.tags),
                )
                if part
            )
            breakdown = similarity_score(text, reference)
            if breakdown["final"] >= threshold:
                scored.append((breakdown["final"], entry, breakdown))
        scored.sort(key=lambda triple: triple[0], reverse=True)
        return [
            {
                "similarity": round(score, 4),
                "breakdown": breakdown,
                "entry": entry.to_dict(),
            }
            for score, entry, breakdown in scored[:limit]
        ]

    def check_against_cemetery(
        self,
        *,
        idea: str,
        niche: str = "",
        tags: Optional[List[str]] = None,
        threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Portão obrigatório antes de produzir. Lança ``ValidationBlocked``.

        Devolve ``{"allowed": True, "matches": [...]}`` quando a ideia é nova.
        """
        effective_threshold = (
            self._similarity_block if threshold is None else float(threshold)
        )
        haystack = " ".join(
            part for part in (idea, niche, " ".join(tags or [])) if part
        ).strip()
        if not haystack:
            raise ValidationBlocked(
                "Não é possível validar uma ideia vazia contra o cemitério.",
                details={"reason": "ideia_vazia"},
            )
        matches = self.find_similar(haystack, threshold=effective_threshold)
        if matches:
            worst = matches[0]
            entry = worst["entry"]
            raise ValidationBlocked(
                "Ideia já falhou no passado — bloqueada pelo cemitério de negócios.",
                details={
                    "similarity": worst["similarity"],
                    "breakdown": worst.get("breakdown", {}),
                    "title": entry.get("title"),
                    "death_cause": entry.get("death_cause"),
                    "lessons": entry.get("lessons", [])[:3],
                    "threshold": effective_threshold,
                },
            )
        return {
            "allowed": True,
            "matches": [],
            "threshold": effective_threshold,
            "checked_at": utcnow_iso(),
            "corpus_size": len(self._entries()),
        }

    def lessons_for(self, squad: str, limit: int = 10) -> List[str]:
        """Lições agregadas de um esquadrão (injectadas no prompt do CrewAI)."""
        collected: List[str] = []
        for entry in reversed(self._entries()):
            if entry.squad.lower() != squad.lower():
                continue
            collected.extend(entry.lessons)
            if len(collected) >= limit:
                break
        return collected[:limit]


shared_memory = SharedMemory()


def memory_context_block(squad: str, idea: str) -> str:
    """Bloco de contexto textual com lições do passado, para prompts."""
    lessons = shared_memory.lessons_for(squad, limit=8)
    similar = shared_memory.find_similar(f"{idea} {squad}", threshold=0.22, limit=3)
    lines = ["=== MEMÓRIA CORPORATIVA (não repetir erros) ==="]
    if lessons:
        lines.extend(f"- {lesson}" for lesson in lessons)
    else:
        lines.append("- (sem lições registadas para este esquadrão)")
    if similar:
        lines.append("=== IDEIAS SEMELHANTES JÁ MORTA ===")
        for item in similar:
            entry = item["entry"]
            lines.append(
                f"- {entry['title']} (sim={item['similarity']:.2f}): "
                f"{entry['death_cause'][:160]}"
            )
    return "\n".join(lines)


__all__ = [
    "SharedMemory",
    "shared_memory",
    "PostMortemEntry",
    "embed",
    "cosine",
    "normalize_text",
    "words_of",
    "lexical_scores",
    "similarity_score",
    "memory_context_block",
    "EMBEDDING_MODEL",
    "STOPWORDS",
]
