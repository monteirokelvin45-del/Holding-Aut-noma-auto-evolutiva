"""Relógio único da holding (timezone-aware, UTC)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def utcnow() -> datetime:
    """Momento actual em UTC, timezone-aware."""
    return datetime.now(tz=timezone.utc)


def utcnow_iso() -> str:
    """String ISO-8601 em UTC com segundos (formato do cofre)."""
    return utcnow().replace(microsecond=0).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Converte uma string ISO em datetime UTC; ``None`` se inválida."""
    if not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def days_between(start: Optional[str], end: Optional[str] = None) -> float:
    """Dias (fraccionários) entre duas datas ISO; 0.0 se inválidas."""
    start_dt = parse_iso(start)
    if start_dt is None:
        return 0.0
    end_dt = parse_iso(end) if end else utcnow()
    if end_dt is None:
        end_dt = utcnow()
    delta = end_dt - start_dt
    return max(0.0, delta.total_seconds() / 86_400.0)


def days_since(start: Optional[str]) -> float:
    """Dias desde ``start`` até agora."""
    return days_between(start, None)


def add_days(base: datetime, days: int) -> datetime:
    return base + timedelta(days=days)


def today_utc_str() -> str:
    """Data UTC ``YYYY-MM-DD`` usada como chave de contadores diários."""
    return utcnow().strftime("%Y-%m-%d")


def month_key(moment: Optional[datetime] = None) -> str:
    """Chave mensal ``YYYY-MM`` para o motor de fases."""
    moment = moment or utcnow()
    return moment.strftime("%Y-%m")
