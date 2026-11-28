"""Roteador de LLMs com custo zero e respeito estrito dos limites gratuitos.

Cadeia de fallback (configurável por ``LLM_PROVIDER_ORDER``):
``groq`` → ``gemini`` → ``openrouter`` → ``ollama`` → ``local``.

Princípios:
  * **Nunca inventa resultados.** Se nenhum provider estiver configurado, o
    ``LocalHeuristicEngine`` produz uma resposta determinística e *marcada*
    como tal (``is_llm=False``) — os consumidores sabem que não houve LLM.
  * **Rate limiting antes da chamada**, com janela deslizante de 60s (RPM) e
    contadores diários persistidos (RPD/TPD). Um provider sem quota é saltado
    sem sequer fazer o pedido HTTP.
  * **Custo contabilizado**: os tiers gratuitos custam $0.00, mas tokens e
    latência são sempre registados para alimentar o CAC e o Langfuse.
  * **Conteúdo não confiável** (web) é embrulhado pelo ``PromptShield`` antes
    de entrar no prompt — anti prompt-injection.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from ..config.settings import settings
from .clock import today_utc_str, utcnow_iso
from .errors import ConfigurationError, ExternalServiceError
from .logger import get_logger
from .security_guard import prompt_shield

log = get_logger("core.llm_router")

USAGE_FILE = "llm_usage.json"
HTTP_TIMEOUT_SECONDS = 90


# ==========================================================================
# Utilidades de tokens
# ==========================================================================
def estimate_tokens(text: str) -> int:
    """Estimativa conservadora: ~4 caracteres por token, mínimo 1."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_texts(cls, prompt: str, completion: str) -> "TokenUsage":
        p = estimate_tokens(prompt)
        c = estimate_tokens(completion)
        return cls(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)

    def to_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


# ==========================================================================
# Rate limiter persistido
# ==========================================================================
class RateLimiter:
    """Janela deslizante RPM + contadores diários RPD/TPD, por provider."""

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._state_path = state_path or (Path(settings.vault_path) / "runtime" / USAGE_FILE)
        self._windows: Dict[str, Deque[float]] = {}
        self._daily: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------- persistência
    def _reset_day_if_needed(self, provider: str, day: str) -> None:
        entry = self._daily.setdefault(provider, {})
        if entry.get("date") != day:
            entry.clear()
            entry["date"] = day
            entry["requests"] = 0
            entry["tokens"] = 0
            entry["cost_usd"] = 0.0

    def _load(self) -> None:
        try:
            if not self._state_path.is_file():
                return
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            daily = raw.get("daily", {})
            if isinstance(daily, dict):
                self._daily = {
                    str(k): dict(v) for k, v in daily.items() if isinstance(v, dict)
                }
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Histórico de uso de LLM ilegível (%s); a reiniciar.", exc)
            self._daily = {}

    def _persist(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": utcnow_iso(),
                "daily": self._daily,
            }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            import os as _os

            _os.replace(tmp, self._state_path)
        except OSError as exc:
            log.debug("Rate limiter sem persistência: %s", exc)

    # ------------------------------------------------------------------ consulta
    def usage(self, provider: str) -> Dict[str, Any]:
        day = today_utc_str()
        with self._lock:
            self._reset_day_if_needed(provider, day)
            window = self._windows.get(provider, deque())
            now = time.monotonic()
            while window and now - window[0] > 60.0:
                window.popleft()
            entry = self._daily[provider]
            return {
                "date": day,
                "requests_today": int(entry.get("requests", 0)),
                "tokens_today": int(entry.get("tokens", 0)),
                "cost_usd_today": float(entry.get("cost_usd", 0.0)),
                "requests_last_minute": len(window),
            }

    def can_call(
        self,
        provider: str,
        *,
        rpm_limit: int,
        rpd_limit: int,
        tpd_limit: int,
        estimated_tokens: int = 0,
    ) -> Tuple[bool, str]:
        day = today_utc_str()
        with self._lock:
            self._reset_day_if_needed(provider, day)
            window = self._windows.setdefault(provider, deque())
            now = time.monotonic()
            while window and now - window[0] > 60.0:
                window.popleft()
            entry = self._daily[provider]

            if rpm_limit > 0 and len(window) >= rpm_limit:
                wait = max(0.0, 60.0 - (now - window[0]))
                return False, f"rpm_excedido (aguardar {wait:.0f}s)"
            if rpd_limit > 0 and int(entry.get("requests", 0)) >= rpd_limit:
                return False, "rpd_excedido"
            if tpd_limit > 0 and int(entry.get("tokens", 0)) + estimated_tokens > tpd_limit:
                return False, "tpd_excedido"
            return True, "ok"

    def record(
        self,
        provider: str,
        tokens: int,
        cost_usd: float = 0.0,
    ) -> None:
        day = today_utc_str()
        with self._lock:
            self._reset_day_if_needed(provider, day)
            window = self._windows.setdefault(provider, deque())
            window.append(time.monotonic())
            entry = self._daily[provider]
            entry["requests"] = int(entry.get("requests", 0)) + 1
            entry["tokens"] = int(entry.get("tokens", 0)) + max(0, int(tokens))
            entry["cost_usd"] = round(float(entry.get("cost_usd", 0.0)) + float(cost_usd), 6)
            self._persist()

    def headroom(
        self,
        provider: str,
        *,
        rpm_limit: int,
        rpd_limit: int,
        tpd_limit: int,
    ) -> float:
        """Fracção já consumida do limite mais apertado (0.0 a 1.0).

        Serve o failover preventivo: a 85% do limite o provedor é abandonado
        *antes* de bater no 429, para o ciclo nunca parar a meio.
        """
        usage = self.usage(provider)
        ratios = [0.0]
        if rpm_limit > 0:
            ratios.append(float(usage.get("requests_last_minute", 0)) / rpm_limit)
        if rpd_limit > 0:
            ratios.append(float(usage.get("requests_today", 0)) / rpd_limit)
        if tpd_limit > 0:
            ratios.append(float(usage.get("tokens_today", 0)) / tpd_limit)
        return min(1.0, max(ratios))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {provider: self.usage(provider) for provider in sorted(self._daily)}


