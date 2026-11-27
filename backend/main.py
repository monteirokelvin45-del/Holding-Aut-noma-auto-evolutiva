"""API da Automaton Holding Corp.

Serve o APK Android (REST + WebSocket), o bot Telegram (webhook) e o ciclo
autónomo (GitHub Actions). Todas as rotas de escrita exigem o header
``X-Automaton-Token`` quando ``API_ACCESS_TOKEN`` está definido; decisões
financeiras exigem ainda ``biometric_verified=true`` vindo do APK.

Execução:
    uvicorn backend.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config.settings import settings
from .core.approvals import approval_gate
from .core.clock import utcnow_iso
from .core.crew_bridge import agent_registry, crew_bridge
from .core.errors import (
    ApprovalRequired,
    AutomatonError,
    ConfigurationError,
    ExternalServiceError,
    KillSwitchActive,
    NotFound,
    SpendCapExceeded,
    ValidationBlocked,
)
from .core.llm_router import LLMRequest, llm_router
from .core.llm_smoke import run_probe
from .core.logger import get_logger
from .core.observability_langfuse import (
    install_observability,
    llm_tracer,
    telegram_client,
)
from .core.runtime_bus import event_bus
from .core.security_guard import kill_switch, prompt_shield
from .core.shared_memory import shared_memory
from .core.vault_store import PENDING_APPROVALS, SQUAD_METRICS, VAULT_FILES, vault
from .finance.cfo_treasury import cfo
from .finance.payout_bridge import PayoutRequest, payout_bridge
from .governance.accountant_auditor import accountant
from .governance.ceo_orchestrator import ceo
from .governance.hr_department import hr_department
from .governance.legal_compliance import legal_department
from .squads.infoproducts_affiliate_squad import infoproducts_squad
from .squads.micro_saas_dev_squad import micro_saas_squad
from .squads.scout_hunter import scout_hunter
from .squads.video_factory_squad import video_factory
from .validation.demand_validator import demand_validator
from .validation.eval_judge import eval_judge

log = get_logger("api")

STARTED_AT = utcnow_iso()


# ==========================================================================
# Ciclo de vida
# ==========================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranque/paragem: liga o bus ao loop, observabilidade e Telegram."""
    loop = asyncio.get_running_loop()
    event_bus.bind_loop(loop)
    installed = install_observability(llm_router)
    log.info("Observabilidade instalada: %s", installed)

    if settings.telegram_bot_token and settings.telegram_allowed_chat_ids:
        _register_telegram_commands()
        if settings.telegram_polling_enabled:
            telegram_client.start_polling()

    log.warning(
        "Automaton Holding API online | env=%s | cap diário=$%.2f | kill=%s",
        settings.app_env,
        settings.daily_spend_cap_usd,
        kill_switch.status()["engaged"],
    )
    event_bus.publish(
        "system_started",
        agent="api",
        message="Backend da holding iniciado.",
        payload={"env": settings.app_env, "daily_cap_usd": settings.daily_spend_cap_usd},
    )
    try:
        yield
    finally:
        telegram_client.stop_polling()
        llm_tracer.flush()
        log.info("Backend encerrado.")


app = FastAPI(
    title="Automaton Holding Corp API",
    description=(
        "Holding e venture builder autónoma de IA. Capital inicial $0.00, "
        "supervisão humana por APK Android e Telegram."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================================================
# Autenticação
# ==========================================================================
def require_token(x_automaton_token: Optional[str] = Header(default=None)) -> None:
    """Valida o token do APK quando configurado (fail-open só em dev)."""
    expected = settings.api_access_token
    if not expected:
        if settings.is_production and settings.app_env != "development":
            log.warning(
                "API_ACCESS_TOKEN não configurado: rotas de escrita abertas. "
                "Definir o token em produção."
            )
        return
    if not x_automaton_token or x_automaton_token != expected:
        raise HTTPException(status_code=401, detail="Token do APK inválido ou ausente.")


def require_biometric(authorized: bool) -> None:
    if not authorized:
        raise ApprovalRequired(
            "Verificação biométrica obrigatória para esta operação.",
            details={"hint": "Confirmar no APK com impressão digital."},
        )


# ==========================================================================
# Mapeamento de erros
# ==========================================================================
@app.exception_handler(AutomatonError)
async def automaton_error_handler(request: Request, exc: AutomatonError) -> JSONResponse:
    log.warning("Erro de negócio em %s: %s (%s)", request.url.path, exc.message, exc.code)
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "INVALID_INPUT", "message": str(exc), "details": {}},
    )


# ==========================================================================
# Modelos de pedido
# ==========================================================================
class DecideRequest(BaseModel):
    approve: bool
    biometric_verified: bool = False
    note: str = Field(default="", max_length=600)


