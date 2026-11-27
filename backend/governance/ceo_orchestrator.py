"""CEO Orquestrador: coordena o ciclo completo da holding.

Um ciclo (agendado a cada 6h pelo GitHub Actions ou on-demand pelo APK) executa:

  1. **Kill switch** — se activo, aborta antes de qualquer gasto.
  2. **Higiene** — expira aprovações vencidas e reavalia smoke tests.
  3. **Caça** — ``ScoutHunter`` procura oportunidades (se o intervalo passou).
  4. **Validação** — cria smoke tests para as melhores propostas (limite por ciclo).
  5. **Produção** — esquadrões produzem **apenas** para smoke tests aprovados.
  6. **Contabilidade** — TTFD/CAC/LTV e Regra de Abate; abates viram pedidos
     de aprovação ``kill_squad`` (nunca execução silenciosa).
  7. **RH** — reescreve prompts de agentes com ROI baixo.
  8. **Jurídico** — auditoria de licenças do repositório.
  9. **Tesouraria** — recalcula a fase e consolida o relatório.
 10. **Anti-fadiga** — agrupa tudo num único *batch* de aprovações com resumo
     para o APK/Telegram.

O ciclo é idempotente e serializado (um único ciclo corre de cada vez).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.approvals import approval_gate
from ..core.clock import days_since, utcnow_iso
from ..core.errors import KillSwitchActive
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.security_guard import kill_switch
from ..core.shared_memory import shared_memory
from ..core.vault_store import SQUAD_METRICS, vault
from ..finance.cfo_treasury import cfo
from ..governance.accountant_auditor import accountant
from ..governance.hr_department import hr_department
from ..governance.legal_compliance import legal_department
from ..squads.infoproducts_affiliate_squad import infoproducts_squad
from ..squads.micro_saas_dev_squad import micro_saas_squad
from ..squads.scout_hunter import scout_hunter
from ..squads.video_factory_squad import video_factory
from ..validation.demand_validator import demand_validator
from ..validation.eval_judge import eval_judge

log = get_logger("governance.ceo")

MAX_PROPOSALS_PER_CYCLE = 3
MAX_PRODUCTIONS_PER_CYCLE = 2
CYCLE_HISTORY_LIMIT = 60


@dataclass
class CycleReport:
    cycle_id: str
    status: str
    started_at: str
    finished_at: str = ""
    duration_seconds: float = 0.0
    steps: List[Dict[str, Any]] = field(default_factory=list)
    approvals_created: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 2),
            "steps": self.steps,
            "approvals_created": list(self.approvals_created),
            "errors": list(self.errors),
            "summary": self.summary,
        }


class CEOOrchestrator:
    """Coordena todas as camadas. Um ciclo de cada vez (lock global)."""

    def __init__(self) -> None:
        self._cycle_lock = threading.Lock()
        self._last_cycle: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ estado
    def _metrics(self) -> Dict[str, Any]:
        data = vault.read(SQUAD_METRICS)
        if not isinstance(data.get("cycles"), list):
            data["cycles"] = []
        return data

    def last_cycle(self) -> Optional[Dict[str, Any]]:
        if self._last_cycle is not None:
            return self._last_cycle
        cycles = self._metrics().get("cycles", [])
        return cycles[-1] if cycles else None

    def cycle_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return list(reversed(self._metrics().get("cycles", [])[-limit:]))

    def _persist_cycle(self, report: CycleReport) -> None:
        with vault.transaction(SQUAD_METRICS) as data:
            cycles = data.setdefault("cycles", [])
            cycles.append(report.to_dict())
            data["cycles"] = cycles[-CYCLE_HISTORY_LIMIT:]

    def status_snapshot(self) -> Dict[str, Any]:
        """Estado consolidado para o APK (dashboard de topo)."""
        treasury = cfo.snapshot()
        portfolio = accountant.portfolio()
        pending = approval_gate.pending_batches()
        smoke = demand_validator.report()
        return {
            "kill_switch": kill_switch.status(),
            "treasury": {
                "phase": treasury["phase"],
                "phase_name": treasury["phase_meta"]["name"],
                "total_assets_usd": treasury["total_assets_usd"],
                "balances_usd": treasury["balances_usd"],
                "balances_mzn": treasury["balances_mzn"],
                "monthly_revenue_usd": treasury["monthly_revenue_usd"],
            },
            "portfolio": portfolio["summary"],
            "approvals": {
                "pending": pending["pending_count"],
                "critical": pending["critical_count"],
                "pending_usd": pending["total_pending_usd"],
            },
            "validation": smoke["summary"],
            "cemetery": shared_memory.stats(),
            "last_cycle": self.last_cycle(),
            "budget": {
                "daily_cap_usd": settings.daily_spend_cap_usd,
                "per_agent_cap_usd": settings.per_agent_daily_cap_usd,
                "approval_threshold_usd": settings.approval_threshold_usd,
            },
            "generated_at": utcnow_iso(),
        }

    # ------------------------------------------------------------------- ciclo
    def run_cycle(
        self,
        *,
        source: str = "scheduler",
        force_scan: bool = False,
        produce: bool = True,
    ) -> Dict[str, Any]:
        """Executa um ciclo completo. Devolve o relatório."""
        if not self._cycle_lock.acquire(blocking=False):
            log.info("Ciclo já em execução; pedido de %s ignorado.", source)
            return {
                "status": "busy",
                "message": "Já existe um ciclo em execução.",
                "last_cycle": self.last_cycle(),
            }
        started = time.monotonic()
        report = CycleReport(
            cycle_id="cyc_" + utcnow_iso().replace(":", "").replace("-", "")[:15],
            status="running",
            started_at=utcnow_iso(),
        )
        try:
            # ---- 1. kill switch -------------------------------------------
            if kill_switch.engaged:
                state = kill_switch.status()
                report.status = "aborted"
                report.summary = (
                    f"Ciclo abortado: kill switch activo ({state['reason']})."
                )
                report.steps.append({"step": "kill_switch", "status": "blocked",
                                     "detail": state})
                report.finished_at = utcnow_iso()
                self._persist_cycle(report)
                event_bus.publish(
                    "cycle_aborted", agent="ceo_orchestrator",
                    message=report.summary, level="critical",
                    payload=state,
                )
                return report.to_dict()

            event_bus.publish(
                "cycle_started",
                agent="ceo_orchestrator",
                message=f"Ciclo iniciado ({source}).",
                payload={"cycle_id": report.cycle_id, "source": source},
            )

            # ---- 2. higiene ------------------------------------------------
            expired = approval_gate.sweep()
            smoke_changed = demand_validator.sweep_expired()
            report.steps.append(
                {
                    "step": "hygiene",
                    "status": "ok",
                    "detail": {
                        "approvals_expired": expired,
                        "smoke_tests_decided": smoke_changed,
                    },
                }
            )

            # ---- 3. caça ---------------------------------------------------
            should_scan = force_scan or self._scan_due()
            scan_result: Dict[str, Any] = {}
            proposals: List[Dict[str, Any]] = []
            if should_scan:
                try:
                    scout_run = scout_hunter.run(max_viable=MAX_PROPOSALS_PER_CYCLE)
                    scan_result = scout_run.to_dict()
                    proposals = [
                        a for a in scout_run.artifacts
                        if isinstance(a, dict) and a.get("opportunity_id")
                    ]
                    report.steps.append(
                        {
                            "step": "scout",
                            "status": scout_run.status,
                            "detail": {
                                "found": scout_run.metrics.get("total_found"),
                                "viable": scout_run.metrics.get("viable"),
                                "sources_skipped": scout_run.metrics.get("sources_skipped"),
                            },
                        }
                    )
                except Exception as exc:
                    report.errors.append(f"scout: {exc}")
                    log.exception("Scout falhou no ciclo")
                    report.steps.append({"step": "scout", "status": "error",
                                         "detail": str(exc)})
            else:
                report.steps.append(
                    {"step": "scout", "status": "skipped",
                     "detail": "intervalo mínimo não atingido"}
                )

            # ---- 4. validação (smoke tests para propostas) -----------------
            created_tests: List[Dict[str, Any]] = []
            for proposal in proposals[:MAX_PROPOSALS_PER_CYCLE]:
                try:
                    existing = demand_validator.list_tests(status="running")
                    if any(
                        p["idea"][:80].lower() == proposal["idea"][:80].lower()
                        for p in existing
                    ):
                        continue
                    result = demand_validator.create_smoke_test(
                        idea=proposal["idea"],
                        niche=proposal["niche"],
                        squad_type=proposal["squad_type"],
                        hypothesis=proposal.get("hypothesis", ""),
                        cta=proposal.get("cta", "Quero entrar na lista VIP"),
                        price_anchor_usd=proposal.get("price_anchor_usd", 0.0),
                        campaign_source=proposal.get("source", "scout"),
                    )
                    created_tests.append(result["test"])
                except Exception as exc:
                    report.errors.append(f"smoke_test: {exc}")
                    log.info("Smoke test não criado: %s", exc)
            report.steps.append(
                {
                    "step": "validation",
                    "status": "ok" if created_tests else "skipped",
                    "detail": {
                        "created": len(created_tests),
                        "tests": [
                            {"id": t["id"], "idea": t["idea"][:70]} for t in created_tests
                        ],
                    },
                }
            )

            # ---- 5. produção (só com demanda validada) ---------------------
            productions: List[Dict[str, Any]] = []
            if produce:
                productions = self._produce_for_validated(limit=MAX_PRODUCTIONS_PER_CYCLE)
            report.steps.append(
                {
                    "step": "production",
                    "status": "ok" if productions else "skipped",
                    "detail": {
                        "count": len(productions),
                        "items": [
                            {
                                "squad_id": p.get("squad_id"),
                                "type": p.get("squad_type"),
                                "status": p.get("status"),
                                "summary": str(p.get("summary", ""))[:160],
                            }
                            for p in productions
                        ],
                    },
                }
            )

            # ---- 6. contabilidade + regra de abate ------------------------
            audit = accountant.audit_all()
            kill_orders = audit["kill_orders"]
            batch_id = f"batch::{report.cycle_id}"
            for order in kill_orders:
                try:
                    approval_id = approval_gate.request(
                        title=(
                            f"Abater esquadrão {order['squad_id']}: "
                            f"{'; '.join(order['reasons'])[:120]}"
                        ),
                        kind="kill_squad",
                        amount_usd=0.0,
                        reason="; ".join(order["reasons"]),
                        requested_by="accountant_auditor",
                        metadata={"squad_id": order["squad_id"], "verdict": order},
                        batch_id=batch_id,
                    )
                    report.approvals_created.append(approval_id)
                except Exception as exc:
                    report.errors.append(f"kill_approval: {exc}")
            report.steps.append(
                {
                    "step": "accounting",
                    "status": "ok",
                    "detail": {
                        "evaluated": len(audit["verdicts"]),
                        "kill_orders": len(kill_orders),
                        "watched": audit["watched"],
                    },
                }
            )

            # ---- 7. RH -----------------------------------------------------
            hr_report = hr_department.review_all(auto_rewrite=True)
            report.steps.append(
                {
                    "step": "hr",
                    "status": "ok",
                    "detail": {
                        "agents_reviewed": len(hr_report["reviews"]),
                        "actions_needed": hr_report["actions_needed"],
                    },
                }
            )

            # ---- 8. jurídico ----------------------------------------------
            repo_audit = legal_department.audit_repository()
            report.steps.append(
                {
                    "step": "legal",
                    "status": "ok" if repo_audit.passed else "warning",
                    "detail": {
                        "passed": repo_audit.passed,
                        "critical": [
                            f.message for f in repo_audit.findings if f.severity == "critical"
                        ],
                    },
                }
            )

            # ---- 9. tesouraria --------------------------------------------
            phase = cfo.recompute_phase()
            revenue_audit = cfo.audit()
            if not revenue_audit["balanced"]:
                approval_id = approval_gate.request(
                    title=(
                        f"Revisar desvio contabilístico de ${revenue_audit['delta_usd']:.2f}"
                    ),
                    kind="custom",
                    reason="A verificação aritmética do ledger não fechou.",
                    requested_by="cfo_treasury",
                    metadata=revenue_audit,
                    priority="critical",
                    batch_id=batch_id,
                )
                report.approvals_created.append(approval_id)
            report.steps.append(
                {
                    "step": "treasury",
                    "status": "ok",
                    "detail": {
                        "phase": phase["phase"],
                        "phase_changed": phase["changed"],
                        "monthly_revenue_usd": phase["monthly_revenue_usd"],
                        "audit_balanced": revenue_audit["balanced"],
                    },
                }
            )

            # ---- 10. batch anti-fadiga ------------------------------------
            pending = approval_gate.pending_batches()
            report.summary = self._build_summary(
                report, pending, created_tests, productions, len(kill_orders)
            )
            report.status = "completed" if not report.errors else "completed_with_errors"
            report.finished_at = utcnow_iso()
            report.duration_seconds = time.monotonic() - started
            self._persist_cycle(report)
            self._last_cycle = report.to_dict()

            event_bus.publish(
                "cycle_completed",
                agent="ceo_orchestrator",
                message=report.summary,
                level="info" if not report.errors else "warning",
                payload={
                    "cycle_id": report.cycle_id,
                    "duration_seconds": round(report.duration_seconds, 2),
                    "approvals_created": report.approvals_created,
                    "errors": report.errors,
                    "pending_approvals": pending["pending_count"],
                },
            )
            return report.to_dict()
        except KillSwitchActive as exc:
            report.status = "aborted"
            report.errors.append(exc.message)
            report.finished_at = utcnow_iso()
            report.duration_seconds = time.monotonic() - started
            self._persist_cycle(report)
            return report.to_dict()
        except Exception as exc:
            log.exception("Ciclo falhou inesperadamente")
            report.status = "failed"
            report.errors.append(str(exc))
            report.finished_at = utcnow_iso()
            report.duration_seconds = time.monotonic() - started
            self._persist_cycle(report)
            event_bus.publish(
                "cycle_failed", agent="ceo_orchestrator",
                message=f"Ciclo falhou: {exc}", level="error",
                payload={"cycle_id": report.cycle_id},
            )
            return report.to_dict()
        finally:
            self._cycle_lock.release()

    # ---------------------------------------------------------------- internos
    def _scan_due(self) -> bool:
        cycles = self._metrics().get("cycles", [])
        last_scout = None
        for cycle in reversed(cycles):
            for step in cycle.get("steps", []):
                if step.get("step") == "scout" and step.get("status") not in {"skipped", "error"}:
                    last_scout = cycle.get("started_at")
                    break
            if last_scout:
                break
        if last_scout is None:
            return True
        elapsed_days = days_since(last_scout)
        return elapsed_days * 24 * 60 >= settings.scout_interval_minutes

    def _produce_for_validated(self, limit: int = 2) -> List[Dict[str, Any]]:
        """Produz para smoke tests aprovados ainda sem produção associada."""
        produced: List[Dict[str, Any]] = []
        passed_tests = demand_validator.list_tests(status="passed")
        cycles = self._metrics().get("cycles", [])
        already_produced: set[str] = set()
        for cycle in cycles:
            for step in cycle.get("steps", []):
                if step.get("step") == "production":
                    for item in step.get("detail", {}).get("items", []):
                        if item.get("smoke_test_id"):
                            already_produced.add(str(item["smoke_test_id"]))
        for test in passed_tests:
            if len(produced) >= limit:
                break
            if test["id"] in already_produced:
                continue
            if test.get("metadata", {}).get("produced"):
                continue
            squad_type = test.get("squad_type", "generic")
            try:
                result = self._dispatch_production(test)
                if result is not None:
                    produced.append(
                        {
                            "squad_id": result.squad_id,
                            "squad_type": squad_type,
                            "status": result.status,
                            "summary": result.summary,
                            "smoke_test_id": test["id"],
                        }
                    )
                    self._mark_produced(test["id"], result.squad_id)
            except Exception as exc:
                log.warning("Produção falhou para %s: %s", test["id"], exc)
                produced.append(
                    {
                        "squad_id": None,
                        "squad_type": squad_type,
                        "status": "error",
                        "summary": str(exc)[:200],
                        "smoke_test_id": test["id"],
                    }
                )
        return produced

    def _dispatch_production(self, test: Dict[str, Any]) -> Optional[Any]:
        squad_type = str(test.get("squad_type", "generic"))
        idea = str(test.get("idea", ""))
        niche = str(test.get("niche", ""))
        price = float(test.get("price_anchor_usd", 0.0) or 0.0)
        # Cada esquadrão tem um limite de guiões/ficheiros por ciclo para
        # manter o custo em $0.00 e a duração do ciclo controlada.
        if squad_type == "video_factory":
            return video_factory.run(
                idea=idea,
                niche=niche,
                smoke_test_id=test["id"],
                duration_target=30,
            )
        if squad_type == "infoproducts_affiliate":
            return infoproducts_squad.run(
                idea=idea,
                niche=niche,
                smoke_test_id=test["id"],
                product_type="ebook",
                price_usd=price or 12.0,
            )
        if squad_type == "micro_saas_dev":
            return micro_saas_squad.run(
                idea=idea,
                niche=niche,
                smoke_test_id=test["id"],
                price_usd=price or 9.0,
            )
        if squad_type == "data_b2b":
            # O esquadrão B2B reaproveita o pipeline de infoprodutos (lista +
            # guia de prospecção), mantendo o mesmo portão de qualidade.
            return infoproducts_squad.run(
                idea=idea,
                niche=niche,
                smoke_test_id=test["id"],
                product_type="seo_article",
            )
        log.info("Tipo de esquadrão sem produtor dedicado: %s", squad_type)
        return None

    def _mark_produced(self, test_id: str, squad_id: str) -> None:
        with vault.transaction(SQUAD_METRICS) as data:
            for raw in data.setdefault("smoke_tests", []):
                if raw.get("id") == test_id:
                    metadata = raw.setdefault("metadata", {})
                    metadata["produced"] = True
                    metadata["produced_by"] = squad_id
                    metadata["produced_at"] = utcnow_iso()
                    break

    def _build_summary(
        self,
        report: CycleReport,
        pending: Dict[str, Any],
        tests: List[Dict[str, Any]],
        productions: List[Dict[str, Any]],
        kill_orders: int,
    ) -> str:
        parts = [
            f"Ciclo {report.cycle_id}",
            f"{len(tests)} smoke test(s) novo(s)",
            f"{len(productions)} produção(ões)",
            f"{kill_orders} ordem(ns) de abate para aprovação",
            f"{pending['pending_count']} aprovação(ões) pendente(s) "
            f"(${pending['total_pending_usd']:.2f})",
        ]
        if report.errors:
            parts.append(f"{len(report.errors)} erro(s)")
        return " · ".join(parts)

    # ------------------------------------------------------- executar aprovado
    def execute_approved(self, approval_id: str) -> Dict[str, Any]:
        """Executa um item já aprovado (chamado após decisão no APK/Telegram)."""
        item = approval_gate.get(approval_id)
        if item.status != "approved":
            return {
                "ok": False,
                "status": item.status,
                "message": f"Aprovação {approval_id} não está aprovada.",
            }
        try:
            if item.kind == "kill_squad":
                squad_id = str((item.metadata or {}).get("squad_id", ""))
                result = accountant.kill_squad(
                    squad_id,
                    cause=item.reason or item.title,
                    approved_by="president",
                )
            elif item.kind == "payout":
                from ..finance.payout_bridge import PayoutRequest, payout_bridge

                channel = str((item.metadata or {}).get("channel", "mpesa"))
                result = payout_bridge.request_payout(
                    PayoutRequest(
                        amount_usd=float(item.amount_usd),
                        channel=channel,
                        approval_id=approval_id,
                        destination=str((item.metadata or {}).get("destination", "")),
                        note=item.title,
                        idempotency_key=approval_id,
                    )
                ).to_dict()
            elif item.kind == "spend":
                result = cfo.spend(
                    float(item.amount_usd),
                    agent_id=str((item.metadata or {}).get("agent_id", "ceo")),
                    purpose=item.title,
                    approved_by="president",
                    approval_id=approval_id,
                )
            elif item.kind == "phase_change":
                result = cfo.recompute_phase()
            elif item.kind == "production":
                result = {"note": "Produção é despoletada pelo ciclo.", "item": item.to_dict()}
            else:
                result = {
                    "note": "Item aprovado sem efeito automático; requer acção manual.",
                    "item": item.to_dict(),
                }
            try:
                approval_gate.mark_executed(approval_id, {"result": result})
            except Exception as exc:
                log.warning("Aprovação não marcada como executada: %s", exc)
            return {"ok": True, "status": "executed", "result": result}
        except Exception as exc:
            log.exception("Execução de aprovação %s falhou", approval_id)
            return {"ok": False, "status": "error", "message": str(exc)}


ceo = CEOOrchestrator()

__all__ = ["CEOOrchestrator", "ceo", "CycleReport"]
