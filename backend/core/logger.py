"""Logger estruturado (JSON opcional) partilhado por todo o backend.

Sem dependências externas: escreve para stdout e, quando ``LOG_DIR`` existir,
também para um ficheiro rotativo simples. Nunca lança excepções.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ..config.settings import settings

_CONFIGURED = False
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-34s | %(message)s"


class _JsonFormatter(logging.Formatter):
    """Formata registos como JSON de uma linha (consumível pelo Langfuse)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "automaton", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _configure_once() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger("automaton")
    root.setLevel(level)
    root.propagate = False

    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(stream=sys.stdout)
    if os.environ.get("LOG_JSON", "").strip().lower() in {"1", "true", "yes"}:
        stream.setFormatter(_JsonFormatter())
    else:
        stream.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(stream)

    log_dir_env = os.environ.get("LOG_DIR", "").strip()
    if log_dir_env:
        try:
            log_dir = Path(log_dir_env)
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                log_dir / "automaton.log", encoding="utf-8", delay=True
            )
            file_handler.setFormatter(logging.Formatter(_FORMAT))
            root.addHandler(file_handler)
        except OSError:
            # Sistema de ficheiros só-leitura: stdout continua a funcionar.
            pass


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Devolve um logger filho de ``automaton`` já configurado."""
    _configure_once()
    suffix = (name or "core").replace("backend.", "").strip(".")
    return logging.getLogger(f"automaton.{suffix}")


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    **fields: Any,
) -> None:
    """Registo estruturado com campos extras anexados ao JSON."""
    logger.log(level, message, extra={"automaton": fields})


__all__ = ["get_logger", "log_event"]
