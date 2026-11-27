"""Esquadrão Scout: caça 24/7 brechas lucrativas e pontua-as com honestidade.

Fontes integradas (todas gratuitas e sem chave):
  * **GitHub Trending** — repositórios em alta nas últimas 24h.
  * **Reddit** — JSON público de ``r/SideProject``, ``r/Entrepreneur`` e
    ``r/PassiveIncome`` (respeitando o User-Agent identificável e o rate limit).

Fontes registadas mas **desactivadas por falta de endpoint público estável**
(TikTok Creative Center, Gumroad Discover, AppSumo, YouTube Trending): o Scout
reporta-as como ``skipped`` com a razão, em vez de inventar dados. Quando o
utilizador configura ``CRAWL4AI_ENABLED``/``GPT_RESEARCHER_ENABLED`` ou chaves
oficiais, os adaptadores passam a ser usados.

Cada oportunidade recebe um *score de demanda* determinístico (0-100) baseado em
sinais observáveis (estrelas/upvotes/comentários/recência) e é cruzada com o
cemitério de negócios. As melhores são convertidas em propostas de smoke test —
nunca em produção directa.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config.settings import settings
from ..core.clock import parse_iso, utcnow, utcnow_iso
from ..core.llm_router import LLMRequest, llm_router
from ..core.logger import get_logger
from ..core.security_guard import prompt_shield
from ..core.shared_memory import shared_memory
from .research_backends import research_backends
from .squad_base import BaseSquad, SquadRunResult

log = get_logger("squads.scout")

HTTP_TIMEOUT = 25
SOURCE_LABELS = {
    "github_trending": "GitHub Trending",
    "reddit_sideproject": "Reddit r/SideProject",
    "reddit_entrepreneur": "Reddit r/Entrepreneur",
    "reddit_passiveincome": "Reddit r/PassiveIncome",
    "gumroad": "Gumroad Discover",
    "appsumo": "AppSumo",
    "youtube_trending": "YouTube Trending",
    "tiktok_creative": "TikTok Creative Center",
}

SUBREDDITS = {
    "reddit_sideproject": "SideProject",
    "reddit_entrepreneur": "Entrepreneur",
    "reddit_passiveincome": "PassiveIncome",
}

# Fontes sem endpoint público estável: exigem chave/API oficial ou Crawl4AI.
SOURCES_REQUIRING_TOOLING = {"gumroad", "appsumo", "youtube_trending", "tiktok_creative"}


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: int = HTTP_TIMEOUT) -> Tuple[int, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", settings.scout_user_agent)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as exc:
        log.info("Scout: HTTP %s em %s", exc.code, url.split("?")[0])
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log.info("Scout: falha de rede em %s (%s)", url.split("?")[0], exc)
        return 0, None


def _http_get_text(url: str, timeout: int = HTTP_TIMEOUT) -> Tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", settings.scout_user_agent)
    request.add_header("Accept", "text/html")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, ""


@dataclass
class ScoutOpportunity:
    id: str
    source: str
    title: str
    url: str
    signal: Dict[str, Any]
    niche: str
    demand_score: float
    squad_type: str
    rationale: str
    cemetery_status: str = "clear"  # clear | risky | blocked
    cemetery_similarity: float = 0.0
    discovered_at: str = field(default_factory=utcnow_iso)
    raw_excerpt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _first_int(text: str, pattern: str) -> int:
    """Primeiro número inteiro que casa com ``pattern`` (0 se não houver).

    Aceita separadores de milhar ("1,234" ou "1.234") e devolve sempre ``int``.
    """
    match = re.search(pattern, text or "", flags=re.IGNORECASE)
    if not match:
        return 0
    digits = re.sub(r"[^0-9]", "", match.group(1))
    return int(digits) if digits else 0


def _slug_words(text: str, limit: int = 5) -> str:
    """Núcleo temático de um título, em minúsculas e sem pontuação."""
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "uma", "para",
        "com", "dos", "das", "nas", "nos", "por", "que", "como", "mais",
    }
    words = [w for w in re.findall(r"[\wÀ-ÿ]+", (text or "").lower()) if len(w) > 3]
    return " ".join([w for w in words if w not in stop][:limit])


class ScoutHunter(BaseSquad):
    """Agente Caçador: descobre, pontua e filtra oportunidades."""

    squad_type = "scout"
    squad_name = "Agente Scout"
    owner_agent = "scout_hunter"
    requires_validation = False
    content_type = "copy"

    # --------------------------------------------------------------- fontes
    def _fetch_github_trending(self, limit: int = 8) -> List[ScoutOpportunity]:
        """Repositórios criados nos últimos 30 dias ordenados por estrelas."""
        since = (utcnow().replace(microsecond=0)).date().fromordinal(
            utcnow().date().toordinal() - 30
        ).isoformat()
        url = (
            "https://api.github.com/search/repositories?q="
            + urllib.parse.quote(f"created:>{since} stars:>25")
            + "&sort=stars&order=desc&per_page="
            + str(limit)
        )
        headers = {"Accept": "application/vnd.github+json"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        status, payload = _http_get_json(url, headers=headers)
        if status != 200 or not isinstance(payload, dict):
            return []
        opportunities: List[ScoutOpportunity] = []
        for repo in payload.get("items", []) or []:
            stars = int(repo.get("stargazers_count", 0) or 0)
            forks = int(repo.get("forks_count", 0) or 0)
            issues = int(repo.get("open_issues_count", 0) or 0)
            description = sanitize(repo.get("description") or "")
            name = str(repo.get("full_name", ""))
            license_info = (repo.get("license") or {}).get("spdx_id", "NOASSERTION")
            score = self._score_demand(
                primary=stars,
                secondary=forks,
                engagement=issues,
                age_days=self._age_days(repo.get("created_at")),
            )
            opportunities.append(
                ScoutOpportunity(
                    id=self.stable_id("opp", "github", name),
                    source="github_trending",
                    title=f"{name}: {description[:120] or 'repositório em alta'}",
                    url=str(repo.get("html_url", "")),
                    signal={
                        "stars": stars,
                        "forks": forks,
                        "open_issues": issues,
                        "license": license_info,
                        "created_at": repo.get("created_at"),
                    },
                    niche=self._guess_niche(f"{name} {description}"),
                    demand_score=score,
                    squad_type="micro_saas_dev",
                    rationale=(
                        f"{stars} estrelas e {forks} forks em poucos dias indicam "
                        "dor real; oportunidade de empacotar como micro-SaaS."
                    ),
                    raw_excerpt=description[:400],
                )
            )
        return opportunities

    def _fetch_reddit(self, source_key: str, limit: int = 10) -> List[ScoutOpportunity]:
        subreddit = SUBREDDITS.get(source_key)
        if not subreddit:
            return []
        url = (
            f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}&raw_json=1"
        )
        status, payload = _http_get_json(url)
        if status != 200 or not isinstance(payload, dict):
            return []
        children = ((payload.get("data") or {}).get("children") or [])
        opportunities: List[ScoutOpportunity] = []
        for child in children[:limit]:
            data = child.get("data") or {}
            title = sanitize(str(data.get("title", "")))[:240]
            if not title:
                continue
            ups = int(data.get("ups", 0) or 0)
            comments = int(data.get("num_comments", 0) or 0)
            age_days = self._age_hours(data.get("created_utc")) / 24.0
            score = self._score_demand(
                primary=ups, secondary=comments, engagement=comments, age_days=age_days
            )
            opportunities.append(
                ScoutOpportunity(
                    id=self.stable_id("opp", source_key, data.get("id", title)),
                    source=source_key,
                    title=title,
                    url="https://www.reddit.com" + str(data.get("permalink", "")),
                    signal={
                        "upvotes": ups,
                        "comments": comments,
                        "subreddit": subreddit,
                        "created_utc": data.get("created_utc"),
                    },
                    niche=self._guess_niche(title),
                    demand_score=score,
                    squad_type=self._guess_squad(title),
                    rationale=(
                        f"{ups} upvotes / {comments} comentários em r/{subreddit}: "
                        "sinal de dor ou de vontade de pagar."
                    ),
                    raw_excerpt=sanitize(str(data.get("selftext", "")))[:600],
                )
            )
        return opportunities

    # -------------------------------------------------------------- scoring
    @staticmethod
    def _age_hours(created_utc: Any) -> float:
        try:
            return max(
                0.0,
                (utcnow().timestamp() - float(created_utc)) / 3600.0,
            )
        except (TypeError, ValueError):
            return 72.0

    @staticmethod
    def _age_days(iso_value: Any) -> float:
        moment = parse_iso(str(iso_value)) if iso_value else None
        if moment is None:
            return 30.0
        return max(0.0, (utcnow() - moment).total_seconds() / 86400.0)

    @staticmethod
    def _score_demand(
        *, primary: int, secondary: int, engagement: int, age_days: float
    ) -> float:
        """Pontuação 0-100: magnitude do sinal com decaimento temporal."""
        import math

        magnitude = math.log10(1 + max(0, primary)) * 22.0
        secondary_bonus = math.log10(1 + max(0, secondary)) * 12.0
        engagement_bonus = math.log10(1 + max(0, engagement)) * 10.0
        freshness = max(0.15, 1.0 - min(1.0, max(0.0, age_days) / 30.0))
        raw = (magnitude + secondary_bonus + engagement_bonus) * freshness
        return round(max(0.0, min(100.0, raw)), 2)

    @staticmethod
    def _guess_niche(text: str) -> str:
        lowered = (text or "").lower()
        mapping = {
            "financ": "finanças",
            "money": "finanças",
            "invest": "investimentos",
            "crypto": "cripto",
            "fitness": "fitness",
            "health": "saúde",
            "kids": "infantil educativo",
            "education": "educação",
            "cook": "culinária",
            "recipe": "culinária",
            "travel": "viagens",
            "ai": "inteligência artificial",
            "gpt": "inteligência artificial",
            "saas": "software B2B",
            "crm": "software B2B",
            "invoice": "software B2B",
            "fleet": "logística",
            "logistic": "logística",
            "market": "marketing",
            "seo": "marketing",
            "podcast": "media",
            "design": "design",
            "notion": "produtividade",
            "template": "produtividade",
        }
        for token, niche in mapping.items():
            if token in lowered:
                return niche
        return "geral"

    @staticmethod
    def _guess_squad(text: str) -> str:
        lowered = (text or "").lower()
        if any(token in lowered for token in ("saas", "app", "api", "tool", "dev", "code", "github")):
            return "micro_saas_dev"
        if any(token in lowered for token in ("ebook", "course", "template", "blog", "seo", "affiliate", "notion")):
            return "infoproducts_affiliate"
        if any(token in lowered for token in ("tiktok", "shorts", "reels", "video", "youtube", "podcast")):
            return "video_factory"
        if any(token in lowered for token in ("b2b", "lead", "list", "print", "dropship", "design")):
            return "data_b2b"
        return "infoproducts_affiliate"

    # ----------------------------------------------------------------- ciclo
    def scan_sources(
        self, sources: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Varre as fontes pedidas e devolve oportunidades + fontes saltadas."""
        selected = sources or settings.scout_sources
        opportunities: List[ScoutOpportunity] = []
        skipped: List[Dict[str, str]] = []
        fetched: List[str] = []
        per_source: Dict[str, int] = {}

        research_used: List[Dict[str, Any]] = []

        for source in selected:
            items: List[ScoutOpportunity] = []
            if source == "github_trending":
                items = self._fetch_github_trending()
            elif source in SUBREDDITS:
                items = self._fetch_reddit(source)
            elif source == "deep_research":
                # Crawl4AI / GPT-Researcher / extrator nativo: páginas reais
                # passadas pelo escudo anti-injection antes de virarem sinais.
                items, meta = self._fetch_deep_research()
                research_used.append(meta)
            if source in SOURCES_REQUIRING_TOOLING and not items:
                availability = research_backends.availability()
                reason = (
                    "pesquisa profunda sem resultados legíveis"
                    if availability["active"] != "native"
                    else "requer Crawl4AI/GPT-Researcher ou chave oficial"
                    f" (motores prontos: {availability['active']})"
                )
                skipped.append({"source": source, "reason": reason})
                continue
            if source not in ("github_trending", "deep_research", *SUBREDDITS.keys()):
                skipped.append({"source": source, "reason": "fonte desconhecida"})
                continue
            per_source[source] = len(items)
            if items:
                fetched.append(source)
            else:
                skipped.append(
                    {"source": source, "reason": "sem resultados (rede, rate limit ou vazio)"}
                )
            opportunities.extend(items)

        # Deduplicação por id e ordenação por score.
        unique: Dict[str, ScoutOpportunity] = {}
        for item in opportunities:
            unique[item.id] = item
        ranked = sorted(unique.values(), key=lambda o: o.demand_score, reverse=True)

        # Cruzamento com o cemitério: nunca propor o que já morreu.
        for item in ranked:
            try:
                matches = shared_memory.find_similar(
                    f"{item.title} {item.niche}", threshold=0.3, limit=1
                )
            except Exception as exc:
                log.debug("Cemitério indisponível: %s", exc)
                matches = []
            if matches:
                item.cemetery_status = "blocked"
                item.cemetery_similarity = float(matches[0]["similarity"])
                item.rationale += (
                    f" [BLOQUEADA] semelhante a negócio morto: "
                    f"{matches[0]['entry'].get('title')} ({item.cemetery_similarity:.2f})"
                )
            elif any(
                token in item.title.lower() for token in ("financ", "crypto")
            ) and item.demand_score < 40:
                item.cemetery_status = "risky"

        clean = [o for o in ranked if o.cemetery_status != "blocked"]
        return {
            "opportunities": [o.to_dict() for o in ranked],
            "viable": [o.to_dict() for o in clean],
            "sources_fetched": fetched,
            "sources_skipped": skipped,
            "per_source": per_source,
            "total_found": len(ranked),
            "viable_count": len(clean),
            "deep_research": research_used,
            "research_engines": research_backends.availability(),
            "scanned_at": utcnow_iso(),
        }

    # ----------------------------------------------------- pesquisa profunda
    def _fetch_deep_research(
        self, topic: str = "micro saas inventory management small business"
    ) -> tuple[List[ScoutOpportunity], Dict[str, Any]]:
        """Converte páginas reais em oportunidades com sinal de demanda.

        As páginas vêm do `research_backends` (Crawl4AI → GPT-Researcher →
        extrator nativo) e o texto já passou pelo escudo anti-injection. Só
        geramos oportunidade quando há sinal numérico observável (estrelas,
        pontos, comentários) na página.
        """
        import unicodedata

        normalized = "".join(
            char for char in unicodedata.normalize("NFKD", topic or "")
            if not unicodedata.combining(char)
        )
        result = research_backends.deep_research(normalized.strip() or "micro saas")
        items: List[ScoutOpportunity] = []
        for page in result.get("results", [])[: settings.research_max_results]:
            text = str(page.get("markdown", ""))
            if not text:
                continue
            title = page.get("title") or page.get("url", "")[:80]
            # Uma listagem de API é um *contentor* de sinais, não um negócio:
            # nesse caso o título vem do primeiro item da lista.
            if title.lower().startswith("api "):
                first_line = next(
                    (line for line in text.splitlines() if line.strip()), ""
                )
                title = first_line.lstrip("- ").split(" — ")[0][:180] or title
            stars = _first_int(text, r"(\d[\d,\.]*)\s*(?:stars?|estrelas?)")
            points = _first_int(text, r"(\d[\d,\.]*)\s*points?")
            comments = _first_int(text, r"(\d[\d,\.]*)\s*comments?")
            signal = {
                "stars": stars, "points": points, "comments": comments,
                "chars": int(page.get("chars", 0)), "engine": page.get("source"),
            }
            if not any((stars, points, comments)):
                continue
            # Uma única página não pode valer 100 (isso falsearia o ranking):
            # normalizamos e limitamos a 92 para deixar espaço a sinais
            # multi-fonte, que são os que desbloqueiam produção.
            raw_demand = self._score_demand(
                primary=int(max(stars, points)),
                secondary=int(points),
                engagement=int(comments),
                age_days=0.0,   # página fresca: sem decaimento
            )
            demand = round(min(92.0, raw_demand / 1.8), 2)
            niche = _slug_words(title) or "geral"
            items.append(
                ScoutOpportunity(
                    id="dr_" + hashlib.sha1(
                        (title + page.get("url", "")).encode("utf-8")
                    ).hexdigest()[:12],
                    source=str(page.get("source") or "deep_research"),
                    title=title[:180],
                    url=str(page.get("url", "")),
                    signal=signal,
                    niche=niche,
                    demand_score=demand,
                    squad_type=self._guess_squad(f"{title} {text[:400]}"),
                    rationale=(
                        f"Página real analisada ({page.get('source')}); "
                        f"estrelas={stars} pontos={points} comentários={comments}. "
                        f"Trecho: {text[:180].strip()}"
                    ),
                    raw_excerpt=text[:400],
                )
            )
        return items, {
            "engine": result.get("engine"),
            "ok": result.get("ok"),
            "pages": len(result.get("results", [])),
            "blocked": result.get("blocked", 0),
            "reason": result.get("reason", ""),
        }

    def propose(self, opportunity_id: str, scan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Transforma uma oportunidade em proposta de smoke test (com LLM)."""
        target = next(
            (o for o in scan.get("viable", []) if o["id"] == opportunity_id), None
        )
        if target is None:
            return None
        prompt = (
            "Transforma este sinal de mercado numa proposta de Smoke Test de "
            "demanda (não em produção).\n"
            f"SINAL: {target['title']}\nFONTE: {target['source']} {target['url']}\n"
            f"NICHO: {target['niche']}\nSQUAD: {target['squad_type']}\n"
            f"SINAIS: {json.dumps(target['signal'], ensure_ascii=False)[:400]}\n\n"
            "Devolve JSON com: idea (1 frase concreta), hypothesis (como validar "
            "em 3-7 dias com custo $0), cta (chamada à acção), "
            "risks (lista), price_anchor_usd (número)."
        )
        request = LLMRequest(
            prompt=prompt,
            system=self._default_system(),
            task="strategy",
            json_mode=True,
            max_tokens=700,
            agent_id=self.owner_agent,
            context={"squad": self.squad_type},
            json_schema={
                "type": "object",
                "properties": {
                    "idea": {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "cta": {"type": "string"},
                    "risks": {"type": "array"},
                    "price_anchor_usd": {"type": "number"},
                },
            },
        )
        data, response = llm_router.complete_json(
            request,
            required_keys=["idea", "hypothesis", "cta", "risks", "price_anchor_usd"],
        )
        is_llm = response.is_llm
        if not is_llm:
            # O motor local não "inventa" copy: a proposta é construída por
            # regras a partir do próprio sinal, marcada como não-LLM.
            return {
                "opportunity_id": opportunity_id,
                "squad_type": target["squad_type"],
                "niche": target["niche"],
                "source": target["source"],
                "source_url": target["url"],
                "idea": f"Versão paga/prática de: {target['title'][:180]}",
                "hypothesis": (
                    f"Se existe demanda visível em {target['source']} "
                    f"({target['demand_score']:.0f}/100), uma oferta focada em "
                    f"{target['niche']} com pré-venda converterá >= "
                    f"{settings.smoke_min_conversion_rate:.0%} em "
                    f"{settings.smoke_min_days}-{settings.smoke_max_days} dias."
                ),
                "cta": "Quero entrar na lista VIP",
                "price_anchor_usd": 19.0 if target["squad_type"] == "infoproducts_affiliate" else 9.0,
                "risks": [
                    "Validar ToS da fonte antes de automatizar recolha.",
                    "Confirmar licença de qualquer ferramenta reutilizada.",
                ],
                "demand_score": target["demand_score"],
                "engine": "rules",
                "is_llm": False,
                "note": (
                    "Proposta derivada por regras (sem LLM). Requer revisão humana "
                    "antes de criar o smoke test."
                ),
            }
        idea = str(data.get("idea") or target["title"])[:300]
        try:
            price = round(float(data.get("price_anchor_usd") or 0.0), 2)
        except (TypeError, ValueError):
            price = 0.0
        risks = data.get("risks") or []
        if isinstance(risks, str):
            risks = [risks]
        return {
            "opportunity_id": opportunity_id,
            "squad_type": target["squad_type"],
            "niche": target["niche"],
            "source": target["source"],
            "source_url": target["url"],
            "idea": idea,
            "hypothesis": str(data.get("hypothesis") or "")[:400],
            "cta": str(data.get("cta") or "Quero entrar na lista VIP")[:160],
            "price_anchor_usd": max(0.0, price),
            "risks": [str(r)[:200] for r in risks][:6],
            "demand_score": target["demand_score"],
            "engine": response.provider,
            "is_llm": is_llm,
            "note": (
                "Proposta gerada pelo LLM."
                if is_llm
                else "Proposta do motor local — requer revisão humana antes de "
                     "criar o smoke test."
            ),
        }

    def run(self, *, max_viable: int = 5, sources: Optional[List[str]] = None) -> SquadRunResult:
        """Ciclo completo: varrer -> pontuar -> filtrar -> propor."""
        self.ensure_operational("varrer fontes")
        scan = self.scan_sources(sources)

        proposals: List[Dict[str, Any]] = []
        for item in scan["viable"][: max(0, int(max_viable))]:
            try:
                proposal = self.propose(item["id"], scan)
                if proposal:
                    proposals.append(proposal)
            except Exception as exc:
                log.warning("Proposta falhou para %s: %s", item["id"], exc)

        self.persist_artifact(
            {
                "type": "scout_report",
                "scan": scan,
                "proposals": proposals,
                "at": utcnow_iso(),
            }
        )
        tokens = 0
        self.record_agent_execution(success=bool(scan["total_found"]), tokens=tokens)
        self.publish(
            "scout_scan_completed",
            f"Scout encontrou {scan['total_found']} sinais, "
            f"{scan['viable_count']} viáveis, {len(proposals)} propostas.",
            viable=scan["viable_count"],
            skipped=scan["sources_skipped"],
        )

        status = "completed" if scan["total_found"] else "blocked"
        summary = (
            f"{scan['total_found']} oportunidades encontradas "
            f"({scan['viable_count']} viáveis após cruzar com o cemitério). "
            f"Fontes consultadas: {', '.join(scan['sources_fetched']) or 'nenhuma'}."
        )
        blockers = (
            []
            if scan["total_found"]
            else [
                {
                    "reason": "nenhum sinal obtido",
                    "skipped_sources": scan["sources_skipped"],
                    "hint": "Verificar conectividade ou configurar CRAWL4AI_ENABLED/GITHUB_TOKEN.",
                }
            ]
        )
        return SquadRunResult(
            squad_id="scout",
            squad_type=self.squad_type,
            action="scan",
            status=status,
            summary=summary,
            artifacts=[{"type": "scan", **scan}] + proposals,
            metrics={
                "total_found": scan["total_found"],
                "viable": scan["viable_count"],
                "proposals": len(proposals),
                "sources_fetched": scan["sources_fetched"],
                "sources_skipped": scan["sources_skipped"],
                "per_source": scan.get("per_source", {}),
            },
            next_actions=[
                "Criar smoke test para a melhor proposta (DemandValidator).",
                "Nunca produzir antes de o smoke test atingir a meta.",
            ],
            blockers=blockers,
            llm_provider="router",
            is_llm=any(p.get("is_llm") for p in proposals),
        )


def sanitize(text: str) -> str:
    """Remove instruções maliciosas de conteúdo raspado (anti prompt-injection)."""
    return prompt_shield.scan(text or "", source="web").text


scout_hunter = ScoutHunter()

__all__ = ["ScoutHunter", "ScoutOpportunity", "scout_hunter", "SOURCE_LABELS"]
