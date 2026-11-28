"""Infra-estrutura comum dos esquadrões operacionais.

Todos os esquadrões herdam de ``BaseSquad``, que impõe — *por código*, não por
boa vontade — a cadeia de portões do mandato:

    kill switch → cemitério de negócios → smoke test aprovado
                → jurídico (licenças/ToS/mídia) → Juiz Eval (>= 85) → publicação

Qualquer esquadrão que tente produzir fora desta ordem recebe
``KillSwitchActive``, ``ValidationBlocked`` ou ``ApprovalRequired``.
"""

from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core import grounding
from ..core.clock import utcnow_iso
from ..core.errors import KillSwitchActive, ValidationBlocked
from ..core.llm_router import LLMRequest, llm_router
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.security_guard import kill_switch, prompt_shield
from ..core.shared_memory import memory_context_block, shared_memory
from ..core.vault_store import SQUAD_METRICS, vault
from ..governance.accountant_auditor import accountant
from ..validation.demand_validator import demand_validator
from ..validation.eval_judge import eval_judge

log = get_logger("squads.base")


@dataclass
class SquadRunResult:
    """Resultado uniforme de qualquer execução de esquadrão."""

    squad_id: str
    squad_type: str
    action: str
    status: str  # completed | blocked | awaiting_validation | failed
    summary: str
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    next_actions: List[str] = field(default_factory=list)
    blockers: List[Dict[str, Any]] = field(default_factory=list)
    llm_provider: str = "n/a"
    is_llm: bool = False
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "squad_id": self.squad_id,
            "squad_type": self.squad_type,
            "action": self.action,
            "status": self.status,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "metrics": self.metrics,
            "next_actions": self.next_actions,
            "blockers": self.blockers,
            "llm_provider": self.llm_provider,
            "is_llm": self.is_llm,
            "created_at": self.created_at,
        }


class BaseSquad(abc.ABC):
    """Contrato e portões partilhados por todos os esquadrões."""

    squad_type: str = "generic"
    squad_name: str = "Esquadrão genérico"
    owner_agent: str = "unknown_agent"
    requires_validation: bool = True
    content_type: str = "copy"

    # ------------------------------------------------------------------ gates
    def ensure_operational(self, action: str) -> None:
        kill_switch.ensure_operational(f"{self.squad_name}: {action}")

    def ensure_not_repeated(
        self, idea: str, niche: str = "", tags: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Portão 1: a ideia não pode constar no cemitério de negócios."""
        return shared_memory.check_against_cemetery(
            idea=idea, niche=niche, tags=tags
        )

    def ensure_demand(self, smoke_test_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Portão 2: smoke test aprovado antes de produzir."""
        if not self.requires_validation:
            return None
        if not smoke_test_id:
            raise ValidationBlocked(
                "Produção exige um smoke test aprovado (smoke_test_id em falta).",
                details={"squad_type": self.squad_type},
            )
        return demand_validator.require_validated(smoke_test_id)

    def review_with_judge(
        self,
        *,
        content: str,
        squad_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        content_type: Optional[str] = None,
    ) -> Any:
        """Portão 4: Juiz Eval bloqueia abaixo de 85."""
        return eval_judge.enforce(
            content=content,
            content_type=content_type or self.content_type,
            squad_id=squad_id,
            agent_id=self.owner_agent,
            metadata=metadata or {},
        )

    # ------------------------------------------------------------------ LLM
    def ask_llm(
        self,
        *,
        prompt: str,
        task: str = "general",
        system: Optional[str] = None,
        json_mode: bool = False,
        untrusted: str = "",
        untrusted_source: str = "web",
        max_tokens: int = 1200,
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Chamada ao roteador com memória corporativa e escudo já aplicados."""
        request = LLMRequest(
            prompt=prompt,
            system=system or self._default_system(),
            task=task,
            max_tokens=max_tokens,
            json_mode=json_mode,
            untrusted_content=untrusted,
            untrusted_source=untrusted_source,
            agent_id=self.owner_agent,
            context={"squad": self.squad_type, **(context or {})},
        )
        return llm_router.complete(request)

    def _default_system(self) -> str:
        lessons = shared_memory.lessons_for(self.squad_type, limit=6)
        base = (
            f"És um agente operacional do {self.squad_name} da Automaton Holding Corp. "
            "Operas com orçamento $0.00, usas apenas ferramentas com licença "
            "comercial (MIT/Apache-2.0/BSD/LGPL) e nunca produzes sem validação "
            "de demanda. Escreves em português de Moçambique, com precisão factual."
        )
        # O bloco de factos também entra aqui: o router já o injecta, mas um
        # esquadrão pode chamar o LLM por outro caminho (CrewAI, teste
        # directo). `ancorar` não duplica.
        base = grounding.ancorar(base)
        if lessons:
            base += "\nLições do passado (obrigatório respeitar):\n" + "\n".join(
                f"- {lesson}" for lesson in lessons
            )
        return base

    # -------------------------------------------------------------- registo
    def ensure_registered(
        self,
        *,
        squad_id: str,
        name: str,
        niche: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return accountant.register_squad(
            squad_id=squad_id,
            name=name,
            squad_type=self.squad_type,
            niche=niche,
            owner_agent=self.owner_agent,
            metadata=metadata,
        )

    def record_activity(self, squad_id: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            return accountant.record_activity(squad_id, **kwargs)
        except ValueError as exc:
            log.warning("Registo de actividade ignorado para %s: %s", squad_id, exc)
            return {}

    def record_agent_execution(
        self,
        *,
        success: bool,
        tokens: int = 0,
        judge_score: Optional[float] = None,
        failure_reason: str = "",
    ) -> None:
        from ..governance.hr_department import hr_department

        try:
            hr_department.record_execution(
                self.owner_agent,
                success=success,
                tokens=tokens,
                judge_score=judge_score,
                failure_reason=failure_reason,
            )
        except Exception as exc:
            log.debug("RH não registou execução: %s", exc)

    def persist_artifact(self, record: Dict[str, Any]) -> None:
        """Guarda o artefacto no cofre de métricas para auditoria."""
        with vault.transaction(SQUAD_METRICS) as data:
            artifacts = data.setdefault("artifacts", [])
            artifacts.append(record)
            data["artifacts"] = artifacts[-300:]

    @staticmethod
    def stable_id(prefix: str, *parts: Any) -> str:
        digest = hashlib.sha1(
            "|".join(str(part) for part in parts).encode("utf-8")
        ).hexdigest()[:12]
        return f"{prefix}_{digest}"

    def publish(self, event_type: str, message: str, **kwargs: Any) -> None:
        event_bus.publish(
            event_type,
            agent=self.owner_agent,
            message=message,
            payload=kwargs or None,
        )

    # ------------------------------------------------------------- contrato
    @abc.abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> SquadRunResult:
        """Executa o ciclo do esquadrão (implementado por cada subclasse)."""


def sanitize_external(payload: str, source: str = "web") -> str:
    """Sanitiza texto raspado antes de o usar (nunca confiar na web)."""
    result = prompt_shield.scan(payload, source=source)
    return result.text


__all__ = [
    "BaseSquad",
    "SquadRunResult",
    "sanitize_external",
    "memory_context_block",
    "settings",
]