# ==========================================================================
# Contratos de pedido/resposta
# ==========================================================================
@dataclass
class LLMRequest:
    prompt: str
    system: str = ""
    task: str = "general"
    max_tokens: int = 1024
    temperature: float = 0.4
    json_mode: bool = False
    json_schema: Optional[Dict[str, Any]] = None
    untrusted_content: str = ""
    untrusted_source: str = "web"
    agent_id: str = "system"
    context: Dict[str, Any] = field(default_factory=dict)

    def build_prompt(self) -> Tuple[str, str]:
        """Compõe o prompt final aplicando o escudo a conteúdo externo."""
        prompt = self.prompt
        if self.untrusted_content:
            wrapped = prompt_shield.wrap_untrusted(
                self.untrusted_content, source=self.untrusted_source
            )
            prompt = f"{prompt}\n\n{wrapped}"
        if self.json_mode and "json" not in prompt.lower():
            prompt = f"{prompt}\n\nResponde APENAS com um objecto JSON válido."
        return prompt, self.system


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    usage: TokenUsage
    cost_usd: float = 0.0
    latency_ms: int = 0
    is_llm: bool = True
    fallback_chain: List[str] = field(default_factory=list)
    blocked: bool = False
    error: Optional[str] = None
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": self.latency_ms,
            "is_llm": self.is_llm,
            "fallback_chain": list(self.fallback_chain),
            "blocked": self.blocked,
            "error": self.error,
            "created_at": self.created_at,
        }


# ==========================================================================
# Motor heurístico local (determinístico, sem LLM)
# ==========================================================================
_STOPWORDS = {
    "a", "o", "e", "de", "da", "do", "das", "dos", "em", "no", "na", "para", "por",
    "com", "sem", "que", "um", "uma", "uns", "umas", "os", "as", "the", "and", "or",
    "of", "to", "in", "on", "for", "with", "is", "are", "was", "be", "it", "this",
    "that", "as", "ao", "à", "se", "mais", "como", "mas", "já", "não", "sim", "muito",
}


