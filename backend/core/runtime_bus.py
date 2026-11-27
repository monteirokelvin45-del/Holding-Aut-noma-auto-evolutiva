"""Bus de eventos em tempo real para o APK e o bot Telegram.

O APK mostra o "pensamento" dos agentes (LiveOpsScreen). Em vez de o cliente
fazer polling, mantemos:

  * Um **buffer circular** dos últimos eventos (para quem liga tarde).
  * Um conjunto de **filas assíncronas** (uma por WebSocket) com ``put_nowait``
    para que nenhum consumidor lento bloqueie os agentes.
  * **Redacção de segredos** em todos os payloads antes de saírem do processo.

Eventos importantes são também espelhados no Telegram via
``observability_langfuse`` (hook opcional), evitando imports circulares.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set

from .clock import utcnow_iso
from .logger import get_logger

log = get_logger("core.bus")

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(Bearer\s+[A-Za-z0-9._\-]{8,})", re.IGNORECASE),
    re.compile(r"(\b[A-Za-z0-9]{32,}\b)"),
    re.compile(
        r"((?:api[_-]?key|token|secret|password|senha|authorization)\s*[=:]\s*)"
        r"(['\"]?)([^\s'\"]{4,})\2",
        re.IGNORECASE,
    ),
    re.compile(r"(\+258\d{9})"),
]


def redact(value: Any, _depth: int = 0) -> Any:
    """Remove segredos de strings/dicts antes de expor a clientes."""
    if _depth > 8:
        return "[profundidade_excedida]"
    if isinstance(value, str):
        cleaned = value
        for pattern in _SECRET_PATTERNS[:-1]:
            if pattern.groups >= 3:
                cleaned = pattern.sub(r"\1\2***REDACTED***\2", cleaned)
            else:
                cleaned = pattern.sub(r"***REDACTED***", cleaned)
        # Números de telefone: manter os últimos 3 dígitos
        cleaned = _SECRET_PATTERNS[-1].sub(
            lambda m: m.group(1)[:-3] + "***", cleaned
        )
        return cleaned[:8000]
    if isinstance(value, dict):
        return {
            str(k): (
                "***REDACTED***"
                if re.search(r"key|token|secret|password|senha|credential", str(k), re.I)
                else redact(v, _depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value][:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), _depth + 1)


@dataclass
class Event:
    """Evento imutável publicado no bus."""

    type: str
    agent: str
    message: str
    level: str = "info"
    payload: Dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    ts: str = field(default_factory=utcnow_iso)
    sequence: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": self.type,
            "agent": self.agent,
            "level": self.level,
            "message": redact(self.message),
            "payload": redact(self.payload),
            "correlation_id": self.correlation_id,
            "ts": self.ts,
        }


class EventBus:
    """Bus híbrido thread-safe / asyncio-safe."""

    def __init__(self, capacity: int = 500) -> None:
        self._buffer: Deque[Event] = deque(maxlen=capacity)
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = threading.RLock()
        self._sequence = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._hooks: List[Any] = []

    # ------------------------------------------------------------------ setup
    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Chamado no startup do FastAPI para permitir publicar de threads."""
        self._loop = loop or asyncio.get_event_loop()

    def add_hook(self, hook: Any) -> None:
        """Hook síncrono chamado fora do lock (ex.: espelhar no Telegram)."""
        if callable(hook):
            self._hooks.append(hook)

    # ---------------------------------------------------------------- publicação
    def publish(
        self,
        event_type: str,
        agent: str,
        message: str,
        *,
        level: str = "info",
        payload: Optional[Dict[str, Any]] = None,
        correlation_id: str = "",
    ) -> Event:
        with self._lock:
            self._sequence += 1
            event = Event(
                type=event_type,
                agent=agent,
                message=message,
                level=level,
                payload=payload or {},
                correlation_id=correlation_id,
                sequence=self._sequence,
            )
            self._buffer.append(event)
            queues = list(self._subscribers)

        for queue in queues:
            try:
                queue.put_nowait(event.to_dict())
            except asyncio.QueueFull:
                log.debug("Subscritor lento: evento %d descartado.", event.sequence)
            except Exception as exc:  # pragma: no cover
                log.debug("Falha ao entregar evento: %s", exc)

        for hook in self._hooks:
            try:
                hook(event.to_dict())
            except Exception as exc:
                log.debug("Hook do bus falhou: %s", exc)

        if level in {"error", "critical"}:
            log.error("[%s/%s] %s", event_type, agent, message[:200])
        else:
            log.info("[%s/%s] %s", event_type, agent, message[:200])
        return event

    def publish_threadsafe(self, *args: Any, **kwargs: Any) -> None:
        """Publica a partir de um worker thread (o ciclo do CEO corre em thread)."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                lambda: self.publish(*args, **kwargs)
            )
        else:
            self.publish(*args, **kwargs)

    # -------------------------------------------------------------- subscrição
    def subscribe(self, maxsize: int = 200) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ------------------------------------------------------------------ leitura
    def recent(self, limit: int = 100, since_sequence: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            events = [
                event.to_dict()
                for event in self._buffer
                if event.sequence > since_sequence
            ]
        return events[-limit:]

    def last_sequence(self) -> int:
        with self._lock:
            return self._sequence


event_bus = EventBus()

__all__ = ["EventBus", "event_bus", "Event", "redact"]
