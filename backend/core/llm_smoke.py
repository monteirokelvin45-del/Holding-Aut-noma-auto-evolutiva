"""Diagnóstico real da integração com modelos de linguagem.

Responde, sem ambiguidade, à pergunta «as IAs estão ligadas?»:

* percorre os provedores configurados (Groq, Gemini, OpenRouter, Ollama);
* faz **uma chamada verdadeira** e curta a cada um (2-8 tokens) e mede latência;
* devolve, por provedor, ``status`` explícito:
  - ``ok``            — respondeu com texto do modelo;
  - ``sem_chave``     — não configurado (não é erro);
  - ``rate_limited``  — limite do tier gratuito atingido;
  - ``timeout``/``http_error``/``network_error`` — falha diagnosticada;
* nunca lança: um provedor em baixo não impede o resto do diagnóstico.

Custo: zero. As chaves usadas estão todas no tier gratuito e o pedido é
``max_tokens=8``. O resultado é sempre auditável (nunca «inventa» que está ok).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from .errors import ConfigurationError, ExternalServiceError
from .llm_router import LLMRequest, llm_router
from .logger import get_logger

log = get_logger("core.llm_smoke")

PROBE_PROMPT = "Responde apenas com a palavra: OK"


def _probe(provider: str) -> Dict[str, Any]:
    """Faz uma chamada mínima ao provedor e classifica o resultado."""
    started = time.monotonic()
    try:
        if provider == "groq":
            response = llm_router._call_groq(  # noqa: SLF001 - diagnóstico interno
                LLMRequest(prompt=PROBE_PROMPT, task="fast", max_tokens=8),
                PROBE_PROMPT,
                "",
            )
        elif provider == "gemini":
            response = llm_router._call_gemini(  # noqa: SLF001
                LLMRequest(prompt=PROBE_PROMPT, task="fast", max_tokens=8),
                PROBE_PROMPT,
                "",
            )
        elif provider == "openrouter":
            response = llm_router._call_openrouter(  # noqa: SLF001
                LLMRequest(prompt=PROBE_PROMPT, task="fast", max_tokens=8),
                PROBE_PROMPT,
                "",
            )
        elif provider == "ollama":
            response = llm_router._call_ollama(  # noqa: SLF001
                LLMRequest(prompt=PROBE_PROMPT, task="fast", max_tokens=8),
                PROBE_PROMPT,
                "",
            )
        else:
            return {
                "provider": provider,
                "status": "unknown_provider",
                "ok": False,
                "latency_ms": 0,
            }
    except ConfigurationError as exc:
        return {
            "provider": provider,
            "status": "sem_chave",
            "ok": False,
            "detail": exc.message,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    except ExternalServiceError as exc:
        message = exc.message or str(exc)
        lowered = message.lower()
        if "429" in message or "rate" in lowered:
            status = "rate_limited"
        elif "timeout" in lowered or "timed out" in lowered:
            status = "timeout"
        elif "401" in message or "403" in message:
            status = "chave_invalida"
        else:
            status = "http_error"
        return {
            "provider": provider,
            "status": status,
            "ok": False,
            "detail": message[:400],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # rede/DNS/TLS: nunca derrubar o diagnóstico
        return {
            "provider": provider,
            "status": "network_error",
            "ok": False,
            "detail": str(exc)[:400],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    text = (response.text or "").strip()
    return {
        "provider": provider,
        "status": "ok" if text else "resposta_vazia",
        "ok": bool(text),
        "model": response.model,
        "sample": text[:60],
        "latency_ms": response.latency_ms,
        "tokens": response.usage.to_dict(),
    }


def run_probe(providers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Diagnostica provedores. Sem chaves, devolve tudo como ``sem_chave``."""
    specs = {spec.name: spec for spec in llm_router.provider_specs()}
    targets = providers or list(specs)
    results: List[Dict[str, Any]] = []

    for name in targets:
        spec = specs.get(name)
        if spec is None:
            results.append({
                "provider": name, "status": "unknown_provider", "ok": False,
            })
            continue
        if not spec.configured:
            results.append({
                "provider": name,
                "status": "sem_chave",
                "ok": False,
                "model": spec.model,
                "detail": "Nenhuma chave configurada — provedor não testado.",
            })
            continue
        allowed, reason = llm_router.limiter.can_call(
            name,
            rpm_limit=spec.rpm_limit,
            rpd_limit=spec.rpd_limit,
            tpd_limit=spec.tpd_limit,
            estimated_tokens=32,
        )
        if not allowed:
            results.append({
                "provider": name,
                "status": "limite_atingido",
                "ok": False,
                "model": spec.model,
                "detail": reason,
            })
            continue
        probe = _probe(name)
        if probe.get("ok") and probe.get("tokens"):
            llm_router.limiter.record(
                name, int(probe["tokens"].get("total_tokens", 0)), 0.0
            )
        results.append(probe)

    reachable = [r["provider"] for r in results if r.get("ok")]
    configured = [
        name for name, spec in specs.items() if spec.configured
    ]
    if reachable:
        conclusion = (
            "IA ligada via " + ", ".join(reachable)
            + " (tier gratuito, custo $0.00)."
        )
    elif configured:
        conclusion = (
            "Chaves presentes ("
            + ", ".join(configured)
            + ") mas nenhum provedor respondeu — ver detalhes por provedor."
        )
    else:
        conclusion = (
            "Sem chaves de LLM: o sistema opera com o motor heurístico local "
            "determinístico (is_llm=false). Adiciona GROQ_API_KEY ao .env para "
            "activar modelos reais sem custo."
        )

    return {
        "reachable": reachable,
        "configured": configured,
        "results": results,
        "conclusion": conclusion,
        "budget": {
            "daily_spend_cap_usd": settings.daily_spend_cap_usd,
            "paid_models_enabled": settings.per_agent_daily_cap_usd > 0,
        },
        "local_engine_available": True,
    }


if __name__ == "__main__":  # pragma: no cover - utilidade de linha de comandos
    import json

    print(json.dumps(run_probe(), ensure_ascii=False, indent=2))