class LocalHeuristicEngine:
    """Produz respostas úteis e *honestamente marcadas* sem qualquer LLM.

    Usado quando não há chaves configuradas ou todas as quotas estão esgotadas.
    A saída é sempre determinística para o mesmo input, o que a torna testável
    e auditável. Não substitui um LLM: ``is_llm`` vem sempre a ``False``.
    """

    name = "local_heuristic"

    def keywords(self, text: str, limit: int = 12) -> List[str]:
        tokens = re.findall(r"[a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ0-9\-]{2,}", (text or "").lower())
        freq: Dict[str, int] = {}
        for token in tokens:
            if token in _STOPWORDS:
                continue
            freq[token] = freq.get(token, 0) + 1
        ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
        return [word for word, _ in ranked[:limit]]

    def summary(self, text: str, max_chars: int = 480) -> str:
        clean = re.sub(r"\s+", " ", (text or "").strip())
        if not clean:
            return "Sem conteúdo para resumir."
        words = clean.split(" ")
        key = self.keywords(clean, limit=6)
        head = " ".join(words[:60])
        if len(head) > max_chars:
            head = head[:max_chars].rsplit(" ", 1)[0] + "…"
        return f"{head} [termos-chave: {', '.join(key) or 'n/d'}]"

    def fill_schema(self, prompt: str, schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Preenche um esquema JSON com valores derivados do prompt."""
        keys: List[str] = []
        if isinstance(schema, dict):
            props = schema.get("properties")
            if isinstance(props, dict):
                keys = list(props.keys())
            elif isinstance(schema.get("keys"), list):
                keys = [str(k) for k in schema["keys"]]
        if not keys:
            keys = ["summary", "keywords", "next_action"]

        words = self.keywords(prompt, limit=8)
        base: Dict[str, Any] = {}
        for key in keys:
            lowered = key.lower()
            if lowered in {"keywords", "tags", "terms", "palavras_chave"}:
                base[key] = words
            elif lowered in {"score", "rating", "nota", "confidence", "risk"}:
                base[key] = 50
            elif lowered.startswith("is_") or lowered.startswith("has_"):
                base[key] = False
            elif any(t in lowered for t in ("count", "number", "qty", "num_")):
                base[key] = 0
            elif any(t in lowered for t in ("price", "cost", "usd", "revenue", "amount")):
                base[key] = 0.0
            elif any(t in lowered for t in ("list", "steps", "items", "actions", "risks")):
                base[key] = []
            elif any(t in lowered for t in ("summary", "description", "text", "rationale")):
                base[key] = self.summary(prompt)
            else:
                base[key] = self.summary(prompt, max_chars=240)
        base["_engine"] = "local_heuristic"
        base["_note"] = (
            "Resposta determinística do motor local (sem LLM). "
            "Configura GROQ_API_KEY/GEMINI_API_KEY para raciocínio real."
        )
        return base

    def generate(self, prompt: str, task: str) -> str:
        keywords = ", ".join(self.keywords(prompt, limit=8)) or "n/d"
        return (
            f"[MOTOR LOCAL — SEM LLM] Tarefa: {task}.\n"
            f"Resumo: {self.summary(prompt)}\n"
            f"Termos-chave: {keywords}\n"
            "Recomendação: registar como pendente de revisão humana; "
            "não produzir activos até haver validação de demanda."
        )


# ==========================================================================
# Providers HTTP
# ==========================================================================
def _http_post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """POST JSON com urllib (sem dependências), com retry simples."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)

    last_error: Optional[BaseException] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                last_error = exc
                continue
            raise ExternalServiceError(
                f"HTTP {exc.code} em {url.split('?')[0]}: {detail or exc.reason}",
                details={"status": exc.code, "url": url.split("?")[0]},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < 2:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                last_error = exc
                continue
            raise ExternalServiceError(
                f"Falha de rede em {url.split('?')[0]}: {exc}",
                details={"url": url.split("?")[0]},
            ) from exc
    raise ExternalServiceError(f"Falha após 3 tentativas: {last_error}")


@dataclass
class ProviderSpec:
    name: str
    rpm_limit: int
    rpd_limit: int
    tpd_limit: int
    model: str
    configured: bool


class LLMRouter:
    """Ponto único de acesso a modelos de linguagem em toda a holding."""

    def __init__(self) -> None:
        self.limiter = RateLimiter()
        self.local_engine = LocalHeuristicEngine()
        self._lock = threading.RLock()
        self._last_response: Optional[LLMResponse] = None
        self._trace_hook: Optional[Any] = None

    # ------------------------------------------------------------------ traces
    def set_trace_hook(self, hook: Optional[Any]) -> None:
        """Permite que ``observability_langfuse`` escute cada chamada."""
        self._trace_hook = hook

    # ------------------------------------------------------------------ specs
    def provider_specs(self) -> List[ProviderSpec]:
        return [
            ProviderSpec(
                name="groq",
                rpm_limit=settings.groq_rpm_limit,
                rpd_limit=settings.groq_rpd_limit,
                tpd_limit=settings.groq_tpd_limit,
                model=settings.groq_model_reasoning,
                configured=bool(settings.groq_api_key),
            ),
            ProviderSpec(
                name="gemini",
                rpm_limit=settings.gemini_rpm_limit,
                rpd_limit=settings.gemini_rpd_limit,
                tpd_limit=settings.gemini_tpd_limit,
                model=settings.gemini_model,
                configured=bool(settings.gemini_api_key),
            ),
            ProviderSpec(
                name="github_models",
                rpm_limit=settings.github_models_rpm_limit,
                rpd_limit=settings.github_models_rpd_limit,
                tpd_limit=settings.github_models_tpd_limit,
                model=settings.github_models_model,
                configured=bool(
                    settings.github_models_enabled and settings.github_models_token
                ),
            ),
            ProviderSpec(
                name="openrouter",
                rpm_limit=settings.openrouter_rpm_limit,
                rpd_limit=settings.openrouter_rpd_limit,
                tpd_limit=0,
                model=settings.openrouter_model,
                configured=bool(settings.openrouter_api_key),
            ),
            ProviderSpec(
                name="ollama",
                rpm_limit=0,
                rpd_limit=0,
                tpd_limit=0,
                model=settings.ollama_model,
                configured=bool(settings.ollama_enabled),
            ),
        ]

    def status(self) -> Dict[str, Any]:
        specs = {spec.name: spec for spec in self.provider_specs()}
        order = [p for p in settings.llm_provider_order]
        result: Dict[str, Any] = {"order": order, "providers": {}, "last_response": None}
        for name, spec in specs.items():
            can, reason = self.limiter.can_call(
                name,
                rpm_limit=spec.rpm_limit,
                rpd_limit=spec.rpd_limit,
                tpd_limit=spec.tpd_limit,
            )
            result["providers"][name] = {
                "configured": spec.configured,
                "model": spec.model,
                "limits": {
                    "rpm": spec.rpm_limit,
                    "rpd": spec.rpd_limit,
                    "tpd": spec.tpd_limit,
                },
                "usage": self.limiter.usage(name),
                "available": bool(spec.configured and can),
                "reason": "pronto" if (spec.configured and can) else (
                    reason if spec.configured else "sem_chave"
                ),
            }
        result["providers"]["local"] = {
            "configured": True,
            "model": "heuristic-v1",
            "limits": {"rpm": 0, "rpd": 0, "tpd": 0},
            "usage": self.limiter.usage("local"),
            "available": True,
            "reason": "sempre_disponivel",
        }
        if self._last_response is not None:
            result["last_response"] = self._last_response.to_dict()
        return result

    # ------------------------------------------------------------------ chamadas
    def _call_groq(self, request: LLMRequest, prompt: str, system: str) -> LLMResponse:
        model = (
            settings.groq_model_reasoning
            if request.task in {"reasoning", "strategy", "code", "analysis", "judge"}
            else settings.groq_model_fast
        )
        if request.json_mode:
            model = settings.groq_model_reasoning
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": max(64, request.max_tokens),
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        raw = _http_post_json(
            "https://api.groq.com/openai/v1/chat/completions",
            payload,
            {"Authorization": f"Bearer {settings.groq_api_key}"},
        )
        latency = int((time.monotonic() - started) * 1000)
        try:
            text = raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError(f"Resposta Groq inesperada: {raw}") from exc
        usage_raw = raw.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", estimate_tokens(prompt))),
            completion_tokens=int(
                usage_raw.get("completion_tokens", estimate_tokens(text))
            ),
            total_tokens=0,
        )
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return LLMResponse(
            text=text, provider="groq", model=model, usage=usage,
            cost_usd=0.0, latency_ms=latency, is_llm=True,
        )

    def _call_gemini(self, request: LLMRequest, prompt: str, system: str) -> LLMResponse:
        model = settings.gemini_model
        contents: List[Dict[str, Any]] = [{"role": "user", "parts": [{"text": prompt}]}]
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": max(64, request.max_tokens),
            },
        }
        if request.json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model)}:generateContent?key="
            f"{urllib.parse.quote(settings.gemini_api_key)}"
        )
        started = time.monotonic()
        raw = _http_post_json(url, payload, {})
        latency = int((time.monotonic() - started) * 1000)
        try:
            candidates = raw.get("candidates") or []
            parts = candidates[0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError(f"Resposta Gemini inesperada: {raw}") from exc
        meta = raw.get("usageMetadata") or {}
        usage = TokenUsage(
            prompt_tokens=int(meta.get("promptTokenCount", estimate_tokens(prompt))),
            completion_tokens=int(
                meta.get("candidatesTokenCount", estimate_tokens(text))
            ),
            total_tokens=0,
        )
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return LLMResponse(
            text=text, provider="gemini", model=model, usage=usage,
            cost_usd=0.0, latency_ms=latency, is_llm=True,
        )

    #: Endpoints do GitHub Models, por ordem de tentativa.
    #:
    #: Medido no runner do GitHub (sonda_rede.txt / run 46131623640):
    #:   models.github.ai                      DNS ok, POST 200 (com e sem token)
    #:   models.inference.ai.azure.com         **NÃO RESOLVE** (Name or service
    #:                                         not known) — o endpoint original
    #:                                         está desligado, não só descontinuado.
    #:
    #: O primeiro tem de ser o que funciona. Antes desta correcção o endpoint
    #: morto vinha primeiro e cada chamada pagava o timeout dele antes de
    #: tentar o bom — na prática o provider nunca era usado.
    GITHUB_MODELS_ENDPOINTS = (
        "https://models.github.ai/inference/chat/completions",
        "https://models.inference.ai.azure.com/chat/completions",
    )

    #: Timeout curto para o GitHub Models. O router usa 30 s por omissão, mas
    #: um endpoint morto não deve custar 30 s antes de se tentar o seguinte.
    GITHUB_MODELS_TIMEOUT = 20

    def _call_github_models(
        self, request: LLMRequest, prompt: str, system: str
    ) -> LLMResponse:
        model = settings.github_models_model
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": max(64, request.max_tokens),
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {settings.github_models_token}",
            "Accept": "application/json",
        }
        started = time.monotonic()
        raw: Optional[Dict[str, Any]] = None
        ultimo_erro: Optional[Exception] = None
        for url in self.GITHUB_MODELS_ENDPOINTS:
            try:
                # Timeout curto: um endpoint morto não pode custar 30 s antes
                # de se tentar o seguinte (com retry de 3x, seriam 90 s).
                raw = _http_post_json(
                    url, payload, headers, timeout=self.GITHUB_MODELS_TIMEOUT
                )
                break
            except Exception as exc:  # 404 de endpoint descontinuado, 401, 429…
                ultimo_erro = exc
                log.info("GitHub Models: %s falhou (%s); a tentar o seguinte", url, exc)
        if raw is None:
            raise ExternalServiceError(
                f"GitHub Models indisponível em {len(self.GITHUB_MODELS_ENDPOINTS)} "
                f"endpoints: {ultimo_erro}"
            )
        latency = int((time.monotonic() - started) * 1000)

        try:
            escolha = (raw.get("choices") or [])[0]
            text = escolha["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError(
                f"Resposta GitHub Models inesperada: {str(raw)[:300]}"
            ) from exc
        if not text.strip():
            raise ExternalServiceError("GitHub Models devolveu resposta vazia")

        meta = raw.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(meta.get("prompt_tokens", estimate_tokens(prompt))),
            completion_tokens=int(
                meta.get("completion_tokens", estimate_tokens(text))
            ),
            total_tokens=0,
        )
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return LLMResponse(
            text=text, provider="github_models", model=model, usage=usage,
            cost_usd=0.0, latency_ms=latency, is_llm=True,
        )

    def _call_openrouter(self, request: LLMRequest, prompt: str, system: str) -> LLMResponse:
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: Dict[str, Any] = {
            "model": settings.openrouter_model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": max(64, request.max_tokens),
        }
        started = time.monotonic()
        raw = _http_post_json(
            "https://openrouter.ai/api/v1/chat/completions",
            payload,
            {
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "HTTP-Referer": "https://github.com/automaton-holding",
                "X-Title": "Automaton Holding Corp",
            },
        )
        latency = int((time.monotonic() - started) * 1000)
        try:
            text = raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError(f"Resposta OpenRouter inesperada: {raw}") from exc
        usage_raw = raw.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", estimate_tokens(prompt))),
            completion_tokens=int(
                usage_raw.get("completion_tokens", estimate_tokens(text))
            ),
            total_tokens=0,
        )
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return LLMResponse(
            text=text, provider="openrouter", model=settings.openrouter_model,
            usage=usage, cost_usd=0.0, latency_ms=latency, is_llm=True,
        )

    def _call_ollama(self, request: LLMRequest, prompt: str, system: str) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": settings.ollama_model,
            "messages": (
                ([{"role": "system", "content": system}] if system else [])
                + [{"role": "user", "content": prompt}]
            ),
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": max(64, request.max_tokens),
            },
        }
        if request.json_mode:
            payload["format"] = "json"
        url = settings.ollama_base_url.rstrip("/") + "/api/chat"
        started = time.monotonic()
        raw = _http_post_json(url, payload, {}, timeout=180)
        latency = int((time.monotonic() - started) * 1000)
        text = ""
        if isinstance(raw.get("message"), dict):
            text = raw["message"].get("content", "") or ""
        if not text:
            text = raw.get("response", "") or ""
        usage = TokenUsage(
            prompt_tokens=int(raw.get("prompt_eval_count", estimate_tokens(prompt))),
            completion_tokens=int(raw.get("eval_count", estimate_tokens(text))),
            total_tokens=0,
        )
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return LLMResponse(
            text=text, provider="ollama", model=settings.ollama_model,
            usage=usage, cost_usd=0.0, latency_ms=latency, is_llm=True,
        )

    # ------------------------------------------------------------------ público
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Executa a cadeia de fallback e devolve sempre uma ``LLMResponse``."""
        prompt, system = request.build_prompt()
        estimated = estimate_tokens(prompt) + max(32, request.max_tokens // 2)
        chain: List[str] = []
        errors: List[str] = []

        order = list(settings.llm_provider_order) or ["local"]
        if request.json_mode and "groq" in order:
            # Groq é o único com json_object garantido no tier gratuito.
            order.remove("groq")
            order.insert(0, "groq")

        for provider in order:
            spec = next((s for s in self.provider_specs() if s.name == provider), None)
            if provider == "local":
                continue
            if spec is None or not spec.configured:
                continue
            allowed, reason = self.limiter.can_call(
                provider,
                rpm_limit=spec.rpm_limit,
                rpd_limit=spec.rpd_limit,
                tpd_limit=spec.tpd_limit,
                estimated_tokens=estimated,
            )
            if not allowed:
                chain.append(f"{provider}:skip({reason})")
                log.info("Provider %s ignorado: %s", provider, reason)
                continue

            # Failover preventivo a 85% do limite: em vez de esperar o 429,
            # guarda o resto da quota para as tarefas críticas.
            used = self.limiter.headroom(
                provider,
                rpm_limit=spec.rpm_limit,
                rpd_limit=spec.rpd_limit,
                tpd_limit=spec.tpd_limit,
            )
            if used >= settings.llm_failover_threshold:
                chain.append(f"{provider}:skip(quota_{used:.0%})")
                log.info(
                    "Provider %s em %.0f%% da quota (limiar %.0f%%): a passar "
                    "para o seguinte.", provider, used * 100,
                    settings.llm_failover_threshold * 100,
                )
                continue

            chain.append(f"{provider}:try")
            try:
                if provider == "groq":
                    response = self._call_groq(request, prompt, system)
                elif provider == "gemini":
                    response = self._call_gemini(request, prompt, system)
                elif provider == "github_models":
                    response = self._call_github_models(request, prompt, system)
                elif provider == "openrouter":
                    response = self._call_openrouter(request, prompt, system)
                elif provider == "ollama":
                    response = self._call_ollama(request, prompt, system)
                else:
                    continue
            except (ExternalServiceError, ConfigurationError) as exc:
                errors.append(f"{provider}: {exc.message if hasattr(exc, 'message') else exc}")
                chain.append(f"{provider}:error")
                log.warning("Provider %s falhou: %s", provider, exc)
                continue
            except Exception as exc:  # nunca deixar o ciclo cair por um provider
                errors.append(f"{provider}: {exc}")
                chain.append(f"{provider}:unexpected_error")
                log.exception("Erro inesperado no provider %s", provider)
                continue

            self.limiter.record(provider, response.usage.total_tokens, response.cost_usd)
            response.fallback_chain = chain
            self._publish(response, request)
            return response

        # ------------------------------------------------------- fallback local
        chain.append("local:used")
        text = (
            self.local_engine.fill_schema(prompt, request.json_schema)
            if request.json_mode
            else self.local_engine.generate(prompt, request.task)
        )
        rendered = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        usage = TokenUsage.from_texts(prompt, rendered)
        self.limiter.record("local", usage.total_tokens, 0.0)
        response = LLMResponse(
            text=rendered,
            provider="local",
            model="heuristic-v1",
            usage=usage,
            cost_usd=0.0,
            latency_ms=0,
            is_llm=False,
            fallback_chain=chain,
            error="; ".join(errors) if errors else None,
        )
        self._publish(response, request)
        return response

    def _publish(self, response: LLMResponse, request: LLMRequest) -> None:
        with self._lock:
            self._last_response = response
        if callable(self._trace_hook):
            try:
                self._trace_hook(response, request)
            except Exception as exc:
                log.debug("Hook de tracing falhou: %s", exc)

    # -------------------------------------------------------------- JSON helper
    def complete_json(
        self,
        request: LLMRequest,
        *,
        required_keys: Optional[Iterable[str]] = None,
    ) -> Tuple[Dict[str, Any], LLMResponse]:
        """Chama o roteador forçando JSON e valida as chaves obrigatórias.

        Se o modelo devolver texto inválido, tenta extrair o primeiro bloco
        ``{...}``; se ainda assim falhar, cai no motor local determinístico.
        """
        request.json_mode = True
        if required_keys and not (request.json_schema or {}).get("properties"):
            # Sem esquema explícito, derivamos um mínimo a partir das chaves
            # exigidas para que o preenchimento de emergência saiba o formato.
            request.json_schema = {
                "type": "object",
                "properties": {
                    key: {"type": "string"} for key in required_keys
                },
            }
        response = self.complete(request)
        parsed = _extract_json_object(response.text)
        if parsed is None and response.is_llm:
            log.warning("Resposta não-JSON de %s; a repetir em modo estrito.", response.provider)
            strict = LLMRequest(
                prompt=request.prompt + "\n\nResponde SÓ com JSON, sem markdown.",
                system=request.system,
                task=request.task,
                max_tokens=request.max_tokens,
                temperature=0.1,
                json_mode=True,
                json_schema=request.json_schema,
                untrusted_content=request.untrusted_content,
                untrusted_source=request.untrusted_source,
                agent_id=request.agent_id,
            )
            retry = self.complete(strict)
            parsed = _extract_json_object(retry.text)
            if parsed is not None:
                response = retry

        if parsed is None:
            parsed = self.local_engine.fill_schema(request.prompt, request.json_schema)
            response = LLMResponse(
                text=json.dumps(parsed, ensure_ascii=False),
                provider="local",
                model="heuristic-v1",
                usage=TokenUsage.from_texts(request.prompt, json.dumps(parsed)),
                is_llm=False,
                fallback_chain=response.fallback_chain + ["local:json_fallback"],
                error=response.error or "json_invalido_do_provider",
            )
            self._publish(response, request)

        missing = [
            key for key in (required_keys or []) if key not in parsed
        ]
        if missing:
            log.warning("Chaves JSON em falta (%s); preenchidas pelo motor local.", missing)
            filler = self.local_engine.fill_schema(request.prompt, request.json_schema)
            for key in missing:
                parsed[key] = filler.get(key)
        return parsed, response

    def last_response(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._last_response.to_dict() if self._last_response else None


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extrai o primeiro objecto JSON de um texto possivelmente com markdown."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    for index, char in enumerate(candidate):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                fragment = candidate[start : index + 1]
                try:
                    data = json.loads(fragment)
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    start = -1
                    depth = 0
    return None


# Instância partilhada
llm_router = LLMRouter()

__all__ = [
    "LLMRouter",
    "llm_router",
    "LLMRequest",
    "LLMResponse",
    "TokenUsage",
    "RateLimiter",
    "LocalHeuristicEngine",
    "estimate_tokens",
]
