"""Observabilidade: Langfuse + streaming Chain-of-Thought + bot Telegram.

Duas responsabilidades:

1. **Tracing** de cada chamada LLM (provider, modelo, tokens, latência, custo,
   prompt/resposta redigidos) para o Langfuse self-hosted. Se o pacote ou as
   chaves não existirem, usamos um *buffer local* em memória + ficheiro JSONL,
   pelo que o painel continua a ter dados.

2. **Telegram Bot** como fallback de supervisão humana: comandos ``/status``,
   ``/kill``, ``/release``, ``/pendentes``, ``/aprovar``, ``/vetar``,
   ``/tesouraria``, ``/esquadroes``. Implementado com ``urllib`` (sem
   dependências) e *long polling* opcional; whitelist de chat_ids obrigatória.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from ..config.settings import settings
from .clock import utcnow_iso
from .logger import get_logger
from .runtime_bus import event_bus, redact

log = get_logger("core.observability")

TRACE_FILE = "llm_traces.jsonl"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


# ==========================================================================
# 1. Tracer
# ==========================================================================
class LLMTracer:
    """Registra cada chamada LLM e expõe métricas agregadas por esquadrão/agente."""

    def __init__(self, max_memory: int = 1000) -> None:
        self._lock = threading.RLock()
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=max_memory)
        self._totals: Dict[str, Dict[str, float]] = {}
        self._langfuse_client: Optional[Any] = None
        self._langfuse_checked = False
        self._trace_path = Path(settings.vault_path) / "runtime" / TRACE_FILE
        self._cost_per_1k_tokens_usd = 0.0  # free tiers
        try:
            self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # -------------------------------------------------------- integração opcional
    def _client(self) -> Optional[Any]:
        if self._langfuse_checked:
            return self._langfuse_client
        self._langfuse_checked = True
        if not (settings.langfuse_enabled and settings.langfuse_public_key):
            log.info("Langfuse desactivado: tracing fica em buffer local.")
            return None
        try:
            from langfuse import Langfuse  # type: ignore

            self._langfuse_client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
            log.info("Langfuse conectado em %s", settings.langfuse_host)
        except Exception as exc:
            log.warning("Langfuse indisponível (%s); buffer local activo.", exc)
            self._langfuse_client = None
        return self._langfuse_client

    # ----------------------------------------------------------------- registo
    def record(self, response: Any, request: Any) -> None:
        """Hook compatível com ``LLMRouter.set_trace_hook``."""
        try:
            agent = getattr(request, "agent_id", "system")
            squad = getattr(request, "context", {}).get("squad", "core") if isinstance(
                getattr(request, "context", None), dict
            ) else "core"
            entry = {
                "ts": getattr(response, "created_at", utcnow_iso()),
                "agent_id": agent,
                "squad": squad,
                "task": getattr(request, "task", "general"),
                "provider": getattr(response, "provider", "unknown"),
                "model": getattr(response, "model", "unknown"),
                "is_llm": bool(getattr(response, "is_llm", False)),
                "tokens": getattr(getattr(response, "usage", None), "total_tokens", 0),
                "cost_usd": float(getattr(response, "cost_usd", 0.0)),
                "latency_ms": int(getattr(response, "latency_ms", 0)),
                "fallback_chain": list(getattr(response, "fallback_chain", [])),
                "prompt_excerpt": redact(str(getattr(request, "prompt", ""))[:600]),
                "response_excerpt": redact(str(getattr(response, "text", ""))[:600]),
                "error": getattr(response, "error", None),
            }
            with self._lock:
                self._buffer.append(entry)
                bucket = self._totals.setdefault(
                    squad, {"calls": 0.0, "tokens": 0.0, "cost_usd": 0.0, "ms": 0.0, "llm_calls": 0.0}
                )
                bucket["calls"] += 1
                bucket["tokens"] += float(entry["tokens"])
                bucket["cost_usd"] += entry["cost_usd"]
                bucket["ms"] += float(entry["latency_ms"])
                if entry["is_llm"]:
                    bucket["llm_calls"] += 1
            self._append_jsonl(entry)

            client = self._client()
            if client is not None:
                try:
                    trace = client.trace(
                        name=f"{agent}:{entry['task']}",
                        metadata={"squad": squad, "provider": entry["provider"]},
                        tags=[entry["provider"], entry["task"]],
                    )
                    trace.generation(
                        name=f"{entry['provider']}/{entry['model']}",
                        model=entry["model"],
                        input=entry["prompt_excerpt"],
                        output=entry["response_excerpt"],
                        usage={
                            "input": getattr(getattr(response, "usage", None), "prompt_tokens", 0),
                            "output": getattr(getattr(response, "usage", None), "completion_tokens", 0),
                            "unit": "TOKENS",
                        },
                        metadata={"latency_ms": entry["latency_ms"]},
                    )
                except Exception as exc:
                    log.debug("Falha ao enviar trace ao Langfuse: %s", exc)
        except Exception as exc:  # observabilidade nunca derruba a operação
            log.warning("Falha ao registar trace LLM: %s", exc)

    def _append_jsonl(self, entry: Dict[str, Any]) -> None:
        try:
            with self._trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ----------------------------------------------------------------- consulta
    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buffer)[-limit:][::-1]

    def metrics(self) -> Dict[str, Any]:
        with self._lock:
            squads = {
                squad: {
                    "calls": int(data["calls"]),
                    "llm_calls": int(data["llm_calls"]),
                    "tokens": int(data["tokens"]),
                    "cost_usd": round(data["cost_usd"], 6),
                    "avg_latency_ms": round(data["ms"] / max(1.0, data["calls"]), 1),
                }
                for squad, data in self._totals.items()
            }
            total_calls = sum(int(d["calls"]) for d in self._totals.values())
            total_tokens = sum(int(d["tokens"]) for d in self._totals.values())
        return {
            "squads": squads,
            "total_calls": total_calls,
            "total_tokens": total_tokens,
            "total_cost_usd": 0.0,
            "cost_per_1k_tokens_usd": self._cost_per_1k_tokens_usd,
            "langfuse_enabled": bool(settings.langfuse_enabled and self._langfuse_client),
            "generated_at": utcnow_iso(),
        }

    def flush(self) -> None:
        client = self._client()
        if client is not None:
            try:
                client.flush()
            except Exception as exc:
                log.debug("Falha no flush do Langfuse: %s", exc)


llm_tracer = LLMTracer()


def trace_chain_of_thought(
    agent: str, step: str, thought: str, *, payload: Optional[Dict[str, Any]] = None
) -> None:
    """Publica um passo de raciocínio no bus (LiveOpsScreen) e no log."""
    event_bus.publish(
        "chain_of_thought",
        agent=agent,
        message=thought,
        payload={"step": step, **(payload or {})},
    )


# ==========================================================================
# 2. Telegram
# ==========================================================================
class TelegramClient:
    """Cliente mínimo da Bot API (sem dependências), com whitelist."""

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token if token is not None else settings.telegram_bot_token
        self.allowed = set(settings.telegram_allowed_chat_ids)
        self._offset = 0
        self._poll_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._handlers: Dict[str, Callable[[str, List[str], str], str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # ------------------------------------------------------------------ HTTP
    def _call(self, method: str, payload: Optional[Dict[str, Any]] = None,
              timeout: int = 30) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "description": "TELEGRAM_BOT_TOKEN ausente"}
        url = TELEGRAM_API.format(token=self.token, method=method)
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return json.loads(body) if body else {"ok": True}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            log.warning("Telegram %s -> HTTP %s: %s", method, exc.code, detail)
            return {"ok": False, "error": f"HTTP {exc.code}", "detail": detail}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("Telegram %s falhou: %s", method, exc)
            return {"ok": False, "error": str(exc)}

    def send(self, chat_id: str, text: str, *, markdown: bool = False) -> Dict[str, Any]:
        if not self.is_allowed(chat_id):
            log.warning("Chat %s não está na whitelist do Telegram.", chat_id)
            return {"ok": False, "error": "chat_nao_autorizado"}
        return self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "Markdown" if markdown else None,
                "disable_web_page_preview": True,
            },
        )

    def broadcast(self, text: str, *, markdown: bool = False) -> None:
        for chat_id in self.allowed:
            try:
                self.send(chat_id, text, markdown=markdown)
            except Exception as exc:
                log.debug("Broadcast falhou para %s: %s", chat_id, exc)

    def is_allowed(self, chat_id: Any) -> bool:
        if not self.allowed:
            return False
        return str(chat_id) in self.allowed

    # ------------------------------------------------------------- comandos
    def register(self, command: str, handler: Callable[[str, List[str], str], str]) -> None:
        self._handlers[command.lstrip("/").lower()] = handler

    def dispatch(self, chat_id: str, text: str) -> Optional[str]:
        """Processa um comando localmente (usado pelo webhook e pelo polling)."""
        if not self.is_allowed(chat_id):
            return "⛔ Chat não autorizado."
        parts = (text or "").strip().split()
        if not parts:
            return None
        command = parts[0].lstrip("/").split("@")[0].lower()
        handler = self._handlers.get(command)
        if handler is None:
            return (
                "Comandos disponíveis: /status /kill /release /pendentes "
                "/aprovar <id> /vetar <id> /tesouraria /esquadroes /ajuda"
            )
        try:
            return handler(chat_id, parts[1:], text)
        except Exception as exc:
            log.exception("Handler Telegram /%s falhou", command)
            return f"⚠️ Erro ao executar /{command}: {exc}"

    # ------------------------------------------------------------- long polling
    def start_polling(self) -> bool:
        if not self.enabled or not self.allowed:
            log.info("Polling Telegram inactivo (token ou whitelist ausentes).")
            return False
        if self._poll_thread and self._poll_thread.is_alive():
            return True
        self._stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="telegram-poll", daemon=True
        )
        self._poll_thread.start()
        log.info("Polling Telegram iniciado para %d chat(s).", len(self.allowed))
        return True

    def stop_polling(self) -> None:
        self._stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = self._call(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": ["message"],
                    },
                    timeout=35,
                )
                if not result.get("ok"):
                    time.sleep(5)
                    continue
                for update in result.get("result", []) or []:
                    self._offset = max(
                        self._offset, int(update.get("update_id", 0)) + 1
                    )
                    message = update.get("message") or {}
                    chat_id = str((message.get("chat") or {}).get("id", ""))
                    text = message.get("text") or ""
                    if not chat_id or not text.startswith("/"):
                        continue
                    reply = self.dispatch(chat_id, text)
                    if reply:
                        self.send(chat_id, reply)
            except Exception as exc:
                log.warning("Ciclo de polling Telegram falhou: %s", exc)
                time.sleep(5)


telegram_client = TelegramClient()


def notify_president(text: str, *, markdown: bool = False) -> None:
    """Envia mensagem a todos os chats autorizados (usado pelos hooks do bus)."""
    if not telegram_client.enabled or not telegram_client.allowed:
        return
    try:
        telegram_client.broadcast(text, markdown=markdown)
    except Exception as exc:
        log.debug("Notificação Telegram falhou: %s", exc)


def bus_to_telegram_hook(event: Dict[str, Any]) -> None:
    """Espelha no Telegram apenas eventos relevantes (evita spam)."""
    if not telegram_client.enabled or not telegram_client.allowed:
        return
    level = str(event.get("level", "info"))
    event_type = str(event.get("type", ""))
    if level not in {"error", "critical", "warning"} and event_type not in {
        "approval_requested",
        "cycle_completed",
        "squad_killed",
        "payout_executed",
        "kill_switch",
    }:
        return
    emoji = {"critical": "🚨", "error": "❌", "warning": "⚠️"}.get(level, "ℹ️")
    notify_president(
        f"{emoji} *{event_type}* ({event.get('agent')})\n{event.get('message')}"
    )


def install_observability(router: Any) -> Dict[str, bool]:
    """Liga router -> tracer e bus -> Telegram. Devolve o que ficou activo."""
    installed = {"tracer": False, "telegram_hook": False}
    try:
        router.set_trace_hook(llm_tracer.record)
        installed["tracer"] = True
    except Exception as exc:
        log.error("Não foi possível instalar o tracer: %s", exc)
    try:
        event_bus.add_hook(bus_to_telegram_hook)
        installed["telegram_hook"] = True
    except Exception as exc:
        log.error("Não foi possível instalar o hook do Telegram: %s", exc)
    return installed


__all__ = [
    "LLMTracer",
    "llm_tracer",
    "trace_chain_of_thought",
    "TelegramClient",
    "telegram_client",
    "notify_president",
    "install_observability",
]