class KillRequest(BaseModel):
    reason: str = Field(default="paragem de emergência", max_length=400)


class SpendRequest(BaseModel):
    amount_usd: float = Field(gt=0)
    agent_id: str = Field(default="ceo", max_length=80)
    purpose: str = Field(max_length=300)
    approval_id: Optional[str] = None


class PayoutRequestBody(BaseModel):
    amount_usd: float = Field(gt=0)
    channel: str = Field(pattern="^(mpesa|emola|paypal|wise)$")
    approval_id: str
    destination: str = ""
    note: str = ""
    currency_target: str = "MZN"


class PayoutReserveRequest(BaseModel):
    amount_usd: float = Field(gt=0)
    channel: str = Field(pattern="^(mpesa|emola|paypal|wise)$")
    destination: str = ""
    reason: str = ""


class RevenueRequest(BaseModel):
    amount_usd: float = Field(gt=0)
    source: str = Field(max_length=80)
    squad: str = Field(default="unknown", max_length=80)
    description: str = ""
    currency: str = "USD"
    external_ref: str = ""


class ActivityRequest(BaseModel):
    views: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    conversions: int = Field(default=0, ge=0)
    revenue_usd: float = Field(default=0.0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    notes: str = ""


class SmokeTestRequest(BaseModel):
    idea: str = Field(min_length=8, max_length=600)
    niche: str = ""
    squad_type: str = "generic"
    hypothesis: str = ""
    cta: str = "Quero entrar na lista VIP"
    price_anchor_usd: float = Field(default=0.0, ge=0)
    min_clicks: Optional[int] = None
    min_conversion_rate: Optional[float] = None
    window_days: Optional[int] = None


class SmokeEventRequest(BaseModel):
    event: str = "click"
    source: str = "direct"
    value_usd: float = 0.0
    count: int = Field(default=1, ge=1, le=10000)


class CycleRequest(BaseModel):
    force_scan: bool = False
    produce: bool = True


class VideoRequest(BaseModel):
    idea: str
    niche: str = ""
    smoke_test_id: str
    duration_target: int = 40
    language: str = "pt"


class InfoproductRequest(BaseModel):
    idea: str
    niche: str = ""
    smoke_test_id: str
    product_type: str = "ebook"
    price_usd: float = 12.0


class MicroSaasRequest(BaseModel):
    idea: str
    niche: str = ""
    smoke_test_id: str
    price_usd: float = 9.0
    allow_code_execution: bool = False


class CrewTaskRequest(BaseModel):
    task_id: str = Field(min_length=3, max_length=80)
    inputs: Dict[str, Any] = Field(default_factory=dict)
    require_json: bool = False


class CrewPipelineRequest(BaseModel):
    task_ids: Optional[List[str]] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)


class JudgeRequest(BaseModel):
    content: str
    content_type: str = "copy"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    prompt: str
    task: str = "general"
    json_mode: bool = False


class FxRequest(BaseModel):
    usd_mzn: float = Field(gt=0)


class SquadKillRequest(BaseModel):
    cause: str = Field(min_length=4, max_length=600)
    biometric_verified: bool = False


# ==========================================================================
# Saúde e estado
# ==========================================================================
@app.get("/health", tags=["sistema"])
def health() -> Dict[str, Any]:
    """Estado técnico (sem segredos) — usado pela monitorização e pelo APK."""
    return {
        "status": "ok",
        "service": "automaton-holding-api",
        "version": "1.0.0",
        "env": settings.app_env,
        "started_at": STARTED_AT,
        "now": utcnow_iso(),
        "kill_switch": kill_switch.status(),
        "vault": vault.health(),
        "integrations": settings.configured_secrets(),
        "budget": {
            "daily_cap_usd": settings.daily_spend_cap_usd,
            "per_agent_cap_usd": settings.per_agent_daily_cap_usd,
            "approval_threshold_usd": settings.approval_threshold_usd,
        },
        "llm": {
            provider: {
                "configured": data["configured"],
                "available": data["available"],
                "reason": data["reason"],
                "requests_today": data["usage"]["requests_today"],
            }
            for provider, data in llm_router.status()["providers"].items()
        },
        "websocket_clients": event_bus.subscriber_count(),
        "telegram": {
            "enabled": telegram_client.enabled,
            "allowed_chats": len(telegram_client.allowed),
        },
    }


@app.get("/system/status", tags=["sistema"])
def system_status() -> Dict[str, Any]:
    """Dashboard consolidado (topo do APK)."""
    return ceo.status_snapshot()


