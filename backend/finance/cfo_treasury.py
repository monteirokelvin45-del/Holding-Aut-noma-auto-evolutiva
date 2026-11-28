"""CFO da holding: provisão fiscal, motor de fases e cofres em USD.

Regras inegociáveis implementadas:
  * **5% da receita bruta** vai imediatamente para ``tax_reserve_usd`` e é
    intocável (nem o reinvestimento nem o Presidente lhe tocam).
  * O líquido divide-se pelos **3 cofres** segundo a fase de facturação:
      - Fase 1 (0 – 1 500 USD/mês): 10% bolso / 20% poupança / 70% reinvestimento
      - Fase 2 (1 500 – 15 000 USD/mês): 10% / 40% / 50%
      - Fase 3 (> 15 000 USD/mês): 50% / 30% / 20%
  * Só o ``reinvestment_pool_usd`` pode financiar despesas operacionais.
  * Cada movimento gera uma entrada em ``ledger_entries`` com os saldos
    resultantes (auditoria completa, nunca se apaga nada).
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..config.settings import settings
from ..core.clock import month_key, parse_iso, utcnow, utcnow_iso
from ..core.errors import ApprovalRequired, SpendCapExceeded, ValidationBlocked
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.security_guard import kill_switch
from ..core.vault_store import LEDGER, vault

log = get_logger("finance.cfo")

PHASE_2_THRESHOLD_USD = 1_500.0
PHASE_3_THRESHOLD_USD = 15_000.0
USD_MZN_REFERENCE = 63.50  # ~R$ 10 000 / R$ 100 000 nas notas do mandato

PHASES: Dict[int, Dict[str, Any]] = {
    1: {
        "name": "Lancamento",
        "min_usd": 0.0,
        "max_usd": PHASE_2_THRESHOLD_USD,
        "split": {"owner_pocket": 0.10, "savings_vault": 0.20, "reinvestment_pool": 0.70},
        "description": "Capital zero: quase tudo reinvestido para escalar.",
    },
    2: {
        "name": "Consolidacao",
        "min_usd": PHASE_2_THRESHOLD_USD,
        "max_usd": PHASE_3_THRESHOLD_USD,
        "split": {"owner_pocket": 0.10, "savings_vault": 0.40, "reinvestment_pool": 0.50},
        "description": "Cofre de segurança cresce; operação continua a escalar.",
    },
    3: {
        "name": "Colheita",
        "min_usd": PHASE_3_THRESHOLD_USD,
        "max_usd": None,
        "split": {"owner_pocket": 0.50, "savings_vault": 0.30, "reinvestment_pool": 0.20},
        "description": "Empresa madura: metade do líquido para o Presidente.",
    },
}


def _money(value: Any) -> float:
    try:
        return round(float(value) + 0.0, 6)
    except (TypeError, ValueError):
        return 0.0


def phase_for_monthly_revenue(monthly_revenue_usd: float) -> int:
    """Fase determinística a partir da facturação mensal em USD."""
    revenue = _money(monthly_revenue_usd)
    # Mandato: Fase 2 = [1 500,00 ; 15 000,00] inclusive; Fase 3 só acima.
    if revenue > PHASE_3_THRESHOLD_USD:
        return 3
    if revenue >= PHASE_2_THRESHOLD_USD:
        return 2
    return 1


def split_for_phase(phase: int) -> Dict[str, float]:
    return dict(PHASES.get(phase, PHASES[1])["split"])


@dataclass
class RevenueSplit:
    """Resultado de uma injecção de receita (usado pelo APK/Telegram)."""

    gross_usd: float
    tax_usd: float
    net_usd: float
    owner_pocket_usd: float
    savings_vault_usd: float
    reinvestment_pool_usd: float
    phase: int
    phase_changed: bool
    event_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gross_usd": self.gross_usd,
            "tax_usd": self.tax_usd,
            "net_usd": self.net_usd,
            "owner_pocket_usd": self.owner_pocket_usd,
            "savings_vault_usd": self.savings_vault_usd,
            "reinvestment_pool_usd": self.reinvestment_pool_usd,
            "phase": self.phase,
            "phase_changed": self.phase_changed,
            "event_id": self.event_id,
        }


class CFOTreasury:
    """Motor financeiro central. Toda a escrita passa por ``vault.transaction``."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ leitura
    def ledger(self) -> Dict[str, Any]:
        return vault.read(LEDGER)

    def monthly_revenue(self, ledger: Optional[Dict[str, Any]] = None) -> float:
        """Receita bruta acumulada nos últimos 30 dias (rolling)."""
        data = ledger or self.ledger()
        cutoff = utcnow() - timedelta(days=30)
        total = 0.0
        for event in data.get("revenue_events", []):
            moment = parse_iso(str(event.get("occurred_at", "")))
            if moment is None or moment < cutoff:
                continue
            total += _money(event.get("gross_usd", 0.0))
        return _money(total)

    def monthly_revenue_by_month(self) -> Dict[str, float]:
        data = self.ledger()
        buckets: Dict[str, float] = {}
        for event in data.get("revenue_events", []):
            moment = parse_iso(str(event.get("occurred_at", "")))
            if moment is None:
                continue
            key = month_key(moment)
            buckets[key] = _money(buckets.get(key, 0.0) + _money(event.get("gross_usd", 0.0)))
        return buckets

    def snapshot(self) -> Dict[str, Any]:
        """Estado completo da tesouraria para a aba TreasuryScreen."""
        data = self.ledger()
        phase = int(data.get("phase", 1))
        monthly = self.monthly_revenue(data)
        computed_phase = phase_for_monthly_revenue(monthly)
        fx_rate = _money((data.get("fx") or {}).get("usd_mzn", settings.usd_mzn_rate))
        if fx_rate <= 0:
            fx_rate = settings.usd_mzn_rate

        def to_mzn(value: float) -> float:
            return round(_money(value) * fx_rate, 2)

        balances = {
            "tax_reserve_usd": _money(data.get("tax_reserve_usd", 0.0)),
            "owner_pocket_usd": _money(data.get("owner_pocket_usd", 0.0)),
            "savings_vault_usd": _money(data.get("savings_vault_usd", 0.0)),
            "reinvestment_pool_usd": _money(data.get("reinvestment_pool_usd", 0.0)),
        }
        return {
            "phase": phase,
            "phase_meta": PHASES.get(phase, PHASES[1]),
            "computed_phase": computed_phase,
            "split": split_for_phase(phase),
            "balances_usd": balances,
            "balances_mzn": {key: to_mzn(value) for key, value in balances.items()},
            "total_assets_usd": _money(sum(balances.values())),
            "gross_revenue_usd": _money(data.get("gross_revenue_usd", 0.0)),
            "net_revenue_usd": _money(data.get("net_revenue_usd", 0.0)),
            "spent_from_reinvestment_usd": _money(
                data.get("spent_from_reinvestment_usd", 0.0)
            ),
            "paid_out_usd": _money(data.get("paid_out_usd", 0.0)),
            "monthly_revenue_usd": monthly,
            "phase_thresholds_usd": {
                "phase_2": PHASE_2_THRESHOLD_USD,
                "phase_3": PHASE_3_THRESHOLD_USD,
            },
            "fx": {"usd_mzn": fx_rate, "source": (data.get("fx") or {}).get("source", "static")},
            "distributable_owner_usd": balances["owner_pocket_usd"],
            "tax_reserve_rate": _money(
                (data.get("current_split") or {}).get(
                    "tax_reserve_rate", settings.tax_reserve_rate
                )
            ),
            "generated_at": utcnow_iso(),
        }

    # ------------------------------------------------------------------ receita
    def record_revenue(
        self,
        amount_usd: float,
        *,
        source: str,
        squad: str = "unknown",
        description: str = "",
        currency: str = "USD",
        external_ref: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RevenueSplit:
        """Regista receita bruta e aplica provisão fiscal + divisão por fase.

        Operação atómica: ou todos os cofres são actualizados, ou nenhum.
        """
        kill_switch.ensure_operational("registo de receita")
        gross = _money(amount_usd)
        if gross <= 0:
            raise ValidationBlocked(
                "A receita deve ser positiva.",
                details={"amount_usd": amount_usd, "currency": currency},
            )
        if currency.upper() not in {"USD", "EUR"}:
            raise ValidationBlocked(
                "Moeda de receita não suportada (usar USD ou EUR).",
                details={"currency": currency},
            )
        if currency.upper() == "EUR":
            # Conversão conservadora de reporte; a receita real em EUR fica no evento.
            gross = _money(gross * 1.08)

        with self._lock, vault.transaction(LEDGER) as data:
            data.setdefault("revenue_events", [])
            data.setdefault("ledger_entries", [])

            # Idempotência por referência externa: a mesma cobrança PayPal
            # (ou o mesmo Payout ID) não pode entrar duas vezes no cofre.
            # Acontece a sério — um retry de rede repete a chamada, e sem esta
            # guarda o cofre passa a achar que recebeu o dobro. Devolve o
            # evento existente em vez de falhar: quem repetiu já tem o
            # resultado que queria.
            referencias = (
                {"", "none", "-", "null", "n/a"} if not external_ref else set()
            )
            if external_ref and external_ref not in referencias:
                for anterior in data["revenue_events"]:
                    if str(anterior.get("external_ref") or "") == external_ref:
                        log.warning(
                            "Receita já registada para ref=%s (evento %s): "
                            "ignorado o duplicado de $%.2f.",
                            external_ref,
                            anterior.get("id"),
                            gross,
                        )
                        return RevenueSplit(
                            gross_usd=_money(anterior.get("gross_usd", gross)),
                            tax_usd=_money(anterior.get("tax_usd", 0.0)),
                            net_usd=_money(anterior.get("net_usd", 0.0)),
                            owner_pocket_usd=0.0,
                            savings_vault_usd=0.0,
                            reinvestment_pool_usd=0.0,
                            phase=int(anterior.get("phase_at_event", 1)),
                            phase_changed=False,
                            event_id=str(anterior.get("id", "")),
                        )
            data.setdefault("phase_history", [])
            data.setdefault("current_split", {})
            data["current_split"]["tax_reserve_rate"] = settings.tax_reserve_rate

            tax_rate = float(
                data["current_split"].get("tax_reserve_rate", settings.tax_reserve_rate)
            )
            tax = _money(gross * tax_rate)
            net = _money(gross - tax)

            previous_phase = int(data.get("phase", 1))
            monthly_after = self.monthly_revenue(data) + gross
            phase = phase_for_monthly_revenue(monthly_after)
            split = split_for_phase(phase)

            owner = _money(net * split["owner_pocket"])
            savings = _money(net * split["savings_vault"])
            reinvest = _money(net - owner - savings)  # absorve arredondamentos

            event_id = "rev_" + hashlib.sha1(
                f"{source}|{squad}|{gross}|{utcnow_iso()}".encode("utf-8")
            ).hexdigest()[:12]

            data["gross_revenue_usd"] = _money(data.get("gross_revenue_usd", 0.0) + gross)
            data["net_revenue_usd"] = _money(data.get("net_revenue_usd", 0.0) + net)
            data["tax_reserve_usd"] = _money(data.get("tax_reserve_usd", 0.0) + tax)
            data["owner_pocket_usd"] = _money(data.get("owner_pocket_usd", 0.0) + owner)
            data["savings_vault_usd"] = _money(data.get("savings_vault_usd", 0.0) + savings)
            data["reinvestment_pool_usd"] = _money(
                data.get("reinvestment_pool_usd", 0.0) + reinvest
            )

            phase_changed = phase != previous_phase
            data["phase"] = phase
            data["current_split"] = {
                **split,
                "tax_reserve_rate": tax_rate,
            }
            if phase_changed:
                data["phase_history"].append(
                    {
                        "phase": phase,
                        "entered_at": utcnow_iso(),
                        "monthly_revenue_usd": _money(monthly_after),
                        "from_phase": previous_phase,
                    }
                )

            data["revenue_events"].append(
                {
                    "id": event_id,
                    "gross_usd": gross,
                    "net_usd": net,
                    "tax_usd": tax,
                    "currency": currency.upper(),
                    "source": source[:80],
                    "squad": squad[:80],
                    "description": description[:300],
                    "external_ref": external_ref[:120],
                    "metadata": dict(metadata or {}),
                    "occurred_at": utcnow_iso(),
                    "phase_at_event": phase,
                    "split": split,
                }
            )
            data["ledger_entries"].append(
                {
                    "id": "led_" + event_id,
                    "type": "revenue",
                    "amount_usd": gross,
                    "net_usd": net,
                    "tax_usd": tax,
                    "squad": squad,
                    "source": source,
                    "phase": phase,
                    "balances_after": {
                        "tax_reserve_usd": data["tax_reserve_usd"],
                        "owner_pocket_usd": data["owner_pocket_usd"],
                        "savings_vault_usd": data["savings_vault_usd"],
                        "reinvestment_pool_usd": data["reinvestment_pool_usd"],
                    },
                    "at": utcnow_iso(),
                }
            )

        event_bus.publish(
            "revenue_recorded",
            agent="cfo_treasury",
            message=(
                f"Receita ${gross:.2f} de {source} ({squad}). "
                f"Imposto ${tax:.2f}; fase {phase}."
            ),
            payload={
                "event_id": event_id,
                "gross_usd": gross,
                "tax_usd": tax,
                "phase": phase,
                "split": split,
            },
        )
        log.warning(
            "Receita registada: $%.2f (%s/%s) -> tax=%.2f owner=%.2f savings=%.2f reinvest=%.2f fase=%d",
            gross, source, squad, tax, owner, savings, reinvest, phase,
        )
        return RevenueSplit(
            gross_usd=gross,
            tax_usd=tax,
            net_usd=net,
            owner_pocket_usd=owner,
            savings_vault_usd=savings,
            reinvestment_pool_usd=reinvest,
            phase=phase,
            phase_changed=phase_changed,
            event_id=event_id,
        )

    # ------------------------------------------------------------------ despesa
    def spend(
        self,
        amount_usd: float,
        *,
        agent_id: str,
        purpose: str,
        approved_by: Optional[str] = None,
        approval_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Debita o fundo de reinvestimento depois das travas de gasto.

        A autorização (``SpendGuard``) é verificada *de novo* aqui para que
        nenhum caminho de código possa saltar o tecto de $0.00.
        """
        kill_switch.ensure_operational(f"despesa de {agent_id}")
        amount = _money(amount_usd)
        if amount <= 0:
            raise ValidationBlocked("Despesa deve ser positiva.")

        # Import tardio para evitar ciclo (security_guard não conhece o CFO).
        from ..core.security_guard import SpendGuard

        guard = SpendGuard(ledger_reader=self.ledger)
        try:
            guard.authorize_spend(
                agent_id,
                amount,
                purpose,
                require_approval_above_threshold=approved_by is None,
            )
        except ApprovalRequired as exc:
            if not (approved_by and approval_id):
                raise
            log.info(
                "Despesa aprovada por %s (aprovação %s) acima do limiar.",
                approved_by, approval_id,
            )

        with self._lock, vault.transaction(LEDGER) as data:
            pool = _money(data.get("reinvestment_pool_usd", 0.0))
            if amount > pool + 1e-9:
                raise SpendCapExceeded(
                    "Fundo de reinvestimento insuficiente.",
                    details={
                        "requested_usd": amount,
                        "reinvestment_pool_usd": pool,
                        "agent_id": agent_id,
                    },
                )
            data["reinvestment_pool_usd"] = _money(pool - amount)
            data["spent_from_reinvestment_usd"] = _money(
                data.get("spent_from_reinvestment_usd", 0.0) + amount
            )
            day = utcnow().strftime("%Y-%m-%d")
            daily = data.setdefault("daily_spend_usd", {})
            daily[day] = _money(daily.get(day, 0.0) + amount)
            agent_spend = data.setdefault("agent_daily_spend_usd", {})
            agent_day = agent_spend.setdefault(agent_id, {})
            agent_day[day] = _money(agent_day.get(day, 0.0) + amount)
            entry = {
                "id": "led_spend_" + hashlib.sha1(
                    f"{agent_id}|{amount}|{utcnow_iso()}".encode("utf-8")
                ).hexdigest()[:12],
                "type": "spend",
                "amount_usd": amount,
                "agent_id": agent_id,
                "purpose": purpose[:300],
                "approved_by": approved_by or "spend_guard",
                "approval_id": approval_id,
                "metadata": dict(metadata or {}),
                "balances_after": {
                    "reinvestment_pool_usd": data["reinvestment_pool_usd"],
                    "tax_reserve_usd": _money(data.get("tax_reserve_usd", 0.0)),
                },
                "at": utcnow_iso(),
            }
            data["ledger_entries"].append(entry)
            pool_after = data["reinvestment_pool_usd"]

        event_bus.publish(
            "spend_executed",
            agent=agent_id,
            message=f"Despesa ${amount:.2f} — {purpose} (fundo: ${pool_after:.2f})",
            payload={"amount_usd": amount, "purpose": purpose, "pool_after_usd": pool_after},
        )
        return {"entry": entry, "reinvestment_pool_usd": pool_after}

    # ------------------------------------------------------------------- saque
    def reserve_for_payout(
        self, amount_usd: float, *, channel: str, destination: str = ""
    ) -> Dict[str, Any]:
        """Move valor do bolso do Presidente para um saque pendente.

        Não envia dinheiro: apenas debita o bolso e cria a intenção, que o
        ``payout_bridge`` executa só depois da aprovação biométrica.
        """
        kill_switch.ensure_operational("saque")
        amount = _money(amount_usd)
        if amount <= 0:
            raise ValidationBlocked("O saque deve ser positivo.")
        with self._lock, vault.transaction(LEDGER) as data:
            pocket = _money(data.get("owner_pocket_usd", 0.0))
            if amount > pocket + 1e-9:
                raise ValidationBlocked(
                    "Saldo do bolso do Presidente insuficiente para o saque.",
                    details={"requested_usd": amount, "owner_pocket_usd": pocket},
                )
            data["owner_pocket_usd"] = _money(pocket - amount)
            payout_id = "pay_" + hashlib.sha1(
                f"{amount}|{channel}|{utcnow_iso()}".encode("utf-8")
            ).hexdigest()[:12]
            payout = {
                "id": payout_id,
                "status": "reserved",
                "amount_usd": amount,
                "channel": channel,
                "destination": destination,
                "created_at": utcnow_iso(),
                "fx_rate": _money((data.get("fx") or {}).get("usd_mzn", settings.usd_mzn_rate)),
                "amount_mzn": round(amount * _money((data.get("fx") or {}).get("usd_mzn", settings.usd_mzn_rate)), 2),
            }
            data.setdefault("payouts", []).append(payout)
            data["ledger_entries"].append(
                {
                    "id": "led_" + payout_id,
                    "type": "payout_reserved",
                    "amount_usd": amount,
                    "channel": channel,
                    "at": utcnow_iso(),
                }
            )
            pocket_after = data["owner_pocket_usd"]
        log.warning("Saque reservado %s: $%.2f via %s", payout_id, amount, channel)
        return {"payout": payout, "owner_pocket_usd": pocket_after}

    def finalize_payout(
        self, payout_id: str, *, status: str, provider_ref: str = "", note: str = ""
    ) -> Dict[str, Any]:
        """Fecha um saque (executado ou revertido para o bolso)."""
        with self._lock, vault.transaction(LEDGER) as data:
            target = None
            for payout in data.get("payouts", []):
                if payout.get("id") == payout_id:
                    target = payout
                    break
            if target is None:
                raise ValidationBlocked(f"Saque {payout_id} não existe.")
            if target.get("status") not in {"reserved", "pending"}:
                raise ValidationBlocked(
                    f"Saque {payout_id} já está '{target.get('status')}'."
                )
            amount = _money(target.get("amount_usd", 0.0))
            if status == "executed":
                target["status"] = "executed"
                data["paid_out_usd"] = _money(data.get("paid_out_usd", 0.0) + amount)
            elif status == "failed":
                target["status"] = "failed"
                data["owner_pocket_usd"] = _money(
                    data.get("owner_pocket_usd", 0.0) + amount
                )
            else:
                raise ValidationBlocked(f"Estado de saque inválido: {status}")
            target["provider_ref"] = provider_ref
            target["note"] = note[:400]
            target["finalized_at"] = utcnow_iso()
            data["ledger_entries"].append(
                {
                    "id": "led_final_" + payout_id,
                    "type": f"payout_{status}",
                    "amount_usd": amount,
                    "channel": target.get("channel"),
                    "at": utcnow_iso(),
                }
            )
            result = dict(target)
        event_bus.publish(
            "payout_executed" if status == "executed" else "payout_failed",
            agent="cfo_treasury",
            message=f"Saque {payout_id} {status}: ${amount:.2f}",
            payload=result,
        )
        return result

    # ------------------------------------------------------------- fase / fx
    def recompute_phase(self) -> Dict[str, Any]:
        """Reavalia a fase com base na facturação dos últimos 30 dias."""
        with self._lock, vault.transaction(LEDGER) as data:
            monthly = self.monthly_revenue(data)
            previous = int(data.get("phase", 1))
            phase = phase_for_monthly_revenue(monthly)
            data["phase"] = phase
            data["current_split"] = {
                **split_for_phase(phase),
                "tax_reserve_rate": float(
                    (data.get("current_split") or {}).get(
                        "tax_reserve_rate", settings.tax_reserve_rate
                    )
                ),
            }
            changed = phase != previous
            if changed:
                data.setdefault("phase_history", []).append(
                    {
                        "phase": phase,
                        "entered_at": utcnow_iso(),
                        "monthly_revenue_usd": monthly,
                        "from_phase": previous,
                    }
                )
        if changed:
            event_bus.publish(
                "phase_changed",
                agent="cfo_treasury",
                message=(
                    f"Fase {previous} -> {phase} ({PHASES[phase]['name']}): "
                    f"facturação mensal ${monthly:.2f}"
                ),
                level="warning",
                payload={"from": previous, "to": phase, "monthly_usd": monthly},
            )
        return {
            "phase": phase,
            "previous_phase": previous,
            "changed": changed,
            "monthly_revenue_usd": monthly,
            "split": split_for_phase(phase),
        }

    def update_fx_rate(self, usd_mzn: float, source: str = "manual") -> Dict[str, Any]:
        """Actualiza a taxa USD→MZN usada nos saques e no APK."""
        rate = _money(usd_mzn)
        if rate <= 0:
            raise ValidationBlocked("Taxa de câmbio deve ser positiva.")
        with self._lock, vault.transaction(LEDGER) as data:
            data["fx"] = {
                "usd_mzn": rate,
                "source": source[:60],
                "updated_at": utcnow_iso(),
            }
        log.info("Taxa USD/MZN actualizada para %.4f (%s).", rate, source)
        return {"usd_mzn": rate, "source": source, "updated_at": utcnow_iso()}

    def process_incoming_revenue(
        self, gross_usd: float, squad_id: str, *, source: str = "external"
    ) -> RevenueSplit:
        """Nome do mandato para ``record_revenue`` (receita de um esquadrão).

        Mantém a semântica exacta: provisão fiscal de 5%, líquido somado à
        facturação mensal, multiplicadores da fase escolhidos pelo total
        mensal e gravação atómica do ledger (``vault.transaction``).
        """
        return self.record_revenue(
            gross_usd,
            source=source,
            squad=squad_id or "unknown",
            description=f"Receita atribuída ao esquadrão {squad_id or 'unknown'}",
        )

    def convert_usd_to_mzn(self, amount_usd: float) -> Dict[str, Any]:
        data = self.ledger()
        rate = _money((data.get("fx") or {}).get("usd_mzn", settings.usd_mzn_rate))
        if rate <= 0:
            rate = settings.usd_mzn_rate
        amount = _money(amount_usd)
        return {
            "usd": amount,
            "rate": rate,
            "mzn": round(amount * rate, 2),
            "reference_note": (
                f"Referência interna: 1 USD ≈ {rate:.2f} MZN "
                f"(fase 2 ≈ $1.500 ≈ MZN {PHASE_2_THRESHOLD_USD * rate:,.0f}; "
                f"fase 3 ≈ $15.000 ≈ MZN {PHASE_3_THRESHOLD_USD * rate:,.0f}). "
                f"Nota do mandato usa R$ {USD_MZN_REFERENCE:.2f} como âncora."
            ),
        }

    def ledger_history(self, limit: int = 100, entry_type: Optional[str] = None) -> List[Dict[str, Any]]:
        entries = self.ledger().get("ledger_entries", [])
        if entry_type:
            entries = [e for e in entries if e.get("type") == entry_type]
        return list(reversed(entries))[:limit]

    def audit(self) -> Dict[str, Any]:
        """Verificação de integridade aritmética do ledger (usada nos testes)."""
        data = self.ledger()
        gross = _money(data.get("gross_revenue_usd", 0.0))
        tax = _money(data.get("tax_reserve_usd", 0.0))
        owner = _money(data.get("owner_pocket_usd", 0.0))
        savings = _money(data.get("savings_vault_usd", 0.0))
        reinvest = _money(data.get("reinvestment_pool_usd", 0.0))
        spent = _money(data.get("spent_from_reinvestment_usd", 0.0))
        paid_out = _money(data.get("paid_out_usd", 0.0))

        inflow_net = _money(sum(
            _money(event.get("net_usd", 0.0)) for event in data.get("revenue_events", [])
        ))
        outflow = _money(spent + paid_out)
        vaults = _money(owner + savings + reinvest)
        expected = _money(inflow_net - outflow)
        # O bolso é debitado no momento da reserva do saque; o valor fica
        # "em trânsito" até finalizar, pelo que toleramos $0.01 por evento.
        reserved_in_flight = _money(sum(
            _money(p.get("amount_usd", 0.0))
            for p in data.get("payouts", [])
            if p.get("status") in {"reserved", "pending"}
        ))
        expected_with_float = _money(expected - reserved_in_flight)
        delta = _money(vaults - expected_with_float)
        return {
            "gross_usd": gross,
            "tax_reserve_usd": tax,
            "vaults_usd": vaults,
            "net_inflow_usd": inflow_net,
            "outflow_usd": outflow,
            "in_flight_payouts_usd": reserved_in_flight,
            "expected_vaults_usd": expected_with_float,
            "delta_usd": delta,
            "balanced": abs(delta) <= 0.02,
            "checked_at": utcnow_iso(),
        }


cfo = CFOTreasury()

__all__ = [
    "CFOTreasury",
    "cfo",
    "RevenueSplit",
    "PHASES",
    "PHASE_2_THRESHOLD_USD",
    "PHASE_3_THRESHOLD_USD",
    "phase_for_monthly_revenue",
    "split_for_phase",
]
