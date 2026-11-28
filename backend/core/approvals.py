"""Portão de decisões do Presidente (APK + Telegram).

Anti-Fadiga: em vez de notificar o Presidente a cada micro-decisão, o
``ceo_orchestrator`` agrupa tudo num **lote** (``batch``). Só decisões
classificadas como ``critical`` (gastos > limiar, saques M-Pesa, abate de
esquadrão, kill switch) interrompem imediatamente.

Ciclo de vida de um item:
    pending ──approve──► approved ──(executor)──► executed
        └────reject───► rejected
        └────expire───► expired

Aprovações exigem ``biometric_verified=True`` (validado no APK via
``BiometricAuthManager`` e reenviado como prova no corpo do pedido).
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from .clock import parse_iso, utcnow, utcnow_iso
from .errors import ApprovalRequired, NotFound
from .logger import get_logger
from .runtime_bus import event_bus
from .vault_store import PENDING_APPROVALS, vault

log = get_logger("core.approvals")

VALID_KINDS = {
    "spend",
    "payout",
    "kill_squad",
    "production",
    "license",
    "hire",
    "fire",
    "phase_change",
    "custom",
}
CRITICAL_KINDS = {"payout", "kill_squad", "phase_change"}
DEFAULT_TTL_HOURS = 72


@dataclass
class ApprovalItem:
    id: str
    title: str
    kind: str
    amount_usd: float = 0.0
    priority: str = "normal"  # normal | critical
    status: str = "pending"  # pending | approved | rejected | expired | executed
    requested_by: str = "system"
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)
    expires_at: str = ""
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    biometric_verified: bool = False
    decision_note: str = ""
    batch_id: str = ""
    execution_result: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ApprovalItem":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})  # type: ignore[arg-type]

    @property
    def is_expired(self) -> bool:
        expiry = parse_iso(self.expires_at)
        return bool(expiry and utcnow() > expiry and self.status == "pending")


class ApprovalGate:
    """Gestão persistente de pedidos de aprovação (anti-fadiga incluída)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ criação
    def request(
        self,
        *,
        title: str,
        kind: str = "custom",
        amount_usd: float = 0.0,
        reason: str = "",
        requested_by: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        priority: Optional[str] = None,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        batch_id: str = "",
    ) -> str:
        """Cria um pedido pendente e devolve o ``approval_id``."""
        safe_kind = kind if kind in VALID_KINDS else "custom"
        try:
            amount = round(max(0.0, float(amount_usd)), 6)
        except (TypeError, ValueError):
            amount = 0.0

        computed_priority = priority or (
            "critical"
            if safe_kind in CRITICAL_KINDS or amount > settings.approval_threshold_usd
            else "normal"
        )
        expires = (utcnow() + timedelta(hours=max(1, int(ttl_hours)))).replace(
            microsecond=0
        ).isoformat()

        with self._lock, vault.transaction(PENDING_APPROVALS) as data:
            data.setdefault("items", [])
            sequence = int(data.get("sequence", 0))
            # Deduplicação: mesmo título+kind pendente não gera ruído extra.
            for raw in data["items"]:
                if (
                    raw.get("status") == "pending"
                    and raw.get("title") == title
                    and raw.get("kind") == safe_kind
                ):
                    log.info("Pedido duplicado reutilizado: %s", raw.get("id"))
                    return str(raw.get("id"))

            sequence += 1
            item_id = "apr_" + hashlib.sha1(
                f"{title}|{safe_kind}|{utcnow_iso()}|{sequence}".encode("utf-8")
            ).hexdigest()[:12]
            item = ApprovalItem(
                id=item_id,
                title=title[:240],
                kind=safe_kind,
                amount_usd=amount,
                priority=str(computed_priority),
                requested_by=requested_by[:80],
                reason=(reason or "")[:800],
                metadata=dict(metadata or {}),
                expires_at=expires,
                batch_id=batch_id,
            )
            data["sequence"] = sequence
            data["items"].append(item.to_dict())
            # Mantém o ficheiro compacto: só os 300 mais recentes.
            if len(data["items"]) > 300:
                data["items"] = data["items"][-300:]

        event_bus.publish(
            "approval_requested",
            agent=requested_by,
            message=f"Nova aprovação pendente: {item.title}",
            level="warning" if computed_priority == "critical" else "info",
            payload={
                "approval_id": item_id,
                "kind": safe_kind,
                "amount_usd": amount,
                "priority": computed_priority,
            },
        )
        log.warning("Aprovação criada %s (%s, $%.2f) — %s", item_id, safe_kind, amount, title)
        return item_id

    # ------------------------------------------------------------------ leitura
    def _read_items(self) -> Dict[str, Any]:
        data = vault.read(PENDING_APPROVALS)
        if not isinstance(data.get("items"), list):
            data["items"] = []
        return data

    def list_items(
        self,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        items = [ApprovalItem.from_dict(raw) for raw in self._read_items()["items"]]
        self._expire_stale(items)
        filtered = [
            item
            for item in items
            if (status is None or item.status == status)
            and (kind is None or item.kind == kind)
        ]
        filtered.sort(key=lambda i: (i.status != "pending", i.created_at), reverse=False)
        return [item.to_dict() for item in filtered[-limit:]][::-1]

    def get(self, approval_id: str) -> ApprovalItem:
        for raw in self._read_items()["items"]:
            if raw.get("id") == approval_id:
                return ApprovalItem.from_dict(raw)
        raise NotFound(f"Aprovação {approval_id} não encontrada.")

    def pending_batches(self) -> Dict[str, Any]:
        """Agrupamento para o ApprovalDeck do APK (anti-fadiga)."""
        items = [ApprovalItem.from_dict(raw) for raw in self._read_items()["items"]]
        self._expire_stale(items)
        pending = [item for item in items if item.status == "pending"]
        critical = [item for item in pending if item.priority == "critical"]
        batches: Dict[str, List[Dict[str, Any]]] = {}
        for item in pending:
            key = item.batch_id or f"default::{item.kind}"
            batches.setdefault(key, []).append(item.to_dict())
        return {
            "pending_count": len(pending),
            "critical_count": len(critical),
            "total_pending_usd": round(sum(i.amount_usd for i in pending), 6),
            "critical": [item.to_dict() for item in critical],
            "batches": batches,
            "generated_at": utcnow_iso(),
        }

    # ------------------------------------------------------------------- decisão
    def decide(
        self,
        approval_id: str,
        *,
        approve: bool,
        decided_by: str = "president",
        biometric_verified: bool = False,
        note: str = "",
        require_biometric: bool = True,
    ) -> Dict[str, Any]:
        """Aprova ou veta um pedido. Exige biometria por omissão."""
        with self._lock:
            item = self.get(approval_id)
            if item.status != "pending":
                raise ApprovalRequired(
                    f"Pedido {approval_id} já está '{item.status}' e não pode ser decidido.",
                    details={"status": item.status},
                )
            if item.is_expired:
                self._mark(approval_id, status="expired", decided_by="system",
                           note="TTL esgotado")
                raise ApprovalRequired(
                    f"Pedido {approval_id} expirou antes da decisão.",
                    details={"expires_at": item.expires_at},
                )
            needs_bio = require_biometric and (
                item.priority == "critical" or item.amount_usd > 0
            )
            if needs_bio and not biometric_verified:
                raise ApprovalRequired(
                    "Aprovação exige verificação biométrica no APK.",
                    details={
                        "approval_id": approval_id,
                        "amount_usd": item.amount_usd,
                        "critical": item.priority == "critical",
                    },
                )
            new_status = "approved" if approve else "rejected"
            updated = self._mark(
                approval_id,
                status=new_status,
                decided_by=decided_by,
                note=note,
                biometric_verified=biometric_verified,
            )

        event_bus.publish(
            "approval_decided",
            agent=decided_by,
            message=(
                f"{'APROVADO' if approve else 'VETADO'}: {item.title}"
            ),
            level="info" if approve else "warning",
            payload={
                "approval_id": approval_id,
                "status": new_status,
                "amount_usd": item.amount_usd,
                "kind": item.kind,
            },
        )
        log.warning(
            "Decisão %s em %s por %s (biometria=%s): %s",
            new_status, approval_id, decided_by, biometric_verified, item.title,
        )
        return updated

    def _mark(
        self,
        approval_id: str,
        *,
        status: str,
        decided_by: str = "system",
        note: str = "",
        biometric_verified: bool = False,
        execution_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with vault.transaction(PENDING_APPROVALS) as data:
            for raw in data.get("items", []):
                if raw.get("id") != approval_id:
                    continue
                raw["status"] = status
                raw["decided_at"] = utcnow_iso()
                raw["decided_by"] = decided_by
                raw["decision_note"] = note[:600]
                if status in {"approved", "rejected"}:
                    raw["biometric_verified"] = bool(biometric_verified)
                if execution_result is not None:
                    raw["execution_result"] = execution_result
                return dict(raw)
        raise NotFound(f"Aprovação {approval_id} desapareceu do cofre.")

    def mark_executed(
        self, approval_id: str, result: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        item = self.get(approval_id)
        if item.status != "approved":
            raise ApprovalRequired(
                f"Só pedidos aprovados podem ser executados (estado: {item.status}).",
                details={"approval_id": approval_id},
            )
        updated = self._mark(
            approval_id,
            status="executed",
            decided_by="executor",
            note="executado",
            execution_result=result or {},
        )
        event_bus.publish(
            "approval_executed",
            agent="executor",
            message=f"Executado: {item.title}",
            payload={"approval_id": approval_id, "result": result or {}},
        )
        return updated

    def _expire_stale(self, items: List[ApprovalItem]) -> None:
        expired_ids = [item.id for item in items if item.is_expired]
        if not expired_ids:
            return
        try:
            with vault.transaction(PENDING_APPROVALS) as data:
                for raw in data.get("items", []):
                    if raw.get("id") in expired_ids and raw.get("status") == "pending":
                        raw["status"] = "expired"
                        raw["decided_at"] = utcnow_iso()
                        raw["decided_by"] = "system"
                        raw["decision_note"] = "TTL esgotado"
            log.info("%d aprovações expiradas automaticamente.", len(expired_ids))
        except Exception as exc:
            log.warning("Falha ao expirar aprovações: %s", exc)

    def sweep(self) -> int:
        """Expira pendentes vencidos. Devolve quantos foram tocados."""
        before = len([i for i in self.list_items(status="pending")])
        items = [ApprovalItem.from_dict(raw) for raw in self._read_items()["items"]]
        self._expire_stale(items)
        after = len([i for i in self.list_items(status="pending")])
        return max(0, before - after)


approval_gate = ApprovalGate()

__all__ = ["ApprovalGate", "approval_gate", "ApprovalItem", "VALID_KINDS"]
