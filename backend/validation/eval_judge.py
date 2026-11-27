"""Juiz Interno (EvalJudge): nada sai sem nota >= 85.

Avalia entregas de qualquer esquadrão em quatro eixos, com pesos ajustados ao
tipo de conteúdo:

  * **Retenção/gancho** — os primeiros 3 segundos (vídeo), o título (blog) ou
    a clareza da proposta (SaaS/ebook).
  * **Clareza** — legibilidade, estrutura, ausência de jargão vazio.
  * **Qualidade técnica** — bugs (código), CTA (copy), SEO (blog).
  * **Compliance/SEO** — licenças, promessas proibidas, densidade de keywords.

Estratégia de pontuação (defensável e reproduzível):
  1. **Métricas objetivas** calculadas em Python (sempre).
  2. **Revisão do LLM** (quando existe), com rubrica JSON e justificação.
  3. Nota final = 45% objetiva + 55% LLM (ou 100% objetiva sem LLM).
  4. Achados **críticos** (licença proibida, promessa de rendimento garantido,
     `TODO` no código entregue) forçam reprovação independentemente da média.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.clock import utcnow_iso
from ..core.llm_router import LLMRequest, _extract_json_object, llm_router
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.vault_store import SQUAD_METRICS, vault

log = get_logger("validation.judge")

CONTENT_TYPES = {"video_short", "blog_article", "ebook", "code", "landing_page", "copy"}

WEIGHTS: Dict[str, Dict[str, float]] = {
    "video_short": {"hook": 0.40, "clarity": 0.25, "technical": 0.20, "compliance": 0.15},
    "blog_article": {"hook": 0.25, "clarity": 0.25, "technical": 0.20, "compliance": 0.30},
    "ebook": {"hook": 0.20, "clarity": 0.30, "technical": 0.20, "compliance": 0.30},
    "code": {"hook": 0.05, "clarity": 0.20, "technical": 0.55, "compliance": 0.20},
    "landing_page": {"hook": 0.35, "clarity": 0.25, "technical": 0.20, "compliance": 0.20},
    "copy": {"hook": 0.35, "clarity": 0.30, "technical": 0.15, "compliance": 0.20},
}

FORBIDDEN_PROMISES = [
    r"rendimento\s+garantido",
    r"lucro\s+garantido",
    r"ganha[r]?\s+\$?\d+[\d\.,]*\s*(por|/)\s*(dia|semana|mês|mes)",
    r"guaranteed\s+(income|profit|returns?)",
    r"(sem|zero)\s+risco\s+de\s+perder",
    r"enriquec[ae]\s+(em|numa)\s+\d+\s+dias",
]

LAZY_MARKERS = [
    r"#\s*TODO",
    r"//\s*TODO",
    r"#\s*FIXME",
    r"implementar\s+depois",
    r"not\s+implemented(?!\s+exception)",
    r"\bpass\s*#\s*temp",
    r"\.\.\.\s*$",
]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass
class JudgeVerdict:
    score: float
    passed: bool
    content_type: str
    axes: Dict[str, float]
    findings: List[str]
    critical_findings: List[str]
    suggestions: List[str]
    engine: str
    attempts: int = 1
    evaluated_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "passed": self.passed,
            "content_type": self.content_type,
            "axes": {k: round(v, 2) for k, v in self.axes.items()},
            "findings": list(self.findings),
            "critical_findings": list(self.critical_findings),
            "suggestions": list(self.suggestions),
            "engine": self.engine,
            "attempts": self.attempts,
            "evaluated_at": self.evaluated_at,
        }


def _extract_axes(data: Dict[str, Any]) -> Dict[str, float]:
    """Normaliza os eixos de avaliação, aceitando 3 formas comuns do modelo.

    Aceita ``{"hook": 90, ...}``, ``{"axes": {"hook": 90, ...}}`` e
    ``{"scores": {...}}``; devolve ``{}`` se nada for utilizável.
    """
    candidates: Dict[str, Any] = {}
    for container in (data, data.get("axes"), data.get("scores")):
        if isinstance(container, dict):
            for key in ("hook", "clarity", "technical", "compliance"):
                if key in container:
                    candidates.setdefault(key, container[key])
    axes: Dict[str, float] = {}
    for key in ("hook", "clarity", "technical", "compliance"):
        if key not in candidates:
            continue
        try:
            axes[key] = _clamp(float(candidates[key]))
        except (TypeError, ValueError):
            continue
    return axes


class EvalJudge:
    """Juiz determinístico + LLM, com bloqueio obrigatório abaixo do limiar."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------- métricas objetivas
    def _objective_scores(
        self, content: str, content_type: str, metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Rubrica determinística: 4 eixos 0-100, calibrados por tipo de entrega.

        Calibração validada em testes: uma entrega genuinamente boa (gancho +
        CTA + estrutura + licenças) pontua 88-96; uma entrega fraca (sem gancho,
        demasiado curta, sem CTA) fica em 55-75; violações críticas forçam < 85.
        """
        text = content or ""
        lower = text.lower()
        findings: List[str] = []
        critical: List[str] = []
        suggestions: List[str] = []

        words = re.findall(r"[A-Za-zÀ-ÿ0-9'%]+", text)
        word_count = len(words)
        sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
        sentence_count = max(1, len(sentences))
        avg_sentence = word_count / sentence_count
        paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        first_120 = " ".join(words[:20]).lower()

        cta_patterns = (
            "link na bio", "segue", "segue-me", "comenta", "inscreve",
            "lista vip", "acesso antecipado", "clica", "quero", "subscreve",
            "guarda este", "partilha", "entra na lista", "compra", "reserva",
        )
        has_cta_text = any(token in lower for token in cta_patterns)
        has_numbers = bool(re.search(r"\d", text))
        strong_hook = any(
            token in first_120
            for token in (
                "imagina", "ninguem", "ninguém", "sabias", "atenção", "atencao",
                "espera", "nunca", "porque", "porquê", "erro", "verdade",
                "descobri", "segredo", "para", "antes de",
            )
        ) or ("?" in " ".join(sentences[:2]))

        # --- eixo 1: GANCHO / RETENÇÃO -------------------------------------
        hook = 55.0
        if content_type == "video_short":
            if word_count < 30:
                hook = 40.0
                findings.append(
                    f"Guião com {word_count} palavras: demasiado curto para um short."
                )
                suggestions.append("Expandir o guião para 60-200 palavras de fala.")
            elif word_count > 260:
                hook = 45.0
                findings.append(
                    f"Guião com {word_count} palavras: longo para um short de ~50s."
                )
                suggestions.append("Cortar para 200 palavras ou dividir em dois shorts.")
            else:
                hook = 62.0
            if strong_hook:
                hook += 16
                findings.append("Gancho forte nos primeiros segundos (pergunta/número).")
            else:
                suggestions.append(
                    "Abrir com gancho explícito (pergunta, número ou promessa "
                    "concreta) para segurar os 3 primeiros segundos."
                )
            if has_cta_text:
                hook += 12
                findings.append("CTA explícito presente no guião.")
            elif metadata.get("has_cta") is True:
                hook += 6
            else:
                suggestions.append("Fechar com CTA claro (seguir/comentar/link na bio).")
            if metadata.get("duration_seconds") is not None:
                try:
                    duration = float(metadata["duration_seconds"])
                    if duration > settings.video_max_duration_seconds:
                        hook -= 10
                        findings.append(
                            f"Duração {duration:.0f}s acima do alvo de short."
                        )
                except (TypeError, ValueError):
                    pass
            if len(" ".join(words[:14])) > 130:
                hook -= 6
                suggestions.append(
                    "Encurtar a primeira frase: o gancho deve caber em 3s de fala."
                )
        elif content_type in {"blog_article", "ebook"}:
            hook = 58.0
            title = str(metadata.get("title", "")).strip()
            if title:
                if 20 <= len(title) <= 70:
                    hook += 15
                    findings.append("Título no intervalo ótimo de SEO (20-70 chars).")
                else:
                    hook -= 8
                    suggestions.append("Ajustar o título para 20-70 caracteres.")
            minimum_words = 600 if content_type == "blog_article" else 1200
            if word_count >= minimum_words:
                hook += 15
                findings.append(f"Extensão adequada para {content_type}.")
            else:
                hook -= 10
                suggestions.append(
                    f"Expandir para pelo menos {minimum_words} palavras "
                    f"(actual: {word_count})."
                )
            if len(paragraphs) >= 3:
                hook += 7
            if has_numbers:
                hook += 5
        elif content_type == "code":
            hook = 70.0
            if metadata.get("tests_passing") is False:
                hook -= 25
                findings.append("Testes automatizados a falhar.")
            elif metadata.get("tests_passing") is True:
                hook += 10
            if metadata.get("has_tests") is False:
                suggestions.append("Adicionar testes automatizados antes da entrega.")
        else:  # landing_page | copy
            hook = 60.0
            if has_cta_text:
                hook += 16
                findings.append("Chamada à acção presente.")
            else:
                suggestions.append("Incluir CTA explícito (lista VIP / compra).")
            if has_numbers:
                hook += 9
                findings.append("Usa números concretos (aumenta credibilidade).")
            if "?" in text[:240]:
                hook += 7
            if word_count < 50:
                hook -= 10
                suggestions.append(
                    "Expandir a copy: oferecer contexto e prova antes do CTA."
                )
        hook = _clamp(hook)

        # --- eixo 2: CLAREZA -------------------------------------------------
        clarity = 70.0
        # Guiões de short têm frases naturalmente curtas: o intervalo aceitável
        # começa nos 6 vocábulos para não penalizar ritmo de fala.
        if 6 <= avg_sentence <= 25:
            clarity += 14
            findings.append(f"Frases legíveis (média {avg_sentence:.1f} palavras).")
        elif avg_sentence > 28:
            clarity -= 14
            findings.append(f"Frases longas (média {avg_sentence:.1f} palavras).")
            suggestions.append("Partir frases acima de 25 palavras.")
        if word_count >= 80:
            clarity += 8
        elif word_count < 40 and content_type not in {"code"}:
            clarity -= 10
            findings.append("Conteúdo muito curto para avaliar clareza.")
        if len(paragraphs) > 1 or content_type == "code":
            clarity += 6
        elif word_count > 220:
            clarity -= 8
            findings.append("Texto sem quebras de parágrafo (difícil de ler).")
        filler = len(
            re.findall(r"\b(muito|bastante|realmente|basicamente|coisa|coisas)\b", lower)
        )
        if filler > max(3, word_count // 120):
            clarity -= 6
            suggestions.append("Remover advérbios de enchimento e palavras vagas.")
        clarity = _clamp(clarity)

        # --- eixo 3: QUALIDADE TÉCNICA --------------------------------------
        technical = 65.0
        if content_type == "code":
            technical = 55.0
            if re.search(r"\b(def|class|function|const|let|fn|func)\b", text):
                technical += 15
            if "try" in lower and ("except" in lower or "catch" in lower):
                technical += 12
                findings.append("Tratamento de erros presente.")
            else:
                technical -= 10
                findings.append("Sem tratamento de erros explícito.")
                suggestions.append("Envolver chamadas externas em try/except com log.")
            if re.search(r":\s*(str|int|float|bool|List|Dict|Optional|->)", text):
                technical += 8
            lines = int(metadata.get("lines", 0) or 0)
            if lines and lines < 10:
                technical -= 10
                findings.append("Entrega de código demasiado pequena para a tarefa.")
        else:
            if has_cta_text:
                technical += 15
            elif content_type in {"landing_page", "copy", "video_short"}:
                technical -= 10
                suggestions.append("Falta uma chamada à acção explícita.")
            if has_numbers:
                technical += 8
            if content_type == "blog_article":
                headings = len(re.findall(r"^#{1,3}\s", text, re.MULTILINE))
                if headings >= 2:
                    technical += 8
                    findings.append(f"{headings} subtítulos para SEO e leitura.")
                else:
                    technical -= 6
                    suggestions.append("Adicionar subtítulos H2/H3 para SEO.")
                if re.search(r"https?://", text):
                    technical += 7
                    findings.append("Inclui ligações/fontes externas.")
                else:
                    suggestions.append("Citar pelo menos uma fonte externa verificável.")
            if metadata.get("broken_links"):
                technical -= 20
                findings.append("Ligações quebradas detectadas.")
        technical = _clamp(technical)

        # --- eixo 4: COMPLIANCE / LICENÇAS ----------------------------------
        compliance = 90.0
        for pattern in FORBIDDEN_PROMISES:
            if re.search(pattern, lower):
                compliance -= 45
                critical.append(
                    f"Promessa proibida detectada ('{pattern}'): viola políticas de "
                    "publicidade (Gumroad/Meta/Google Ads)."
                )
        if re.search(r"\b(chattts|f5-tts|f5_tts)\b", lower):
            compliance -= 60
            critical.append("Referência a modelo CC-BY-NC proibido (ChatTTS/F5-TTS).")
        for pattern in LAZY_MARKERS:
            if re.search(pattern, text, re.MULTILINE):
                compliance -= 30
                critical.append(
                    f"Entrega incompleta detectada ('{pattern.strip()}'): "
                    "é proibido código/conteúdo inacabado."
                )
                break
        if re.search(r"\b(afiliad|affiliate|link pago|publicidade paga)\b", lower):
            if not re.search(
                r"(aviso|declara|divulga|advertising|disclosure|comissão|comissao)",
                lower,
            ):
                compliance -= 12
                findings.append(
                    "Conteúdo de afiliados sem declaração de relação comercial."
                )
                suggestions.append("Adicionar aviso de afiliação no topo do conteúdo.")
        if re.search(
            r"(imagem|video|vídeo|musica|música|footage)\s+(da|de)\s+"
            r"(internet|google|tiktok)\b",
            lower,
        ):
            compliance -= 25
            findings.append("Referência a mídia sem proveniência/licença declarada.")
        compliance = _clamp(compliance)

        return {
            "axes": {
                "hook": hook,
                "clarity": clarity,
                "technical": technical,
                "compliance": compliance,
            },
            "findings": findings,
            "critical": critical,
            "suggestions": suggestions,
            "stats": {
                "words": word_count,
                "sentences": sentence_count,
                "avg_sentence_words": round(avg_sentence, 1),
                "paragraphs": len(paragraphs),
            },
        }

    # ------------------------------------------------------------ revisão LLM
    def _llm_review(
        self, content: str, content_type: str, metadata: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        try:
            request = LLMRequest(
                prompt=(
                    "Avalia a entrega abaixo como juiz de qualidade de uma holding "
                    "de mídia e software. Sê exigente e concreto.\n"
                    f"TIPO: {content_type}\n"
                    f"METADADOS: {json.dumps(metadata, ensure_ascii=False)[:800]}\n"
                    f"ENTREGA (truncada):\n{content[:6000]}\n\n"
                    "Devolve JSON com: hook (0-100), clarity (0-100), technical "
                    "(0-100), compliance (0-100), findings (lista de frases), "
                    "suggestions (lista accionável)."
                ),
                system=(
                    "És o Juiz Interno da Automaton Holding. Reprovas abaixo de 85. "
                    "Valorizas retenção nos 3 primeiros segundos, clareza, ausência "
                    "de bugs e conformidade legal. Nunca aprovas promessas de "
                    "rendimento garantido nem código incompleto."
                ),
                task="judge",
                json_mode=True,
                max_tokens=900,
                temperature=0.2,
                agent_id="eval_judge",
                context={"squad": "validation"},
                json_schema={
                    "type": "object",
                    "properties": {
                        "hook": {"type": "number"},
                        "clarity": {"type": "number"},
                        "technical": {"type": "number"},
                        "compliance": {"type": "number"},
                        "findings": {"type": "array"},
                        "suggestions": {"type": "array"},
                    },
                },
            )
            # Um único ``complete`` em vez de ``complete_json``: este último
            # exigiria as 4 chaves de eixo e, se o modelo devolvesse outra
            # forma (ex.: ``axes: {...}``), o modo local preenchia-as com 0 e
            # reprovava uma entrega boa. Aqui aceitamos as duas formas e, sem
            # eixos utilizáveis, devolvemos ``None`` para pontuar só com as
            # métricas objectivas.
            response = llm_router.complete(request)
            if not response.is_llm:
                return None
            data = _extract_json_object(response.text) or {}
            axes = _extract_axes(data)
            if not axes:
                log.warning(
                    "Juiz LLM devolveu sem eixos utilizáveis (%s); a usar só "
                    "métricas objectivas.", response.provider,
                )
                return None
            findings = data.get("findings") or []
            suggestions = data.get("suggestions") or []
            if isinstance(findings, str):
                findings = [findings]
            if isinstance(suggestions, str):
                suggestions = [suggestions]
            return {
                "axes": axes,
                "findings": [str(f)[:240] for f in findings][:8],
                "suggestions": [str(s)[:240] for s in suggestions][:8],
                "engine": response.provider,
            }
        except Exception as exc:
            log.warning("Revisão LLM do juiz falhou (%s); só métricas objetivas.", exc)
            return None

    # ---------------------------------------------------------------- público
    def evaluate(
        self,
        *,
        content: str,
        content_type: str = "copy",
        squad_id: str = "unknown",
        agent_id: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
        min_score: Optional[float] = None,
        attempts: int = 1,
    ) -> JudgeVerdict:
        """Avalia e devolve o veredicto (não lança; usar ``enforce`` para bloquear)."""
        if content_type not in CONTENT_TYPES:
            content_type = "copy"
        meta = dict(metadata or {})
        threshold = float(
            min_score if min_score is not None else settings.eval_min_score
        )

        objective = self._objective_scores(content, content_type, meta)
        llm = self._llm_review(content, content_type, meta)

        weights = WEIGHTS[content_type]
        if llm is not None:
            axes = {
                key: round(
                    0.45 * objective["axes"][key] + 0.55 * llm["axes"].get(key, objective["axes"][key]),
                    2,
                )
                for key in ("hook", "clarity", "technical", "compliance")
            }
            engine = f"hybrid:{llm.get('engine', 'llm')}+objective"
        else:
            axes = {key: round(value, 2) for key, value in objective["axes"].items()}
            engine = "objective"

        weighted = sum(axes[key] * weights[key] for key in weights)
        score = round(_clamp(weighted), 2)

        findings = list(objective["findings"])
        suggestions = list(objective["suggestions"])
        if llm is not None:
            findings.extend(llm["findings"])
            suggestions.extend(llm["suggestions"])

        critical = list(objective["critical"])
        passed = score >= threshold and not critical
        if critical:
            # Reprovação forçada: a nota reflecte a gravidade mas não aprova.
            score = min(score, threshold - 1.0)

        verdict = JudgeVerdict(
            score=score,
            passed=passed,
            content_type=content_type,
            axes=axes,
            findings=findings[:14],
            critical_findings=critical,
            suggestions=suggestions[:12],
            engine=engine,
            attempts=attempts,
        )

        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            evaluations = data.setdefault("eval_history", [])
            evaluations.append(
                {
                    "squad_id": squad_id,
                    "agent_id": agent_id,
                    "content_type": content_type,
                    "content_hash": f"len{len(content)}",
                    "verdict": verdict.to_dict(),
                    "metadata": meta,
                    "at": utcnow_iso(),
                }
            )
            data["eval_history"] = evaluations[-200:]

        level = "info" if passed else "warning"
        event_bus.publish(
            "eval_verdict",
            agent="eval_judge",
            message=(
                f"Juiz: {score:.1f}/100 ({content_type}) — "
                f"{'APROVADO' if passed else 'REPROVADO'} "
                f"[{squad_id}/{agent_id}]"
            ),
            level=level,
            payload={
                "score": score,
                "passed": passed,
                "threshold": threshold,
                "squad_id": squad_id,
                "axes": axes,
                "critical": critical,
            },
        )
        if not passed:
            log.warning(
                "REPROVADO %.2f < %.2f (%s/%s): %s",
                score, threshold, squad_id, agent_id,
                "; ".join((critical or findings)[:3])[:200],
            )
        return verdict

    def enforce(
        self,
        *,
        content: str,
        content_type: str = "copy",
        squad_id: str = "unknown",
        agent_id: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
        min_score: Optional[float] = None,
        regenerate: Optional[Any] = None,
    ) -> JudgeVerdict:
        """Avalia e **bloqueia** abaixo do limiar.

        Se ``regenerate`` for um callable ``(feedback: str) -> str``, o juiz
        tenta até ``EVAL_MAX_RETRIES`` vezes obter uma versão aprovada. Sem
        aprovação, lança ``ValidationBlocked``.
        """
        from ..core.errors import ValidationBlocked

        threshold = float(min_score if min_score is not None else settings.eval_min_score)
        current = content
        verdict = self.evaluate(
            content=current,
            content_type=content_type,
            squad_id=squad_id,
            agent_id=agent_id,
            metadata=metadata,
            min_score=threshold,
            attempts=1,
        )
        attempt = 1
        while (
            not verdict.passed
            and callable(regenerate)
            and attempt < max(1, int(settings.eval_max_retries))
        ):
            attempt += 1
            feedback = (
                f"Nota {verdict.score:.1f} < {threshold:.0f}. Achados: "
                + "; ".join((verdict.critical_findings or verdict.findings)[:5])
            )
            try:
                regenerated = regenerate(feedback)
            except Exception as exc:
                log.error("Regeneração falhou: %s", exc)
                break
            if not regenerated or regenerated == current:
                break
            current = regenerated
            verdict = self.evaluate(
                content=current,
                content_type=content_type,
                squad_id=squad_id,
                agent_id=agent_id,
                metadata=metadata,
                min_score=threshold,
                attempts=attempt,
            )

        if not verdict.passed:
            raise ValidationBlocked(
                f"Juiz Eval reprovou a entrega ({verdict.score:.1f} < {threshold:.0f}).",
                details=verdict.to_dict(),
            )
        return verdict

    # -------------------------------------------------------------- relatório
    def report(self) -> Dict[str, Any]:
        history = vault.read(SQUAD_METRICS).get("eval_history", [])
        scores = [
            float(item["verdict"]["score"])
            for item in history
            if isinstance(item.get("verdict"), dict) and "score" in item["verdict"]
        ]
        passed = len([item for item in history if item.get("verdict", {}).get("passed")])
        by_type: Dict[str, List[float]] = {}
        for item in history:
            verdict = item.get("verdict") or {}
            if "score" in verdict:
                by_type.setdefault(str(verdict.get("content_type", "copy")), []).append(
                    float(verdict["score"])
                )
        return {
            "threshold": settings.eval_min_score,
            "total_evaluations": len(history),
            "passed": passed,
            "failed": len(history) - passed,
            "pass_rate": round(passed / len(history), 4) if history else 0.0,
            "average_score": round(sum(scores) / len(scores), 2) if scores else None,
            "by_content_type": {
                key: {"count": len(values), "avg": round(sum(values) / len(values), 2)}
                for key, values in by_type.items()
            },
            "recent": history[-10:][::-1],
            "generated_at": utcnow_iso(),
        }


eval_judge = EvalJudge()

__all__ = ["EvalJudge", "eval_judge", "JudgeVerdict", "WEIGHTS", "CONTENT_TYPES"]
