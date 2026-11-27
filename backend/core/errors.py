"""Excepções canónicas da holding.

Cada erro carrega um ``code`` estável (consumido pelo APK e pelo bot Telegram)
e um ``http_status`` para o mapeamento em ``main.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AutomatonError(Exception):
    """Erro base: erro de negócio esperado, com código estável."""

    code: str = "AUTOMATON_ERROR"
    http_status: int = 400

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class KillSwitchActive(AutomatonError):
    """Kill switch accionado: nenhuma operação de agente pode correr."""

    code = "KILL_SWITCH_ACTIVE"
    http_status = 423


class SpendCapExceeded(AutomatonError):
    """Tecto de gasto (global ou por agente) atingido."""

    code = "SPEND_CAP_EXCEEDED"
    http_status = 402


class ApprovalRequired(AutomatonError):
    """Operação exige aprovação biométrica do Presidente no APK."""

    code = "APPROVAL_REQUIRED"
    http_status = 428


class ValidationBlocked(AutomatonError):
    """Validação de demanda ou Juiz Eval bloqueou a produção."""

    code = "VALIDATION_BLOCKED"
    http_status = 409


class ExternalServiceError(AutomatonError):
    """Falha num conector externo (API, rede, subprocesso)."""

    code = "EXTERNAL_SERVICE_ERROR"
    http_status = 502


class ConfigurationError(AutomatonError):
    """Configuração em falta ou inválida para a operação pedida."""

    code = "CONFIGURATION_ERROR"
    http_status = 500


class NotFound(AutomatonError):
    """Recurso inexistente no cofre."""

    code = "NOT_FOUND"
    http_status = 404