@app.get("/system/config", tags=["sistema"])
def system_config() -> Dict[str, Any]:
    """Configuração pública (sem segredos) para o APK ajustar a UI."""
    return {
        **settings.safe_summary(),
        "thresholds": {
            "eval_min_score": settings.eval_min_score,
            "smoke_min_clicks": settings.smoke_min_clicks,
            "smoke_min_conversion_rate": settings.smoke_min_conversion_rate,
            "smoke_window_days": [settings.smoke_min_days, settings.smoke_max_days],
            "video": {
                "width": settings.video_target_width,
                "height": settings.video_target_height,
                "fps": settings.video_target_fps,
                "max_seconds": settings.video_max_duration_seconds,
            },
        },
        "server_now": utcnow_iso(),
    }


# ==========================================================================
# Kill switch
# ==========================================================================
@app.post("/kill-switch/engage", tags=["segurança"], dependencies=[Depends(require_token)])
def engage_kill_switch(payload: KillRequest) -> Dict[str, Any]:
    """Paragem de emergência (< 1s): congela agentes, ciclos e gastos."""
    state = kill_switch.engage(payload.reason, actor="president_apk")
    event_bus.publish(
        "kill_switch",
        agent="security",
        message=f"KILL SWITCH ACCIONADO: {payload.reason}",
        level="critical",
        payload=state,
    )
    return {"ok": True, "state": state}


@app.post("/kill-switch/release", tags=["segurança"], dependencies=[Depends(require_token)])
def release_kill_switch() -> Dict[str, Any]:
    state = kill_switch.release(actor="president_apk")
    event_bus.publish(
        "kill_switch_released",
        agent="security",
        message="Kill switch libertado: operações retomadas.",
        payload=state,
    )
    return {"ok": True, "state": state}


@app.get("/kill-switch", tags=["segurança"])
def kill_switch_status() -> Dict[str, Any]:
    return kill_switch.status()


# ==========================================================================
# Aprovações
# ==========================================================================
@app.get("/approvals", tags=["aprovações"])
def list_approvals(
    status: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
) -> Dict[str, Any]:
    return {
        "items": approval_gate.list_items(status=status, kind=kind, limit=limit),
        "deck": approval_gate.pending_batches(),
    }


@app.post("/approvals", tags=["aprovações"], dependencies=[Depends(require_token)])
def create_approval(
    title: str = Body(..., embed=True),
    kind: str = Body(default="custom", embed=True),
    amount_usd: float = Body(default=0.0, embed=True),
    reason: str = Body(default="", embed=True),
    metadata: Dict[str, Any] = Body(default_factory=dict, embed=True),
) -> Dict[str, Any]:
    approval_id = approval_gate.request(
        title=title,
        kind=kind,
        amount_usd=amount_usd,
        reason=reason,
        requested_by="president_apk",
        metadata=metadata,
    )
    return {"ok": True, "approval_id": approval_id}


@app.post("/approvals/{approval_id}/decide", tags=["aprovações"],
          dependencies=[Depends(require_token)])
def decide_approval(approval_id: str, payload: DecideRequest) -> Dict[str, Any]:
    """Aprova/veta com biometria. Executa automaticamente se aprovado."""
    updated = approval_gate.decide(
        approval_id,
        approve=payload.approve,
        decided_by="president_apk",
        biometric_verified=payload.biometric_verified,
        note=payload.note,
        require_biometric=True,
    )
    execution: Optional[Dict[str, Any]] = None
    if payload.approve:
        execution = ceo.execute_approved(approval_id)
    return {"ok": True, "item": updated, "execution": execution}


@app.get("/approvals/{approval_id}", tags=["aprovações"])
def get_approval(approval_id: str) -> Dict[str, Any]:
    return approval_gate.get(approval_id).to_dict()


# ==========================================================================
# Tesouraria
# ==========================================================================
@app.get("/treasury", tags=["tesouraria"])
def treasury() -> Dict[str, Any]:
    return {
        "snapshot": cfo.snapshot(),
        "channels": payout_bridge.channels_status(),
        "payouts": list(reversed(cfo.ledger().get("payouts", [])))[:30],
        "audit": cfo.audit(),
    }


