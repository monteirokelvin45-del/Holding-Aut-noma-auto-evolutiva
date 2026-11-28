"""Ferramentas transversais partilhadas por todo o backend."""

from .vault_store import VaultStore, vault
from .clock import utcnow, utcnow_iso, parse_iso, days_since
from .errors import (
    AutomatonError,
    KillSwitchActive,
    SpendCapExceeded,
    ApprovalRequired,
    ValidationBlocked,
    ExternalServiceError,
    ConfigurationError,
)
from .logger import get_logger

__all__ = [
    "VaultStore",
    "vault",
    "utcnow",
    "utcnow_iso",
    "parse_iso",
    "days_since",
    "AutomatonError",
    "KillSwitchActive",
    "SpendCapExceeded",
    "ApprovalRequired",
    "ValidationBlocked",
    "ExternalServiceError",
    "ConfigurationError",
    "get_logger",
]
