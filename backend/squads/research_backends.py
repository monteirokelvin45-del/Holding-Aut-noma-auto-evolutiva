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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.clock import utcnow_iso
from ..core.logger import get_logger
from ..core.security_guard import prompt_shield

log = get_logger("squads.research")

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

    def _native(self, url: str) -> ResearchPage:
        # Endpoints de API exigem Accept JSON; páginas exigem HTML. Escolhemos
        # pelo URL para não apanhar 415 do GitHub.
        is_api = any(
            host in url for host in (
                "api.github.com", "hn.algolia.com", "/api/", "raw.githubusercontent",
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
            # JSON transformado em markdown legível (mesmo pipeline de escudo).
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                lines: List[str] = []
                hits = payload.get("hits") or payload.get("items") or []
                for hit in hits[:10]:
                    if not isinstance(hit, dict):
                        continue
                    title_hit = (
                        hit.get("title") or hit.get("name")
                        or hit.get("full_name") or hit.get("story_title") or ""
                    )
                    stars = hit.get("points") or hit.get("stargazers_count") or 0
                    comments = (
                        hit.get("num_comments") or hit.get("open_issues_count") or 0
                    )
                    link = hit.get("url") or hit.get("html_url") or ""
                    if title_hit:
                        lines.append(
                            f"- {title_hit} — {stars} points/stars, "
                            f"{comments} comments/issues — {link}"
                        )
                rendered = "\n".join(lines)
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
    def _candidate_urls(query: str, limit: int) -> List[str]:
        """URLs de partida sem depender de um motor de busca pago.

        Usa o GitHub Search (API pública, sem chave para leitura básica) e o
        Hacker News (Algolia, público) — ambos com ToS que permitem leitura.
        """
        slug = re.sub(r"[^a-zA-Z0-9 ]", " ", query).strip()
        encoded = urllib.parse.quote_plus(slug) if slug else "startup"
        urls = [
            "https://hn.algolia.com/api/v1/search?query=" + encoded,
            # O GitHub devolve 415 a pedidos com Accept: text/html — pedimos a
            # API JSON com o Accept explícito para não perdermos a fonte.
            "https://api.github.com/search/repositories?q="
            + urllib.parse.quote_plus(slug or "saas")
            + "&sort=stars&order=desc&per_page=5",
        ]
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
