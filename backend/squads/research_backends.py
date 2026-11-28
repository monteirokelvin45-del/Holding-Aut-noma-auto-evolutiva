"""Pesquisa profunda para o Scout: Crawl4AI (Apache-2.0) e GPT-Researcher.

O Scout já consulta APIs JSON (GitHub, Reddit, marketplaces). O que faltava era
ler **páginas reais** — um README, uma thread, uma landing page — e extrair
sinais de demanda em markdown. É isso que este módulo faz, com três cuidados:

1. **Licença**: Crawl4AI é Apache-2.0, GPT-Researcher é Apache-2.0. Ambos
   aceites pelo departamento jurídico.
2. **Segurança**: todo o texto recolhido é conteúdo *não confiável*. Passa pelo
   `prompt_shield` (LLM Guard) antes de entrar em qualquer prompt, e é
   truncado a `max_untrusted_content_chars`. Um README malicioso não sequestra
   um agente.
3. **Custo**: nenhum. Crawl4AI corre local (Playwright) e o GPT-Researcher usa
   o nosso `llm_router` (free-tier). Sem chaves, degrada para um extrator
   simples de markdown com `urllib` — nunca falha o ciclo.

Todos os métodos devolvem estruturas auditáveis (nunca "inventam" que
pesquisaram): `{"ok", "engine", "results", "blocked", "reason"}`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.clock import utcnow_iso
from ..core.logger import get_logger
from ..core.security_guard import prompt_shield

log = get_logger("squads.research")


def _strip_accents(text: str) -> str:
    """Remove diacríticos: ``gestão`` -> ``gestao`` (URLs e APIs não gostam)."""
    normalised = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in normalised if not unicodedata.combining(ch))


USER_AGENT = settings.scout_user_agent


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class ResearchPage:
    """Uma página recolhida, com o texto já passado pelo escudo."""

    url: str
    title: str = ""
    markdown: str = ""
    source: str = ""
    blocked: bool = False
    block_reason: str = ""
    chars: int = 0
    fetched_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "markdown": self.markdown,
            "source": self.source,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "chars": self.chars,
            "fetched_at": self.fetched_at,
        }


def _sanitize(text: str, source: str) -> tuple[str, bool, str]:
    """Passa o texto pelo escudo anti-injection e trunca. Devolve (texto, bloqueado, motivo)."""
    limit = settings.max_untrusted_content_chars
    clipped = (text or "")[:limit]
    try:
        verdict = prompt_shield.scan(clipped, source=source)
    except Exception as exc:  # o escudo nunca deve derrubar a recolha
        log.warning("Escudo falhou (%s); a truncar conteúdo.", exc)
        return clipped, False, f"shield_error: {exc}"
    if getattr(verdict, "blocked", False):
        reasons = "; ".join(getattr(verdict, "reasons", []) or [])[:200]
        return "", True, reasons or "conteúdo bloqueado"
    return clipped, False, ""


class ResearchBackends:
    """Fachada única para Crawl4AI / GPT-Researcher / extrator nativo."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------- capacidade
    def availability(self) -> Dict[str, Any]:
        crawl4ai = _module_available("crawl4ai")
        researcher = _module_available("gpt_researcher")
        return {
            "crawl4ai": {
                "installed": crawl4ai,
                "enabled": bool(settings.crawl4ai_enabled),
                "license": "apache-2.0",
                "ready": crawl4ai and bool(settings.crawl4ai_enabled),
            },
            "gpt_researcher": {
                "installed": researcher,
                "enabled": bool(settings.gpt_researcher_enabled),
                "license": "apache-2.0",
                "ready": researcher and bool(settings.gpt_researcher_enabled),
            },
            "native": {"installed": True, "enabled": True, "license": "own",
                       "ready": True},
            "active": self._active_engine(),
        }

    def _active_engine(self) -> str:
        if settings.gpt_researcher_enabled and _module_available("gpt_researcher"):
            return "gpt_researcher"
        if settings.crawl4ai_enabled and _module_available("crawl4ai"):
            return "crawl4ai"
        return "native"

    # ------------------------------------------------------------------ crawl
    def fetch_page(self, url: str) -> ResearchPage:
        """Extrai markdown de uma página, preferindo Crawl4AI."""
        engine = self._active_engine()
        if engine == "crawl4ai":
            page = self._crawl4ai(url)
            if page is not None:
                return page
        return self._native(url)

    def _crawl4ai(self, url: str) -> Optional[ResearchPage]:  # pragma: no cover
        """Crawl4AI é pesado (Playwright); só corre quando activado com consciência."""
        try:
            import asyncio

            from crawl4ai import AsyncWebCrawler  # type: ignore

            async def run() -> Any:
                async with AsyncWebCrawler(verbose=False) as crawler:
                    return await crawler.arun(url=url)

            result = asyncio.run(run())
            markdown = str(getattr(result, "markdown", "") or "")
            metadata = getattr(result, "metadata", {}) or {}
            text, blocked, reason = _sanitize(markdown, source=url)
            return ResearchPage(
                url=url,
                title=str(metadata.get("title", ""))[:200],
                markdown=text,
                source="crawl4ai",
                blocked=blocked,
                block_reason=reason,
                chars=len(text),
            )
        except Exception as exc:
            log.warning("Crawl4AI falhou em %s: %s", url, exc)
            return None

    @staticmethod
    def _render_api(payload: Any) -> str:
        """Converte a resposta de uma API de busca em linhas markdown.

        Cobre as três formas que usamos — ``items`` (GitHub Issues),
        ``hits``/``items`` (Algolia) e ``objects`` (npm) — e ordena por
        relevância para que o sinal mais forte apareça primeiro.
        """
        rows: List[tuple] = []

        def add(score: float, line: str, **signal: int) -> None:
            """Guarda a linha com os sinais numéricos em forma parseável.

            O Scout não adivinha números a partir de prosa: cada linha termina
            com ``sinal: stars=.. comments=.. downloads=..`` — um contrato
            estável entre a pesquisa e a pontuação de demanda.
            """
            if not line:
                return
            tail = " ".join(
                f"{key}={int(value or 0)}" for key, value in signal.items()
            )
            rows.append((float(score or 0), f"{line} — sinal: {tail}"))

        if isinstance(payload, dict):
            for hit in (payload.get("hits") or [])[:10]:
                if isinstance(hit, dict):
                    add(
                        hit.get("points") or hit.get("num_comments") or 0,
                        f"- {hit.get('title') or hit.get('story_title') or ''} "
                        f"— {hit.get('points', 0)} pontos, "
                        f"{hit.get('num_comments', 0)} comentários — "
                        f"{hit.get('url') or ''}",
                        stars=0,
                        points=int(hit.get("points") or 0),
                        comments=int(hit.get("num_comments") or 0),
                        downloads=0,
                    )
            for item in (payload.get("items") or [])[:10]:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("name") or ""
                if not title:
                    continue
                # GitHub Issues: reacções + comentários medem a dor real.
                reactions = item.get("reactions") or {}
                heat = (
                    (reactions.get("total_count") if isinstance(reactions, dict) else 0)
                    or item.get("comments") or 0
                )
                add(
                    heat or item.get("stargazers_count") or 0,
                    f"- {title} — {heat} reacções/comentários, "
                    f"estado: {item.get('state', 'n/d')} — "
                    f"{item.get('html_url') or ''}",
                    stars=int(item.get("stargazers_count") or 0),
                    points=int(heat or 0),
                    comments=int(item.get("comments") or 0),
                    downloads=0,
                )
            for obj in (payload.get("objects") or [])[:10]:
                if not isinstance(obj, dict):
                    continue
                package = obj.get("package") or {}
                downloads = obj.get("downloads") or {}
                weekly = downloads.get("weekly") if isinstance(downloads, dict) else 0
                raw_score = obj.get("score")
                score = (
                    float(raw_score.get("final", 0))
                    if isinstance(raw_score, dict)
                    else float(raw_score or 0)
                )
                weekly_value = (
                    int(downloads.get("weekly") or 0)
                    if isinstance(downloads, dict) else 0
                )
                add(
                    score * (weekly_value or 1) / 1000,
                    f"- npm: {package.get('name', '')} — "
                    f"{weekly_value} downloads/semana — "
                    f"{package.get('description', '')[:120]}",
                    stars=0,
                    points=0,
                    comments=0,
                    downloads=weekly_value,
                )
        rows.sort(key=lambda pair: pair[0], reverse=True)
        return "\n".join(line for _score, line in rows)

    def _native(self, url: str) -> ResearchPage:
        # Endpoints de API exigem Accept JSON; páginas exigem HTML. Escolhemos
        # pelo URL para não apanhar 415 do GitHub.
        is_api = any(
            marker in url for marker in (
                "api.github.com", "hn.algolia.com", "/api/",
                "raw.githubusercontent", "registry.npmjs.org/-/v1", "api.",
            )
        )
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json" if is_api else "text/html,application/xhtml+xml",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(400_000).decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.info("Pesquisa: falha de rede em %s (%s)", url, exc)
            return ResearchPage(url=url, source="native",
                                block_reason=f"network: {exc}")

        if is_api:
            # JSON -> markdown legível (mesmo pipeline de escudo).
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                rendered = self._render_api(payload)
                clean, blocked, reason = _sanitize(rendered, source=url)
                return ResearchPage(
                    url=url,
                    title=f"API {url.split('/')[2]}",
                    markdown=clean,
                    source="native_api",
                    blocked=blocked,
                    block_reason=reason,
                    chars=len(clean),
                )

        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
        text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        clean, blocked, reason = _sanitize(text, source=url)
        return ResearchPage(
            url=url, title=title[:200], markdown=clean, source="native",
            blocked=blocked, block_reason=reason, chars=len(clean),
        )

    # -------------------------------------------------------------- researcher
    def deep_research(self, query: str, max_results: Optional[int] = None) -> Dict[str, Any]:
        """Pesquisa multi-fonte. GPT-Researcher se instalado; senão, nativo."""
        if not (query or "").strip():
            return {
                "ok": False,
                "engine": self._active_engine(),
                "query": query,
                "results": [],
                "blocked": 0,
                "reason": "consulta vazia",
                "generated_at": utcnow_iso(),
            }
        limit = int(max_results or settings.research_max_results)
        engine = self._active_engine()
        if engine == "gpt_researcher":
            result = self._gpt_researcher(query)  # pragma: no cover
            if result is not None:
                result["max_results"] = limit
                return result

        # ---- nativo: consulta as fontes configuradas do Scout -------------
        pages: List[Dict[str, Any]] = []
        blocked = 0
        for url in self._candidate_urls(query, limit):
            page = self.fetch_page(url)
            if page.blocked:
                blocked += 1
                continue
            if page.markdown:
                pages.append(page.to_dict())
        return {
            "ok": bool(pages),
            "engine": "native",
            "query": query,
            "results": pages,
            "blocked": blocked,
            "reason": "" if pages else "sem páginas legíveis (rede ou bloqueio)",
            "generated_at": utcnow_iso(),
        }

    def _gpt_researcher(self, query: str) -> Optional[Dict[str, Any]]:  # pragma: no cover
        try:
            import asyncio

            from gpt_researcher import GPTResearcher  # type: ignore

            async def run() -> Any:
                researcher = GPTResearcher(query=query, report_type="research_report")
                await researcher.conduct_research()
                return await researcher.write_report()

            report = str(asyncio.run(run()))
            text, blocked, reason = _sanitize(report, source="gpt_researcher")
            return {
                "ok": bool(text),
                "engine": "gpt_researcher",
                "query": query,
                "results": [{
                    "url": "",
                    "title": f"Relatório: {query[:80]}",
                    "markdown": text,
                    "source": "gpt_researcher",
                    "blocked": blocked,
                    "block_reason": reason,
                    "chars": len(text),
                }],
                "blocked": 1 if blocked else 0,
                "reason": reason,
                "generated_at": utcnow_iso(),
            }
        except Exception as exc:
            log.warning("GPT-Researcher falhou (%s); a usar pesquisa nativa.", exc)
            return None

    @staticmethod
    def _query_terms(query: str, keep: int = 3) -> List[str]:
        """Termos de pesquisa, sem *stopwords* nem acentos.

        APIs de busca são literais: ``gestão de inventário para mercearias``
        não devolve nada, enquanto ``inventario mercearias`` devolve dezenas de
        pedidos reais. Ficamos com os termos mais longos (os mais específicos).
        """
        stop = {
            # gramática (pt/en)
            "de", "da", "do", "das", "dos", "para", "com", "sem", "que", "uma",
            "um", "os", "as", "o", "a", "e", "em", "no", "na", "por", "como",
            "the", "for", "and", "with", "small", "micro", "saas", "app",
            # termos genéricos: casam com tudo e enchem o feed de ruído
            # ("business", "management" aparecem em milhares de issues de
            # qualquer tema — não discriminam nicho nenhum).
            "business", "management", "software", "service", "services",
            "system", "systems", "platform", "tool", "tools", "solution",
            "gestao", "sistema", "sistemas", "servico", "servicos",
            "ferramenta", "ferramentas", "solucao", "empresa", "empresas",
        }
        words = [
            w for w in _strip_accents(query or "").lower().split()
            if len(w) > 2 and w not in stop
        ]
        if not words:
            words = ["inventory", "stock"]
        words.sort(key=len, reverse=True)
        return words[:keep]

    @classmethod
    def _candidate_urls(cls, query: str, limit: int) -> List[str]:
        """URLs de partida **independentes de um motor de busca pago**.

        Fontes, por ordem de utilidade como sinal de mercado, todas com ToS
        que permitem leitura programática e sem chave:

        1. **GitHub Issues** — pedidos reais de funcionalidades: o sinal mais
           forte de "alguém já precisa disto".
        2. **GitHub Repositories** — concorrência e maturidade do nicho.
        3. **npm Registry** — adopção e downloads semanais (só termos ASCII).

        O Hacker News (Algolia) esteve aqui e saiu: falha de forma sistemática
        em redes que bloqueiam o domínio, e uma fonte que não responde não pode
        ser a espinha dorsal da pesquisa.
        """
        terms = cls._query_terms(query)
        if not terms:
            terms = ["inventory", "stock"]
        # GitHub Search faz AND entre os termos soltos. Com 3 termos longos o
        # resultado é quase sempre vazio, por isso começamos por 2 e degradamos
        # para 1 — sempre com resultados reais.
        primary = " ".join(terms[:2])
        fallback = terms[0]
        enc_primary = urllib.parse.quote_plus(primary)
        enc_fallback = urllib.parse.quote_plus(fallback)

        urls = [
            "https://api.github.com/search/issues?q="
            + enc_primary + "+type:issue&sort=relevance&order=desc&per_page=5",
            "https://api.github.com/search/repositories?q="
            + enc_primary + "&sort=stars&order=desc&per_page=5",
            # npm: índice público de downloads, aceita qualquer termo.
            "https://registry.npmjs.org/-/v1/search?text=" + enc_fallback + "&size=5",
        ]
        # Nota: um 4.º URL com ``sort=reactions`` (termo único) foi testado e
        # **rejeitado** — o GitHub devolve issues ultra-votadas mas de temas
        # completamente alheios (ex.: uma issue do `openai/codex` para uma
        # consulta sobre inventário). Fonte com 0% de precisão não entra.
        return urls[: max(1, limit)]

    # ------------------------------------------------------------------ helper
    def summarize(self, pages: List[Dict[str, Any]], max_chars: int = 1200) -> str:
        """Resumo compacto e seguro para injectar num prompt de agente.

        Aceita ``markdown`` (Crawl4AI) ou ``text`` (extractor nativo) e volta a
        passar o conteúdo pelo escudo: um resumo é exactamente o ponto onde
        texto não confiável entra num prompt, pelo que aqui a filtragem não
        pode ser assumida como já feita a montante.
        """
        chunks: List[str] = []
        for page in pages:
            raw = page.get("markdown") or page.get("text") or page.get("content") or ""
            text = str(raw).strip()
            if not text:
                continue
            clean, blocked, why = _sanitize(text, "research_summary")
            if blocked or not clean.strip():
                log.warning("Resumo: página bloqueada pelo escudo (%s).", why)
                continue
            heading = page.get("title") or page.get("url") or "fonte"
            chunks.append(f"### {heading}\n{clean[:max_chars]}")
        return "\n\n".join(chunks)[:max_chars]


research_backends = ResearchBackends()
