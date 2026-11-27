"""Contabilidade e auditoria de esquadrões: TTFD, CAC, LTV e Regra de Abate.

Métricas por unidade de negócio:
  * **TTFD** (*Time to First Dollar*): dias entre a criação do esquadrão e a
    primeira receita registada.
  * **CAC**: custo (USD) por aquisição — normalizado por 1 cliente **ou** por
    1 000 views, conforme a unidade seja de venda directa ou de atenção.
  * **LTV / Retenção**: receita acumulada por conversão e taxa de retenção.

Regra de Abate (Kill Rule), avaliada em cada ciclo:
  1. Sem tracção mínima (views < limite) ao fim de 7 dias -> abate imediato.
  2. Sem o primeiro dólar ao fim de 21 dias -> abate imediato.
  3. Conversão abaixo do mínimo depois de 14 dias com tráfego -> abate.

O abate nunca é silencioso: gera relatório no ``post_mortem_memory.json``,
propõe aprovação ``kill_squad`` (crítica) e publica no bus/Telegram.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.clock import days_between, days_since, utcnow, utcnow_iso
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.shared_memory import shared_memory
from ..core.vault_store import SQUAD_METRICS, vault
from ..finance.cfo_treasury import cfo

log = get_logger("governance.accountant")

DEFAULT_KILL_POLICY: Dict[str, Any] = {
    "min_traction_days": 7,
    "min_views_in_traction_window": 200,
    "first_dollar_deadline_days": 21,
    "min_conversion_rate": 0.005,
    "conversion_check_days": 14,
    "min_spend_to_judge_usd": 0.0,
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class SquadVerdict:
    squad_id: str
    decision: str  # keep | watch | kill
    reasons: List[str]
    ttfd_days: Optional[float]
    cac_usd: Optional[float]
    ltv_usd: Optional[float]
    retention_rate: float
    days_alive: float
    evaluated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "squad_id": self.squad_id,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "ttfd_days": self.ttfd_days,
            "cac_usd": self.cac_usd,
            "ltv_usd": self.ltv_usd,
            "retention_rate": round(self.retention_rate, 4),
            "days_alive": round(self.days_alive, 2),
            "evaluated_at": self.evaluated_at,
        }


class AccountantAuditor:
    """Calcula métricas e aplica a Regra de Abate."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ leitura
    def _data(self) -> Dict[str, Any]:
        data = vault.read(SQUAD_METRICS)
        if not isinstance(data.get("squads"), dict):
            data["squads"] = {}
        policy = data.get("kill_policy")
        if not isinstance(policy, dict):
            policy = {}
        merged = dict(DEFAULT_KILL_POLICY)
        merged.update(policy)
        data["kill_policy"] = merged
        return data

    def policy(self) -> Dict[str, Any]:
        return dict(self._data()["kill_policy"])

    def squads(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        squads = list(self._data()["squads"].values())
        if status:
            squads = [s for s in squads if s.get("status") == status]
        squads.sort(key=lambda s: str(s.get("created_at", "")), reverse=True)
        return squads

    # ------------------------------------------------------------------ registo
    def register_squad(
        self,
        *,
        squad_id: str,
        name: str,
        squad_type: str,
        niche: str,
        owner_agent: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Cria (ou reactiva) a ficha de um esquadrão no portefólio."""
        squad_id = (squad_id or "").strip()
        if not squad_id:
            raise ValueError("squad_id é obrigatório.")
        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            data.setdefault("squads", {})
            existing = data["squads"].get(squad_id)
            if existing and existing.get("status") == "active":
                log.info("Esquadrão %s já está activo.", squad_id)
                return dict(existing)
            now = utcnow_iso()
            record = {
                "id": squad_id,
                "name": name[:120],
                "type": squad_type[:60],
                "niche": niche[:200],
                "owner_agent": owner_agent[:80],
                "status": "active",
                "created_at": now,
                "activated_at": now,
                "closed_at": None,
                "first_dollar_at": existing.get("first_dollar_at") if existing else None,
                "ttfd_days": existing.get("ttfd_days") if existing else None,
                "views": _int((existing or {}).get("views", 0)),
                "clicks": _int((existing or {}).get("clicks", 0)),
                "conversions": _int((existing or {}).get("conversions", 0)),
                "revenue_usd": _num((existing or {}).get("revenue_usd", 0.0)),
                "cost_usd": _num((existing or {}).get("cost_usd", 0.0)),
                "cac_usd": None,
                "ltv_usd": None,
                "retention_rate": 0.0,
                "eval_scores": (existing or {}).get("eval_scores", []),
                "daily": (existing or {}).get("daily", {}),
                "metadata": dict(metadata or {}),
                "verdict_history": (existing or {}).get("verdict_history", []),
                "updated_at": now,
            }
            data["squads"][squad_id] = record
        event_bus.publish(
            "squad_registered",
            agent=owner_agent,
            message=f"Novo esquadrão activo: {name} ({squad_type}) — {niche}",
            payload={"squad_id": squad_id, "type": squad_type, "niche": niche},
        )
        log.warning("Esquadrão registado: %s (%s)", squad_id, name)
        return record

    def record_activity(
        self,
        squad_id: str,
        *,
        views: int = 0,
        clicks: int = 0,
        conversions: int = 0,
        revenue_usd: float = 0.0,
        cost_usd: float = 0.0,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Acumula métricas do dia (views/clicks/vendas/receita/custo)."""
        if not any([views, clicks, conversions, revenue_usd, cost_usd]):
            return self.get_squad(squad_id)
        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            squads = data.setdefault("squads", {})
            if squad_id not in squads:
                raise ValueError(f"Esquadrão {squad_id} não existe no portefólio.")
            record = squads[squad_id]
            day = utcnow().strftime("%Y-%m-%d")
            daily = record.setdefault("daily", {})
            bucket = daily.setdefault(
                day, {"views": 0, "clicks": 0, "conversions": 0, "revenue_usd": 0.0, "cost_usd": 0.0}
            )
            bucket["views"] = _int(bucket.get("views", 0)) + max(0, _int(views))
            bucket["clicks"] = _int(bucket.get("clicks", 0)) + max(0, _int(clicks))
            bucket["conversions"] = _int(bucket.get("conversions", 0)) + max(0, _int(conversions))
            bucket["revenue_usd"] = round(
                _num(bucket.get("revenue_usd", 0.0)) + max(0.0, _num(revenue_usd)), 6
            )
            bucket["cost_usd"] = round(
                _num(bucket.get("cost_usd", 0.0)) + max(0.0, _num(cost_usd)), 6
            )
            record["views"] = _int(record.get("views", 0)) + max(0, _int(views))
            record["clicks"] = _int(record.get("clicks", 0)) + max(0, _int(clicks))
            record["conversions"] = _int(record.get("conversions", 0)) + max(
                0, _int(conversions)
            )
            record["revenue_usd"] = round(
                _num(record.get("revenue_usd", 0.0)) + max(0.0, _num(revenue_usd)), 6
            )
            record["cost_usd"] = round(
                _num(record.get("cost_usd", 0.0)) + max(0.0, _num(cost_usd)), 6
            )
            if (
                record.get("first_dollar_at") is None
                and _num(revenue_usd) > 0
            ):
                record["first_dollar_at"] = utcnow_iso()
                record["ttfd_days"] = round(
                    days_since(record.get("created_at")), 2
                )
                log.warning(
                    "TTFD de %s: %.2f dias (primeiro dólar!)",
                    squad_id, record["ttfd_days"],
                )
            record["notes"] = notes[:400]
            record["updated_at"] = utcnow_iso()
            # Limita o histórico diário a 180 dias.
            if len(daily) > 180:
                for key in sorted(daily.keys())[:-180]:
                    daily.pop(key, None)
            result = dict(record)
        return result

    def get_squad(self, squad_id: str) -> Dict[str, Any]:
        squads = self._data()["squads"]
        if squad_id not in squads:
            raise ValueError(f"Esquadrão {squad_id} não existe.")
        record = dict(squads[squad_id])
        return self._enrich(record)

    # ---------------------------------------------------------------- cálculos
    def _enrich(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Anexa métricas derivadas (CAC, LTV, conversão, dias de vida)."""
        views = _int(record.get("views", 0))
        clicks = _int(record.get("clicks", 0))
        conversions = _int(record.get("conversions", 0))
        revenue = _num(record.get("revenue_usd", 0.0))
        cost = _num(record.get("cost_usd", 0.0))

        # CAC por cliente; se não houve vendas, CAC por 1 000 views.
        if conversions > 0:
            cac: Optional[float] = round(cost / conversions, 6)
            cac_basis = "per_conversion"
        elif views > 0:
            cac = round(cost / (views / 1000.0), 6)
            cac_basis = "per_1000_views"
        else:
            cac = None
            cac_basis = "sem_dados"

        ltv: Optional[float] = (
            round(revenue / conversions, 6) if conversions > 0 else None
        )
        retention = (
            round(conversions / clicks, 4) if clicks > 0 else 0.0
        )
        return {
            **record,
            "cac_usd": cac,
            "cac_basis": cac_basis,
            "ltv_usd": ltv,
            "conversion_rate": retention,
            "ctr": round(clicks / views, 4) if views > 0 else 0.0,
            "days_alive": round(days_since(record.get("created_at")), 2),
            "profit_usd": round(revenue - cost, 6),
            "roi": round((revenue - cost) / cost, 4) if cost > 0 else None,
            "lifetime_days": round(
                days_between(record.get("created_at"), record.get("closed_at")), 2
            ),
        }

    def portfolio(self) -> Dict[str, Any]:
        data = self._data()
        enriched = [self._enrich(dict(s)) for s in data["squads"].values()]
        enriched.sort(key=lambda s: str(s.get("created_at", "")), reverse=True)
        active = [s for s in enriched if s.get("status") == "active"]
        return {
            "squads": enriched,
            "summary": {
                "total": len(enriched),
                "active": len(active),
                "killed": len([s for s in enriched if s.get("status") == "killed"]),
                "paused": len([s for s in enriched if s.get("status") == "paused"]),
                "revenue_usd": round(sum(_num(s.get("revenue_usd")) for s in enriched), 6),
                "cost_usd": round(sum(_num(s.get("cost_usd")) for s in enriched), 6),
                "with_first_dollar": len(
                    [s for s in enriched if s.get("first_dollar_at")]
                ),
            },
            "kill_policy": data["kill_policy"],
            "generated_at": utcnow_iso(),
        }

    def evaluate_squad(self, squad_id: str) -> SquadVerdict:
        """Aplica a Regra de Abate a um esquadrão e devolve o veredicto."""
        record = self.get_squad(squad_id)
        policy = self.policy()
        reasons: List[str] = []
        decision = "keep"

        if record.get("status") != "active":
            return SquadVerdict(
                squad_id=squad_id,
                decision="keep",
                reasons=[f"esquadrão não está activo ({record.get('status')})"],
                ttfd_days=record.get("ttfd_days"),
                cac_usd=record.get("cac_usd"),
                ltv_usd=record.get("ltv_usd"),
                retention_rate=_num(record.get("conversion_rate")),
                days_alive=_num(record.get("days_alive")),
                evaluated_at=utcnow_iso(),
            )

        days_alive = _num(record.get("days_alive"))
        views = _int(record.get("views"))
        conversions = _int(record.get("conversions"))
        revenue = _num(record.get("revenue_usd"))
        first_dollar = record.get("first_dollar_at")

        # --- Regra 1: tracção mínima em 7 dias -------------------------------
        if days_alive >= _num(policy["min_traction_days"]):
            if views < _int(policy["min_views_in_traction_window"]):
                decision = "kill"
                reasons.append(
                    f"Sem tracção: {views} views em {days_alive:.0f} dias "
                    f"(mínimo {policy['min_views_in_traction_window']})."
                )

        # --- Regra 2: primeiro dólar em 21 dias ------------------------------
        if not first_dollar and days_alive >= _num(
            policy["first_dollar_deadline_days"]
        ):
            decision = "kill"
            reasons.append(
                f"Sem primeiro dólar em {days_alive:.0f} dias "
                f"(limite {policy['first_dollar_deadline_days']})."
            )

        # --- Regra 3: conversão cronicamente baixa --------------------------
        conversion_check_days = _num(policy.get("conversion_check_days", 14))
        conversion_rate = _num(record.get("conversion_rate"))
        if (
            days_alive >= conversion_check_days
            and views >= _int(policy["min_views_in_traction_window"])
            and conversion_rate < _num(policy["min_conversion_rate"])
            and revenue <= 0
        ):
            decision = "kill"
            reasons.append(
                f"Conversão {conversion_rate:.3%} abaixo do mínimo "
                f"{_num(policy['min_conversion_rate']):.3%} após {days_alive:.0f} dias."
            )

        # --- Sinais de atenção (não abatem) ---------------------------------
        if decision == "keep":
            if not first_dollar and days_alive >= _num(policy["first_dollar_deadline_days"]) * 0.6:
                decision = "watch"
                reasons.append(
                    f"Dias para o prazo do primeiro dólar: "
                    f"{max(0.0, _num(policy['first_dollar_deadline_days']) - days_alive):.0f}."
                )
            elif record.get("cac_usd") is not None and record.get("ltv_usd") is not None:
                if _num(record["ltv_usd"]) < _num(record["cac_usd"]):
                    decision = "watch"
                    reasons.append(
                        f"LTV (${_num(record['ltv_usd']):.2f}) abaixo do CAC "
                        f"(${_num(record['cac_usd']):.2f})."
                    )
            if not reasons:
                reasons.append("dentro dos parâmetros de saúde.")

        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            record_raw = data.setdefault("squads", {}).get(squad_id)
            if record_raw is not None:
                record_raw.setdefault("verdict_history", []).append(
                    {
                        "decision": decision,
                        "reasons": reasons,
                        "days_alive": round(days_alive, 2),
                        "cac_usd": record.get("cac_usd"),
                        "ltv_usd": record.get("ltv_usd"),
                        "conversion_rate": conversion_rate,
                        "at": utcnow_iso(),
                    }
                )
                record_raw["verdict_history"] = record_raw["verdict_history"][-40:]
                record_raw["last_verdict"] = decision
                record_raw["updated_at"] = utcnow_iso()

        return SquadVerdict(
            squad_id=squad_id,
            decision=decision,
            reasons=reasons,
            ttfd_days=record.get("ttfd_days"),
            cac_usd=record.get("cac_usd"),
            ltv_usd=record.get("ltv_usd"),
            retention_rate=conversion_rate,
            days_alive=days_alive,
            evaluated_at=utcnow_iso(),
        )

    def audit_all(self) -> Dict[str, Any]:
        """Avalia todo o portefólio; devolve veredictos e ordens de abate."""
        verdicts = [
            self.evaluate_squad(squad["id"])
            for squad in self.squads(status="active")
        ]
        to_kill = [v for v in verdicts if v.decision == "kill"]
        return {
            "verdicts": [v.to_dict() for v in verdicts],
            "kill_orders": [v.to_dict() for v in to_kill],
            "watched": [v.squad_id for v in verdicts if v.decision == "watch"],
            "evaluated_at": utcnow_iso(),
            "policy": self.policy(),
        }

    # -------------------------------------------------------------------- abate
    def kill_squad(
        self,
        squad_id: str,
        *,
        cause: str,
        approved_by: str = "accountant_auditor",
        lessons: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Abate a unidade, grava lição no cemitério e congela métricas."""
        record = self.get_squad(squad_id)
        if record.get("status") == "killed":
            return {"squad_id": squad_id, "status": "killed", "already": True}

        post_mortem = shared_memory.record_failure(
            title=record.get("name") or squad_id,
            niche=str(record.get("niche", "")),
            squad=str(record.get("type", "unknown")),
            death_cause=cause,
            lessons=lessons
            or [
                f"Métricas finais: views={record.get('views')}, "
                f"conversões={record.get('conversions')}, "
                f"receita=${_num(record.get('revenue_usd')):.2f}",
                "Reavaliar distribuição antes de produzir mais conteúdo.",
            ],
            tags=[
                str(record.get("type", "")),
                str(record.get("niche", ""))[:40],
                "kill_rule",
            ],
            metrics={
                "ttfd_days": record.get("ttfd_days"),
                "cac_usd": record.get("cac_usd"),
                "ltv_usd": record.get("ltv_usd"),
                "days_alive": record.get("days_alive"),
                "revenue_usd": record.get("revenue_usd"),
                "cost_usd": record.get("cost_usd"),
            },
            outcome="killed",
            created_by=approved_by,
        )

        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            target = data.setdefault("squads", {}).get(squad_id)
            if target is None:
                raise ValueError(f"Esquadrão {squad_id} desapareceu do cofre.")
            target["status"] = "killed"
            target["closed_at"] = utcnow_iso()
            target["kill_cause"] = cause[:600]
            target["killed_by"] = approved_by
            target["post_mortem_id"] = post_mortem.id
            target["updated_at"] = utcnow_iso()
            final = dict(target)

        event_bus.publish(
            "squad_killed",
            agent=approved_by,
            message=(
                f"Unidade abatida: {record.get('name')} ({squad_id}). "
                f"Causa: {cause[:160]}"
            ),
            level="critical",
            payload={
                "squad_id": squad_id,
                "post_mortem_id": post_mortem.id,
                "revenue_usd": record.get("revenue_usd"),
                "days_alive": record.get("days_alive"),
            },
        )
        log.critical("ESQUADRÃO ABATIDO %s — %s", squad_id, cause[:200])
        return {"squad_id": squad_id, "status": "killed", "post_mortem": post_mortem.to_dict(),
                "squad": final}

    def pause_squad(self, squad_id: str, reason: str) -> Dict[str, Any]:
        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            target = data.setdefault("squads", {}).get(squad_id)
            if target is None:
                raise ValueError(f"Esquadrão {squad_id} não existe.")
            target["status"] = "paused"
            target["paused_reason"] = reason[:400]
            target["updated_at"] = utcnow_iso()
            result = dict(target)
        event_bus.publish(
            "squad_paused", agent="accountant_auditor",
            message=f"Esquadrão {squad_id} pausado: {reason}", level="warning",
        )
        return result

    def financial_report(self) -> Dict[str, Any]:
        """Relatório consolidado para a GovernanceScreen do APK."""
        snapshot = cfo.snapshot()
        portfolio = self.portfolio()
        audit = cfo.audit()
        return {
            "treasury": snapshot,
            "portfolio": portfolio["summary"],
            "ledger_audit": audit,
            "monthly_revenue_usd": snapshot["monthly_revenue_usd"],
            "phase": snapshot["phase"],
            "tax_rate": snapshot["tax_reserve_rate"],
            "tax_reserve_usd": snapshot["balances_usd"]["tax_reserve_usd"],
            "generated_at": utcnow_iso(),
        }


accountant = AccountantAuditor()

__all__ = ["AccountantAuditor", "accountant", "SquadVerdict", "DEFAULT_KILL_POLICY"]