@app.get("/treasury/ledger", tags=["tesouraria"])
def treasury_ledger(
    limit: int = Query(default=100, ge=1, le=500),
    entry_type: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    return {
        "entries": cfo.ledger_history(limit=limit, entry_type=entry_type),
        "history_by_month": cfo.monthly_revenue_by_month(),
        "fx": cfo.ledger().get("fx", {}),
    }


@app.post("/treasury/revenue", tags=["tesouraria"], dependencies=[Depends(require_token)])
def record_revenue(payload: RevenueRequest) -> Dict[str, Any]:
    split = cfo.record_revenue(
        payload.amount_usd,
        source=payload.source,
        squad=payload.squad,
        description=payload.description,
        currency=payload.currency,
        external_ref=payload.external_ref,
    )
    return {"ok": True, "split": split.to_dict(), "snapshot": cfo.snapshot()}


@app.post("/treasury/spend", tags=["tesouraria"], dependencies=[Depends(require_token)])
def record_spend(payload: SpendRequest) -> Dict[str, Any]:
    result = cfo.spend(
        payload.amount_usd,
        agent_id=payload.agent_id,
        purpose=payload.purpose,
        approved_by="president_apk" if payload.approval_id else None,
        approval_id=payload.approval_id,
    )
    return {"ok": True, **result}


@app.post("/treasury/payouts", tags=["tesouraria"], dependencies=[Depends(require_token)])
def request_payout(payload: PayoutRequestBody) -> Dict[str, Any]:
    result = payout_bridge.request_payout(
        PayoutRequest(
            amount_usd=payload.amount_usd,
            channel=payload.channel,
            approval_id=payload.approval_id,
            destination=payload.destination,
            note=payload.note,
            currency_target=payload.currency_target,
        )
    )
    return {"ok": result.ok, "result": result.to_dict()}


@app.post("/treasury/payouts/reserve", tags=["tesouraria"],
          dependencies=[Depends(require_token)])
def reserve_payout(payload: PayoutReserveRequest) -> Dict[str, Any]:
    """Cria o pedido de aprovação de saque (passo 1 do fluxo de 2 passos)."""
    quote = payout_bridge.quote(payload.amount_usd)
    approval_id = approval_gate.request(
        title=f"Saque {payload.channel.upper()} de ${payload.amount_usd:.2f}",
        kind="payout",
        amount_usd=payload.amount_usd,
        reason=payload.reason or "Saque de lucros do Presidente",
        requested_by="president_apk",
        metadata={
            "channel": payload.channel,
            "destination": payload.destination,
            "amount_mzn": quote["mzn"],
        },
    )
    return {
        "ok": True,
        "approval_id": approval_id,
        "quote": quote,
        "next_step": "Aprovar no APK com biometria e chamar POST /treasury/payouts.",
    }


@app.get("/treasury/quote", tags=["tesouraria"])
def quote(
    amount_usd: float = Query(gt=0),
    currency: str = Query(default="MZN"),
) -> Dict[str, Any]:
    return payout_bridge.quote(amount_usd, currency)


@app.post("/treasury/fx", tags=["tesouraria"], dependencies=[Depends(require_token)])
def update_fx(payload: FxRequest) -> Dict[str, Any]:
    return {"ok": True, "fx": cfo.update_fx_rate(payload.usd_mzn, source="president_apk")}


@app.post("/treasury/fx/refresh", tags=["tesouraria"], dependencies=[Depends(require_token)])
def refresh_fx() -> Dict[str, Any]:
    return {"ok": True, "fx": payout_bridge.refresh_exchange_rate()}


# ==========================================================================
# Governança
# ==========================================================================
@app.get("/governance", tags=["governança"])
def governance() -> Dict[str, Any]:
    return {
        "hr": hr_department.report(),
        "accounting": accountant.financial_report(),
        "legal": legal_department.report(),
        "judge": eval_judge.report(),
        "cemetery": shared_memory.stats(),
        "generated_at": utcnow_iso(),
    }


@app.get("/governance/hr", tags=["governança"])
def hr_report() -> Dict[str, Any]:
    return hr_department.report()


@app.post("/governance/hr/review", tags=["governança"], dependencies=[Depends(require_token)])
def hr_review(agent_id: Optional[str] = Body(default=None, embed=True),
              auto_rewrite: bool = Body(default=False, embed=True)) -> Dict[str, Any]:
    if agent_id:
        return {"ok": True, "review": hr_department.review_agent(agent_id, auto_rewrite=auto_rewrite).to_dict()}
    return {"ok": True, "review": hr_department.review_all(auto_rewrite=auto_rewrite)}


@app.get("/governance/accounting", tags=["governança"])
def accounting() -> Dict[str, Any]:
    return {
        "report": accountant.financial_report(),
        "audit": accountant.audit_all(),
    }


@app.get("/governance/legal", tags=["governança"])
def legal() -> Dict[str, Any]:
    return legal_department.report()


@app.post("/governance/legal/audit", tags=["governança"],
          dependencies=[Depends(require_token)])
def legal_audit(platforms: Optional[List[str]] = Body(default=None, embed=True),
                packages: Optional[List[str]] = Body(default=None, embed=True)) -> Dict[str, Any]:
    repo = legal_department.audit_repository().to_dict()
    tos = [legal_department.audit_terms(p).to_dict() for p in (platforms or [])]
    pkgs = (
        legal_department.audit_package_licenses(packages).to_dict() if packages else None
    )
    return {"repository": repo, "tos": tos, "packages": pkgs}


@app.get("/governance/cemetery", tags=["governança"])
def cemetery(limit: int = Query(default=100, ge=1, le=300)) -> Dict[str, Any]:
    return {"stats": shared_memory.stats(), "entries": shared_memory.list_entries(limit)}


@app.post("/governance/cemetery/check", tags=["governança"])
def cemetery_check(
    idea: str = Body(..., embed=True),
    niche: str = Body(default="", embed=True),
    tags: Optional[List[str]] = Body(default=None, embed=True),
) -> Dict[str, Any]:
    try:
        return shared_memory.check_against_cemetery(idea=idea, niche=niche, tags=tags)
    except ValidationBlocked as exc:
        return {"allowed": False, **exc.details}


# ==========================================================================
# Portefólio de esquadrões
# ==========================================================================
@app.get("/squads", tags=["esquadrões"])
def list_squads(status: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    return accountant.portfolio()


@app.get("/squads/{squad_id}", tags=["esquadrões"])
def get_squad(squad_id: str) -> Dict[str, Any]:
    try:
        return accountant.get_squad(squad_id)
    except ValueError as exc:
        raise NotFound(str(exc))


@app.post("/squads/{squad_id}/activity", tags=["esquadrões"],
          dependencies=[Depends(require_token)])
def record_squad_activity(squad_id: str, payload: ActivityRequest) -> Dict[str, Any]:
    record = accountant.record_activity(
        squad_id,
        views=payload.views,
        clicks=payload.clicks,
        conversions=payload.conversions,
        revenue_usd=payload.revenue_usd,
        cost_usd=payload.cost_usd,
        notes=payload.notes,
    )
    verdict = accountant.evaluate_squad(squad_id)
    return {"ok": True, "squad": record, "verdict": verdict.to_dict()}


@app.post("/squads/{squad_id}/kill", tags=["esquadrões"],
          dependencies=[Depends(require_token)])
def kill_squad(squad_id: str, payload: SquadKillRequest) -> Dict[str, Any]:
    """Abate manual: exige biometria e cria registo no cemitério."""
    require_biometric(payload.biometric_verified)
    approval_id = approval_gate.request(
        title=f"Abater esquadrão {squad_id}",
        kind="kill_squad",
        reason=payload.cause,
        requested_by="president_apk",
        metadata={"squad_id": squad_id},
    )
    approval_gate.decide(
        approval_id, approve=True, decided_by="president_apk",
        biometric_verified=True, note="abate manual",
    )
    result = accountant.kill_squad(squad_id, cause=payload.cause, approved_by="president_apk")
    approval_gate.mark_executed(approval_id, {"squad_id": squad_id})
    return {"ok": True, "approval_id": approval_id, **result}


# ==========================================================================
# Esquadrões — execução directa
# ==========================================================================
@app.post("/squads/scout/scan", tags=["esquadrões"], dependencies=[Depends(require_token)])
def scout_scan() -> Dict[str, Any]:
    return scout_hunter.run(max_viable=3).to_dict()


@app.post("/squads/video/produce", tags=["esquadrões"], dependencies=[Depends(require_token)])
def produce_video(payload: VideoRequest) -> Dict[str, Any]:
    return video_factory.run(
        idea=payload.idea,
        niche=payload.niche,
        smoke_test_id=payload.smoke_test_id,
        duration_target=payload.duration_target,
        language=payload.language,
    ).to_dict()


@app.post("/squads/infoproducts/produce", tags=["esquadrões"],
          dependencies=[Depends(require_token)])
def produce_infoproduct(payload: InfoproductRequest) -> Dict[str, Any]:
    return infoproducts_squad.run(
        idea=payload.idea,
        niche=payload.niche,
        smoke_test_id=payload.smoke_test_id,
        product_type=payload.product_type,
        price_usd=payload.price_usd,
    ).to_dict()


@app.post("/squads/micro-saas/build", tags=["esquadrões"],
          dependencies=[Depends(require_token)])
def build_micro_saas(payload: MicroSaasRequest) -> Dict[str, Any]:
    return micro_saas_squad.run(
        idea=payload.idea,
        niche=payload.niche,
        smoke_test_id=payload.smoke_test_id,
        price_usd=payload.price_usd,
        allow_code_execution=payload.allow_code_execution,
    ).to_dict()


# ==========================================================================
# Validação
# ==========================================================================
@app.get("/validation", tags=["validação"])
def validation_report() -> Dict[str, Any]:
    return {
        "report": demand_validator.report(),
        "tests": demand_validator.list_tests(),
    }


@app.post("/validation", tags=["validação"], dependencies=[Depends(require_token)])
def create_validation(payload: SmokeTestRequest) -> Dict[str, Any]:
    return demand_validator.create_smoke_test(
        idea=payload.idea,
        niche=payload.niche,
        squad_type=payload.squad_type,
        hypothesis=payload.hypothesis,
        cta=payload.cta,
        price_anchor_usd=payload.price_anchor_usd,
        min_clicks=payload.min_clicks,
        min_conversion_rate=payload.min_conversion_rate,
        window_days=payload.window_days,
    )


@app.post("/validation/{test_id}/event", tags=["validação"])
def validation_event(test_id: str, payload: SmokeEventRequest) -> Dict[str, Any]:
    """Endpoint público chamado pela landing page (cliques/inscrições)."""
    result = demand_validator.record_event(
        test_id,
        event=payload.event,
        source=payload.source,
        value_usd=payload.value_usd,
        count=payload.count,
    )
    return {"ok": True, **result}


@app.get("/validation/{test_id}", tags=["validação"])
def validation_get(test_id: str) -> Dict[str, Any]:
    try:
        return demand_validator.evaluate(test_id)
    except NotFound as exc:
        raise exc


@app.get("/validation/{test_id}/page", response_class=HTMLResponse, tags=["validação"])
def validation_page(test_id: str) -> HTMLResponse:
    """Serve a landing page do smoke test (gratuita, sem GitHub Pages)."""
    try:
        html = demand_validator.render_page(test_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


@app.post("/validation/judge", tags=["validação"])
def judge_content(payload: JudgeRequest) -> Dict[str, Any]:
    verdict = eval_judge.evaluate(
        content=payload.content,
        content_type=payload.content_type,
        metadata=payload.metadata,
        squad_id="api",
        agent_id="api_client",
    )
    return verdict.to_dict()


# ==========================================================================
# Ciclo do CEO
# ==========================================================================
@app.get("/cycle", tags=["ciclo"])
def cycle_status() -> Dict[str, Any]:
    return {
        "last_cycle": ceo.last_cycle(),
        "history": ceo.cycle_history(limit=10),
        "kill_switch": kill_switch.status(),
    }


@app.post("/cycle/run", tags=["ciclo"], dependencies=[Depends(require_token)])
def run_cycle(payload: CycleRequest) -> Dict[str, Any]:
    return ceo.run_cycle(source="api", force_scan=payload.force_scan, produce=payload.produce)


# ==========================================================================
# Observabilidade / LLM
# ==========================================================================
@app.get("/events", tags=["observabilidade"])
def recent_events(
    limit: int = Query(default=100, ge=1, le=500),
    since_sequence: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    return {
        "events": event_bus.recent(limit=limit, since_sequence=since_sequence),
        "last_sequence": event_bus.last_sequence(),
        "subscribers": event_bus.subscriber_count(),
    }


@app.get("/observability", tags=["observabilidade"])
def observability() -> Dict[str, Any]:
    return {
        "llm": llm_router.status(),
        "tracer": llm_tracer.metrics(),
        "recent_traces": llm_tracer.recent(limit=30),
        "budget": {
            "daily_cap_usd": settings.daily_spend_cap_usd,
            "per_agent_cap_usd": settings.per_agent_daily_cap_usd,
        },
        "generated_at": utcnow_iso(),
    }


@app.post("/llm/chat", tags=["observabilidade"], dependencies=[Depends(require_token)])
def llm_chat(payload: ChatRequest) -> Dict[str, Any]:
    response = llm_router.complete(
        LLMRequest(
            prompt=payload.prompt,
            task=payload.task,
            json_mode=payload.json_mode,
            agent_id="api_client",
        )
    )
    return {"text": response.text, "meta": response.to_dict()}


# ==========================================================================
# Agentes (agents.yaml / tasks.yaml) — ponte CrewAI + roteador LLM
# ==========================================================================
@app.get("/agents", tags=["agentes"])
def list_agents() -> Dict[str, Any]:
    """Catálogo de agentes e tarefas declarados nos YAML, com validação."""
    validation = agent_registry.validate()
    return {
        "validation": validation,
        "bridge": crew_bridge.availability(),
        "agents": [spec.to_dict() for spec in agent_registry.agents()],
        "tasks": [task.to_dict() for task in agent_registry.tasks()],
        "defaults": agent_registry.defaults(),
        "llm": llm_router.status(),
    }


@app.get("/agents/probe", tags=["agentes"])
def probe_llm_providers() -> Dict[str, Any]:
    """Testa de verdade cada provedor de IA (chamada real, custo $0.00).

    Devolve ``status`` por provedor e uma conclusão legível — a resposta
    directa à pergunta «a IA está ligada?».
    """
    return run_probe()


@app.post("/agents/reload", tags=["agentes"], dependencies=[Depends(require_token)])
def reload_agents() -> Dict[str, Any]:
    """Recarrega os YAML sem reiniciar o processo (hot reload auditável)."""
    agent_registry.reload(force=True)
    return agent_registry.validate()


@app.post("/agents/run", tags=["agentes"], dependencies=[Depends(require_token)])
def run_agent_task(payload: CrewTaskRequest) -> Dict[str, Any]:
    """Executa uma tarefa declarada em ``tasks.yaml`` com o agente dono dela.

    Usa CrewAI quando instalado; caso contrário o roteador LLM nativo. Nunca
    gasta dinheiro: provedores pagos não estão na cadeia.
    """
    result = crew_bridge.run_task(
        payload.task_id, payload.inputs, require_json=payload.require_json
    )
    return {"ok": True, **result}


@app.post("/agents/pipeline", tags=["agentes"], dependencies=[Depends(require_token)])
def run_agent_pipeline(payload: CrewPipelineRequest) -> Dict[str, Any]:
    """Corre um pipeline de tarefas respeitando ``depends_on`` e os gates."""
    result = crew_bridge.run_pipeline(payload.task_ids, payload.inputs)
    return {"ok": True, **result}


@app.post("/security/scan", tags=["segurança"])
def scan_content(text: str = Body(..., embed=True), source: str = Body(default="api", embed=True)) -> Dict[str, Any]:
    """Verifica conteúdo não confiável contra prompt injection."""
    result = prompt_shield.scan(text, source=source)
    return result.to_dict()


# ==========================================================================
# WebSocket — LiveOps (Chain-of-Thought em tempo real)
# ==========================================================================
@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    """Streaming de eventos para a aba LiveOps do APK."""
    await websocket.accept()
    queue = event_bus.subscribe()
    try:
        backlog = event_bus.recent(limit=50)
        await websocket.send_json(
            {
                "type": "snapshot",
                "agent": "api",
                "message": "Histórico recente carregado.",
                "payload": {
                    "events": backlog,
                    "kill_switch": kill_switch.status(),
                    "budget": {
                        "daily_cap_usd": settings.daily_spend_cap_usd,
                    },
                },
                "ts": utcnow_iso(),
            }
        )
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                # Heartbeat para manter a ligação viva atrás de proxies.
                await websocket.send_json(
                    {"type": "heartbeat", "agent": "api", "message": "ping",
                     "payload": {"kill_switch": kill_switch.status()},
                     "ts": utcnow_iso()}
                )
                continue
            await websocket.send_json(event)
    except WebSocketDisconnect:
        log.info("Cliente WebSocket desligado.")
    except Exception as exc:
        log.warning("WebSocket encerrado por erro: %s", exc)
    finally:
        event_bus.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await websocket.close()


# ==========================================================================
# Telegram — webhook e comandos
# ==========================================================================
def _register_telegram_commands() -> None:
    """Liga os comandos do bot às camadas da holding."""

    def cmd_status(chat_id: str, args: List[str], raw: str) -> str:
        snap = ceo.status_snapshot()
        treasury = snap["treasury"]
        return (
            f"🏛 *Automaton Holding* — fase {treasury['phase']} "
            f"({treasury['phase_name']})\n"
            f"Património: ${treasury['total_assets_usd']:.2f}\n"
            f"Receita 30d: ${treasury['monthly_revenue_usd']:.2f}\n"
            f"Esquadrões activos: {snap['portfolio']['active']}\n"
            f"Aprovações pendentes: {snap['approvals']['pending']} "
            f"({snap['approvals']['critical']} críticas)\n"
            f"Kill switch: {'🔴 ACTIVO' if snap['kill_switch']['engaged'] else '🟢 livre'}"
        )

    def cmd_kill(chat_id: str, args: List[str], raw: str) -> str:
        reason = " ".join(args) or "comando /kill no Telegram"
        state = kill_switch.engage(reason, actor=f"telegram:{chat_id}")
        event_bus.publish("kill_switch", agent="security",
                          message=f"KILL SWITCH via Telegram: {reason}",
                          level="critical", payload=state)
        return f"🔴 Kill switch accionado. Motivo: {reason}"

    def cmd_release(chat_id: str, args: List[str], raw: str) -> str:
        state = kill_switch.release(actor=f"telegram:{chat_id}")
        return "🟢 Sistema libertado. Operações retomadas." if not state["engaged"] else "Falhou."

    def cmd_pendentes(chat_id: str, args: List[str], raw: str) -> str:
        deck = approval_gate.pending_batches()
        if not deck["pending_count"]:
            return "Sem aprovações pendentes."
        lines = [f"📋 {deck['pending_count']} pendentes "
                 f"(${deck['total_pending_usd']:.2f}):"]
        for item in approval_gate.list_items(status="pending", limit=10):
            flag = "🚨" if item["priority"] == "critical" else "•"
            lines.append(f"{flag} `{item['id']}` — {item['title'][:70]}")
        return "\n".join(lines)

    def cmd_aprovar(chat_id: str, args: List[str], raw: str) -> str:
        if not args:
            return "Uso: /aprovar <id>"
        try:
            updated = approval_gate.decide(
                args[0], approve=True, decided_by=f"telegram:{chat_id}",
                biometric_verified=False, note="aprovado via Telegram",
                require_biometric=False,
            )
            execution = ceo.execute_approved(args[0])
            status = "executado" if execution.get("ok") else "aprovado (execução manual)"
            return f"✅ {updated['title'][:60]} — {status}."
        except Exception as exc:
            return f"⚠️ Falha: {exc}"

    def cmd_vetar(chat_id: str, args: List[str], raw: str) -> str:
        if not args:
            return "Uso: /vetar <id>"
        try:
            approval_gate.decide(
                args[0], approve=False, decided_by=f"telegram:{chat_id}",
                biometric_verified=False, note="vetado via Telegram",
                require_biometric=False,
            )
            return "🚫 Item vetado."
        except Exception as exc:
            return f"⚠️ Falha: {exc}"

    def cmd_tesouraria(chat_id: str, args: List[str], raw: str) -> str:
        snap = cfo.snapshot()
        balances = snap["balances_usd"]
        mzn = snap["balances_mzn"]
        return (
            f"💰 *Tesouraria* — fase {snap['phase']} ({snap['phase_meta']['name']})\n"
            f"Impostos: ${balances['tax_reserve_usd']:.2f}\n"
            f"Bolso Presidente: ${balances['owner_pocket_usd']:.2f} "
            f"(≈ MZN {mzn['owner_pocket_usd']:.2f})\n"
            f"Poupança: ${balances['savings_vault_usd']:.2f}\n"
            f"Reinvestimento: ${balances['reinvestment_pool_usd']:.2f}\n"
            f"Total: ${snap['total_assets_usd']:.2f}"
        )

    def cmd_esquadroes(chat_id: str, args: List[str], raw: str) -> str:
        portfolio = accountant.portfolio()
        squads = portfolio["squads"]
        if not squads:
            return "Sem esquadrões registados."
        lines = ["🧩 *Esquadrões*"]
        for squad in squads[:10]:
            lines.append(
                f"• {squad['name'][:40]} [{squad['status']}] "
                f"views={squad['views']} receita=${squad['revenue_usd']:.2f}"
            )
        return "\n".join(lines)

    def cmd_ajuda(chat_id: str, args: List[str], raw: str) -> str:
        return (
            "*Comandos*\n"
            "/status — estado geral\n"
            "/tesouraria — cofres e fase\n"
            "/esquadroes — portefólio\n"
            "/pendentes — aprovações\n"
            "/aprovar <id> — aprovar item\n"
            "/vetar <id> — vetar item\n"
            "/kill [motivo] — paragem de emergência\n"
            "/release — libertar o sistema"
        )

    telegram_client.register("status", cmd_status)
    telegram_client.register("kill", cmd_kill)
    telegram_client.register("release", cmd_release)
    telegram_client.register("pendentes", cmd_pendentes)
    telegram_client.register("aprovar", cmd_aprovar)
    telegram_client.register("vetar", cmd_vetar)
    telegram_client.register("tesouraria", cmd_tesouraria)
    telegram_client.register("esquadroes", cmd_esquadroes)
    telegram_client.register("start", cmd_ajuda)
    telegram_client.register("ajuda", cmd_ajuda)
    telegram_client.register("help", cmd_ajuda)


@app.post("/telegram/webhook", tags=["telegram"])
async def telegram_webhook(request: Request) -> Dict[str, Any]:
    """Webhook opcional (alternativa ao long polling)."""
    if not telegram_client.enabled:
        raise ConfigurationError("TELEGRAM_BOT_TOKEN não configurado.")
    try:
        update = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")
    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = str(message.get("text") or "")
    if not chat_id or not text:
        return {"ok": True, "ignored": True}
    if not telegram_client.is_allowed(chat_id):
        return {"ok": True, "ignored": True, "reason": "chat não autorizado"}
    reply = telegram_client.dispatch(chat_id, text)
    if reply:
        telegram_client.send(chat_id, reply, markdown=True)
    return {"ok": True}


# ==========================================================================
# Debug / utilitários
# ==========================================================================
@app.get("/", response_class=PlainTextResponse, include_in_schema=False)
def root() -> str:
    return (
        "Automaton Holding Corp API v1.0.0\n"
        f"Status: online | kill switch: {kill_switch.status()['engaged']}\n"
        "Docs: /docs | Health: /health | WebSocket: /ws/live\n"
    )


@app.get("/vault/files", tags=["sistema"], dependencies=[Depends(require_token)])
def vault_files() -> Dict[str, Any]:
    return {"files": vault.list_files(), "expected": list(VAULT_FILES),
            "health": vault.health()}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
