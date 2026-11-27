"""Ponte de saques transfronteiriços: M-Pesa, e-Mola, PayPal e Wise.

Fluxo obrigatório de um saque (nunca se chama a API antes da aprovação):

  1. ``kill_switch`` verifica que o sistema está operacional.
  2. O pedido tem de referenciar uma aprovação **aprovada e biometricamente
     verificada** no APK/Telegram.
  3. O ``CFOTreasury`` **reserva** o valor (debita ``owner_pocket_usd``).
  4. Só então o conector do canal é chamado.
  5. Sucesso -> ``finalize_payout(executed)``; falha -> reembolso automático
     para o bolso do Presidente (``finalize_payout(failed)``).

Sem credenciais, os conectores entram em **dry-run explícito**: devolvem
``dry_run=True`` com o payload que *teriam* enviado e o valor é reembolsado.
Nunca simulamos uma transferência como se fosse real.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from ..config.settings import settings
from ..core.approvals import approval_gate
from ..core.clock import utcnow_iso
from ..core.errors import (
    ApprovalRequired,
    ConfigurationError,
    ExternalServiceError,
    ValidationBlocked,
)
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.security_guard import kill_switch
from .cfo_treasury import cfo

log = get_logger("finance.payout")

SUPPORTED_CHANNELS = {"mpesa", "emola", "paypal", "wise"}


# ==========================================================================
# Utilidades HTTP
# ==========================================================================
def _http(
    method: str,
    url: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 45,
) -> Tuple[int, Dict[str, Any]]:
    """Pedido HTTP com urllib; devolve ``(status, corpo_json)`` sem mascarar erros."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            parsed: Dict[str, Any] = {}
            if body.strip():
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"raw": body[:2000]}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        return exc.code, {"error": str(exc.reason), "detail": detail}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": str(exc)}


@dataclass
class PayoutRequest:
    amount_usd: float
    channel: str
    approval_id: str
    destination: str = ""
    note: str = ""
    currency_target: str = "MZN"
    idempotency_key: str = ""

    def validate(self) -> "PayoutRequest":
        channel = (self.channel or "").strip().lower()
        if channel not in SUPPORTED_CHANNELS:
            raise ValidationBlocked(
                f"Canal de saque não suportado: {self.channel!r}",
                details={"supported": sorted(SUPPORTED_CHANNELS)},
            )
        try:
            amount = round(float(self.amount_usd), 6)
        except (TypeError, ValueError):
            raise ValidationBlocked("Montante de saque inválido.")
        if amount <= 0:
            raise ValidationBlocked("Montante de saque deve ser positivo.")
        if not self.approval_id:
            raise ApprovalRequired(
                "Todo saque exige um approval_id aprovado com biometria.",
                details={"channel": channel},
            )
        self.channel = channel
        self.amount_usd = amount
        return self


@dataclass
class PayoutResult:
    ok: bool
    status: str
    channel: str
    amount_usd: float
    amount_mzn: float = 0.0
    payout_id: str = ""
    provider_ref: str = ""
    message: str = ""
    dry_run: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "channel": self.channel,
            "amount_usd": self.amount_usd,
            "amount_mzn": self.amount_mzn,
            "payout_id": self.payout_id,
            "provider_ref": self.provider_ref,
            "message": self.message,
            "dry_run": self.dry_run,
            "raw": self.raw,
            "created_at": self.created_at,
        }


# ==========================================================================
# Conectores
# ==========================================================================
class _Connector:
    name = "base"
    label = "Base"

    def available(self) -> bool:
        raise NotImplementedError

    def missing_config(self) -> list[str]:
        raise NotImplementedError

    def payout(self, request: PayoutRequest) -> PayoutResult:
        raise NotImplementedError

    def _dry_run(self, request: PayoutRequest, reason: str, payload: Dict[str, Any]) -> PayoutResult:
        log.warning(
            "Saque %s em DRY-RUN (faltam credenciais): %s — payload: %s",
            self.name, reason, json.dumps(payload)[:300],
        )
        return PayoutResult(
            ok=False,
            status="dry_run",
            channel=self.name,
            amount_usd=request.amount_usd,
            message=(
                f"{self.label}: credenciais ausentes ({reason}). "
                "Operação registada como dry-run; nenhum dinheiro foi movido."
            ),
            dry_run=True,
            raw={"payload_preview": payload},
        )


class MPesaConnector(_Connector):
    """M-Pesa (Vodacom Moçambique) — developer.vodacom.co.mz.

    Usa o ``mpesa-python-sdk`` (Apache-2.0) se estiver instalado; caso contrário
    fala directamente com a OpenAPI (fluxo OAuth + B2C single stage).
    A credencial de segurança exige encriptação RSA com a chave pública do
    operador, pelo que ``cryptography`` é necessária para o fluxo manual.
    """

    name = "mpesa"
    label = "M-Pesa (Vodacom Moçambique)"

    def available(self) -> bool:
        return bool(settings.mpesa_api_key and settings.mpesa_public_key)

    def missing_config(self) -> list[str]:
        missing = []
        if not settings.mpesa_api_key:
            missing.append("MPESA_API_KEY")
        if not settings.mpesa_public_key:
            missing.append("MPESA_PUBLIC_KEY")
        if not settings.mpesa_service_provider_code:
            missing.append("MPESA_SERVICE_PROVIDER_CODE")
        return missing

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _encrypt_credential(plain: str, public_key_pem: str) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:  # pragma: no cover - depende do ambiente
            raise ConfigurationError(
                "Encriptação RSA indisponível: instalar 'cryptography' "
                "para o fluxo manual M-Pesa B2C.",
            ) from exc
        key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        ciphertext = key.encrypt(  # type: ignore[union-attr]
            plain.encode("utf-8"),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),
                algorithm=hashes.SHA1(),
                label=None,
            ),
        )
        return base64.b64encode(ciphertext).decode("ascii")

    def _token(self) -> str:
        url = settings.mpesa_base_url.rstrip("/") + "/ipg/v1x/token"
        basic = base64.b64encode(settings.mpesa_api_key.encode("utf-8")).decode("ascii")
        status, body = _http(
            "GET", url, headers={"Authorization": f"Basic {basic}"}
        )
        if status != 200 or not body.get("access_token"):
            raise ExternalServiceError(
                "Falha ao obter token M-Pesa.",
                details={"status": status, "body": body},
            )
        return str(body["access_token"])

    # --------------------------------------------------------------- payout
    def payout(self, request: PayoutRequest) -> PayoutResult:
        if not self.available():
            return self._dry_run(
                request,
                ", ".join(self.missing_config()) or "configuração incompleta",
                {
                    "endpoint": "/ipg/v1x/b2cPayment/singleStage/",
                    "input_CustomerMSISDN": settings.mpesa_receiver_msisdn,
                    "input_Amount": request.amount_usd,
                },
            )
        msisdn = request.destination or settings.mpesa_receiver_msisdn
        if not msisdn:
            raise ValidationBlocked("Número M-Pesa de destino não configurado.")

        exchange = cfo.convert_usd_to_mzn(request.amount_usd)
        amount_mzn = f"{exchange['mzn']:.2f}"
        reference = request.idempotency_key or f"AUTO{int(time.time())}"
        payload = {
            "input_TransactionReference": reference[:12],
            "input_CustomerMSISDN": msisdn,
            "input_Amount": amount_mzn,
            "input_ThirdPartyReference": reference[:12],
            "input_ServiceProviderCode": settings.mpesa_service_provider_code,
        }

        # Caminho preferencial: SDK oficial.
        try:
            from mpesa.api.b2c import B2C  # type: ignore
        except ImportError:
            B2C = None  # type: ignore[assignment]

        if B2C is not None:
            try:
                client = B2C(
                    public_key=settings.mpesa_public_key,
                    host=settings.mpesa_base_url,
                    api_key=settings.mpesa_api_key,
                    service_provider_code=settings.mpesa_service_provider_code,
                    initiator_identifier=settings.mpesa_initiator_identifier,
                    security_credential=settings.mpesa_security_credential,
                )
                raw = client.payment(
                    input_TransactionReference=payload["input_TransactionReference"],
                    input_CustomerMSISDN=msisdn,
                    input_Amount=amount_mzn,
                    input_ThirdPartyReference=payload["input_ThirdPartyReference"],
                    input_ServiceProviderCode=settings.mpesa_service_provider_code,
                )
                ok = str(raw.get("output_ResponseCode", "")) == "INS-0"
                return PayoutResult(
                    ok=ok,
                    status="executed" if ok else "failed",
                    channel=self.name,
                    amount_usd=request.amount_usd,
                    amount_mzn=exchange["mzn"],
                    provider_ref=str(raw.get("output_TransactionID", "")),
                    message=str(raw.get("output_ResponseDesc", "SDK M-Pesa")),
                    dry_run=False,
                    raw=raw if isinstance(raw, dict) else {"raw": str(raw)},
                )
            except Exception as exc:
                log.warning("SDK M-Pesa falhou (%s); a tentar OpenAPI directa.", exc)

        # Caminho manual: OAuth + B2C single stage.
        if not settings.mpesa_security_credential and settings.mpesa_initiator_password:
            settings_credential = self._encrypt_credential(
                settings.mpesa_initiator_password, settings.mpesa_public_key
            )
        else:
            settings_credential = settings.mpesa_security_credential
        if not settings_credential:
            raise ConfigurationError(
                "MPESA_SECURITY_CREDENTIAL (ou MPESA_INITIATOR_PASSWORD) em falta."
            )

        token = self._token()
        url = settings.mpesa_base_url.rstrip("/") + "/ipg/v1x/b2cPayment/singleStage/"
        status, body = _http(
            "POST",
            url,
            payload={
                **payload,
                "input_InitiatorIdentifier": settings.mpesa_initiator_identifier,
                "input_SecurityCredential": settings_credential,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": settings.mpesa_origin,
            },
        )
        code = str(body.get("output_ResponseCode", ""))
        ok = status == 200 and code == "INS-0"
        if not ok:
            log.error("M-Pesa B2C falhou: HTTP %s / %s", status, body)
        return PayoutResult(
            ok=ok,
            status="executed" if ok else "failed",
            channel=self.name,
            amount_usd=request.amount_usd,
            amount_mzn=exchange["mzn"],
            provider_ref=str(body.get("output_TransactionID", "")),
            message=str(body.get("output_ResponseDesc", body.get("error", "sem detalhe"))),
            dry_run=False,
            raw=body,
        )


class EMolaConnector(_Connector):
    """e-Mola (Movitel) — API REST configurável por ambiente.

    A documentação da Movitel é fornecida ao abrigo de contrato de comerciante;
    o endpoint e o esquema são por isso parametrizados (``EMOLA_BASE_URL``) em
    vez de fixados no código. O payload segue o padrão de *merchant payment*
    (``msisdn``, ``amount``, ``currency``, ``reference``).
    """

    name = "emola"
    label = "e-Mola (Movitel)"

    def available(self) -> bool:
        return bool(settings.emola_api_key and settings.emola_merchant_code)

    def missing_config(self) -> list[str]:
        missing = []
        if not settings.emola_api_key:
            missing.append("EMOLA_API_KEY")
        if not settings.emola_merchant_code:
            missing.append("EMOLA_MERCHANT_CODE")
        return missing

    def payout(self, request: PayoutRequest) -> PayoutResult:
        msisdn = request.destination or settings.emola_receiver_msisdn
        exchange = cfo.convert_usd_to_mzn(request.amount_usd)
        reference = request.idempotency_key or f"EML{int(time.time())}"
        payload = {
            "merchantCode": settings.emola_merchant_code,
            "msisdn": msisdn,
            "amount": exchange["mzn"],
            "currency": "MZN",
            "reference": reference,
            "description": (request.note or "Saque Automaton Holding")[:120],
        }
        if not self.available():
            return self._dry_run(
                request, ", ".join(self.missing_config()) or "credenciais",
                {"endpoint": "/api/v1/payments/disbursement", **payload},
            )
        url = settings.emola_base_url.rstrip("/") + "/api/v1/payments/disbursement"
        status, body = _http(
            "POST",
            url,
            payload=payload,
            headers={
                "Authorization": f"Bearer {settings.emola_api_key}",
                "X-Merchant-Code": settings.emola_merchant_code,
            },
        )
        ok = status in {200, 201} and str(
            body.get("status", body.get("code", ""))
        ).upper() in {"SUCCESS", "OK", "ACCEPTED", "PENDING", "201", "200"}
        if not ok:
            log.error("e-Mola falhou: HTTP %s / %s", status, body)
        return PayoutResult(
            ok=ok,
            status="executed" if ok else "failed",
            channel=self.name,
            amount_usd=request.amount_usd,
            amount_mzn=exchange["mzn"],
            provider_ref=str(body.get("transactionId", body.get("reference", ""))),
            message=str(body.get("message", body.get("error", "sem detalhe"))),
            dry_run=False,
            raw=body,
        )


class PayPalConnector(_Connector):
    """PayPal Payouts REST (OAuth2 client_credentials + /v1/payments/payouts)."""

    name = "paypal"
    label = "PayPal"

    def available(self) -> bool:
        return bool(settings.paypal_client_id and settings.paypal_client_secret)

    def missing_config(self) -> list[str]:
        missing = []
        if not settings.paypal_client_id:
            missing.append("PAYPAL_CLIENT_ID")
        if not settings.paypal_client_secret:
            missing.append("PAYPAL_CLIENT_SECRET")
        return missing

    @property
    def _base(self) -> str:
        return (
            "https://api-m.sandbox.paypal.com"
            if settings.paypal_mode.lower() != "live"
            else "https://api-m.paypal.com"
        )

    def _token(self) -> str:
        basic = base64.b64encode(
            f"{settings.paypal_client_id}:{settings.paypal_client_secret}".encode("utf-8")
        ).decode("ascii")
        status, body = _http(
            "POST",
            f"{self._base}/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if status != 200 or not body.get("access_token"):
            raise ExternalServiceError(
                "Falha ao obter token PayPal.", details={"status": status, "body": body}
            )
        return str(body["access_token"])

    def payout(self, request: PayoutRequest) -> PayoutResult:
        if not self.available():
            return self._dry_run(request, ", ".join(self.missing_config()),
                                 {"endpoint": "/v1/payments/payouts", "amount_usd": request.amount_usd})
        receiver = request.destination
        if not receiver or "@" not in receiver:
            raise ValidationBlocked(
                "PayPal exige um email de destino no campo 'destination'."
            )
        token = self._token()
        reference = request.idempotency_key or f"paypal-{int(time.time())}"
        status, body = _http(
            "POST",
            f"{self._base}/v1/payments/payouts",
            payload={
                "sender_batch_header": {
                    "sender_batch_id": reference[:30],
                    "email_subject": "Saque Automaton Holding Corp",
                    "email_message": (request.note or "Distribuição de lucros")[:250],
                },
                "items": [
                    {
                        "recipient_type": "EMAIL",
                        "amount": {
                            "value": f"{request.amount_usd:.2f}",
                            "currency": "USD",
                        },
                        "receiver": receiver,
                        "note": "Saque de lucros — Automaton Holding",
                        "sender_item_id": reference[:30],
                    }
                ],
            },
            headers={
                "Authorization": f"Bearer {token}",
                "PayPal-Request-Id": reference[:38],
            },
        )
        ok = status in {200, 201, 202}
        batch = body.get("batch_header") or {}
        if not ok:
            log.error("PayPal payout falhou: HTTP %s / %s", status, body)
        return PayoutResult(
            ok=ok,
            status="executed" if ok else "failed",
            channel=self.name,
            amount_usd=request.amount_usd,
            provider_ref=str(batch.get("payout_batch_id", "")),
            message=str(batch.get("batch_status", body.get("message", "sem detalhe"))),
            dry_run=False,
            raw=body,
        )


class WiseConnector(_Connector):
    """Wise (TransferWise) API v3: quote -> recipient -> transfer -> fund."""

    name = "wise"
    label = "Wise"

    def available(self) -> bool:
        return bool(settings.wise_api_token and settings.wise_profile_id)

    def missing_config(self) -> list[str]:
        missing = []
        if not settings.wise_api_token:
            missing.append("WISE_API_TOKEN")
        if not settings.wise_profile_id:
            missing.append("WISE_PROFILE_ID")
        return missing

    def payout(self, request: PayoutRequest) -> PayoutResult:
        target = (request.currency_target or settings.wise_default_target_currency).upper()
        if not self.available():
            return self._dry_run(
                request, ", ".join(self.missing_config()),
                {"endpoint": "/v3/profiles/{id}/transfers", "target": target,
                 "amount_usd": request.amount_usd},
            )
        if not request.destination:
            raise ValidationBlocked(
                "Wise exige um identificador de destinatário (account id ou IBAN)."
            )
        headers = {
            "Authorization": f"Bearer {settings.wise_api_token}",
            "Content-Type": "application/json",
        }
        # 1) Cotação
        status, quote = _http(
            "POST",
            "https://api.wise.com/v3/quotes",
            payload={
                "sourceCurrency": "USD",
                "targetCurrency": target,
                "sourceAmount": request.amount_usd,
                "profile": int(settings.wise_profile_id),
            },
            headers=headers,
        )
        if status not in {200, 201} or "id" not in quote:
            return PayoutResult(
                ok=False, status="failed", channel=self.name,
                amount_usd=request.amount_usd,
                message=f"Cotação Wise falhou (HTTP {status}): {quote.get('error', quote.get('message', 'sem detalhe'))}",
                raw=quote,
            )
        # 2) Transferência
        status, transfer = _http(
            "POST",
            "https://api.wise.com/v1/transfers",
            payload={
                "targetAccount": request.destination,
                "quoteUuid": quote["id"],
                "customerTransactionId": request.idempotency_key
                or f"auto-{int(time.time())}",
                "details": {"reference": (request.note or "Automaton payout")[:60]},
            },
            headers=headers,
        )
        if status not in {200, 201} or "id" not in transfer:
            return PayoutResult(
                ok=False, status="failed", channel=self.name,
                amount_usd=request.amount_usd,
                message=f"Transferência Wise falhou (HTTP {status}): {transfer.get('error', transfer.get('message', 'sem detalhe'))}",
                raw=transfer,
            )
        # 3) Financiamento da transferência com o saldo USD
        status, funded = _http(
            "POST",
            f"https://api.wise.com/v3/profiles/{settings.wise_profile_id}"
            f"/transfers/{transfer['id']}/payments",
            payload={"type": "BALANCE"},
            headers=headers,
        )
        ok = status in {200, 201}
        return PayoutResult(
            ok=ok,
            status="executed" if ok else "failed",
            channel=self.name,
            amount_usd=request.amount_usd,
            amount_mzn=float(quote.get("targetAmount", 0.0) or 0.0),
            provider_ref=str(transfer.get("id", "")),
            message=str(
                transfer.get("status", funded.get("status", "transfer_created"))
            ),
            dry_run=False,
            raw={"quote": quote, "transfer": transfer, "funding": funded},
        )


# ==========================================================================
# Orquestrador
# ==========================================================================
class PayoutBridge:
    """Valida, reserva, executa e liquida saques de forma auditável."""

    def __init__(self) -> None:
        self.connectors: Dict[str, _Connector] = {
            "mpesa": MPesaConnector(),
            "emola": EMolaConnector(),
            "paypal": PayPalConnector(),
            "wise": WiseConnector(),
        }

    # ------------------------------------------------------------------ estado
    def channels_status(self) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {}
        for name, connector in self.connectors.items():
            snapshot[name] = {
                "label": connector.label,
                "available": connector.available(),
                "missing_config": connector.missing_config(),
                "mode": "live" if connector.available() else "dry_run",
            }
        fx = cfo.ledger().get("fx") or {}
        return {
            "channels": snapshot,
            "fx": {
                "usd_mzn": fx.get("usd_mzn", settings.usd_mzn_rate),
                "source": fx.get("source", "static_config"),
                "updated_at": fx.get("updated_at"),
            },
            "owner_pocket_usd": cfo.ledger().get("owner_pocket_usd", 0.0),
            "generated_at": utcnow_iso(),
        }

    def quote(self, amount_usd: float, currency: str = "MZN") -> Dict[str, Any]:
        exchange = cfo.convert_usd_to_mzn(amount_usd)
        if currency.upper() != "MZN":
            exchange["requested_currency"] = currency.upper()
            exchange["note"] = (
                "A conversão de referência é USD->MZN; outras moedas são "
                "cotadas pelo provedor no momento do envio."
            )
        return exchange

    # ------------------------------------------------------------------ saque
    def request_payout(self, request: PayoutRequest) -> PayoutResult:
        request.validate()
        kill_switch.ensure_operational("saque")
        connector = self.connectors.get(request.channel)
        if connector is None:
            raise ValidationBlocked(f"Canal sem conector: {request.channel}")

        # ---- 1. verificação da aprovação biométrica -------------------------
        approval = approval_gate.get(request.approval_id)
        if approval.status != "approved":
            raise ApprovalRequired(
                f"Saque bloqueado: aprovação {request.approval_id} está '{approval.status}'.",
                details={"approval_id": request.approval_id, "status": approval.status},
            )
        if not approval.biometric_verified:
            raise ApprovalRequired(
                "Saque bloqueado: aprovação sem verificação biométrica.",
                details={"approval_id": request.approval_id},
            )
        if abs(float(approval.amount_usd) - request.amount_usd) > 0.01:
            raise ValidationBlocked(
                "Montante do saque difere do montante aprovado.",
                details={
                    "approved_usd": approval.amount_usd,
                    "requested_usd": request.amount_usd,
                },
            )
        destinations = (approval.metadata or {}).get("destination")
        if destinations and request.destination and destinations != request.destination:
            raise ValidationBlocked(
                "Destino do saque difere do destino aprovado.",
                details={"approved_destination": destinations,
                         "requested_destination": request.destination},
            )

        # ---- 2. reserva de fundos ------------------------------------------
        reserved = cfo.reserve_for_payout(
            request.amount_usd,
            channel=request.channel,
            destination=request.destination,
        )
        payout_id = str(reserved["payout"]["id"])
        log.warning(
            "Saque %s reservado ($%.2f, %s) — a executar no conector.",
            payout_id, request.amount_usd, request.channel,
        )

        # ---- 3. execução no provedor ---------------------------------------
        try:
            result = connector.payout(request)
        except (ConfigurationError, ValidationBlocked, ExternalServiceError) as exc:
            cfo.finalize_payout(
                payout_id, status="failed", note=f"erro no conector: {exc.message}"
            )
            raise
        except Exception as exc:
            cfo.finalize_payout(payout_id, status="failed", note=f"erro inesperado: {exc}")
            raise ExternalServiceError(
                f"Falha inesperada no conector {request.channel}: {exc}"
            ) from exc

        result.payout_id = payout_id
        result.amount_mzn = result.amount_mzn or reserved["payout"].get("amount_mzn", 0.0)

        # ---- 4. liquidação contabilística ----------------------------------
        if result.ok and not result.dry_run:
            cfo.finalize_payout(
                payout_id, status="executed",
                provider_ref=result.provider_ref, note=result.message,
            )
            try:
                approval_gate.mark_executed(
                    request.approval_id,
                    {
                        "payout_id": payout_id,
                        "provider_ref": result.provider_ref,
                        "channel": request.channel,
                        "amount_usd": request.amount_usd,
                    },
                )
            except Exception as exc:
                log.warning("Saque executado mas aprovação não marcada: %s", exc)
            event_bus.publish(
                "payout_executed",
                agent="payout_bridge",
                message=(
                    f"Saque executado via {connector.label}: ${result.amount_usd:.2f} "
                    f"(≈ MZN {result.amount_mzn:.2f}) ref={result.provider_ref}"
                ),
                level="warning",
                payload=result.to_dict(),
            )
        else:
            note = "dry_run (sem credenciais)" if result.dry_run else result.message
            cfo.finalize_payout(payout_id, status="failed", note=note)
            event_bus.publish(
                "payout_reverted" if not result.dry_run else "payout_dry_run",
                agent="payout_bridge",
                message=f"Saque {payout_id} não executado: {note}. Valor reembolsado.",
                level="warning" if not result.dry_run else "info",
                payload=result.to_dict(),
            )
        return result

    # ------------------------------------------------------------------- fx
    def refresh_exchange_rate(self) -> Dict[str, Any]:
        """Tenta obter USD→MZN numa API pública gratuita; cai na taxa da config."""
        status, body = _http(
            "GET", "https://open.er-api.com/v6/latest/USD", timeout=20
        )
        rate: Optional[float] = None
        source = "static_config"
        if status == 200 and isinstance(body.get("rates"), dict):
            try:
                candidate = float(body["rates"]["MZN"])
                if candidate > 0:
                    rate = candidate
                    source = "open.er-api.com"
            except (KeyError, TypeError, ValueError):
                rate = None
        if rate is None:
            rate = settings.usd_mzn_rate
            log.info("Cotação online indisponível; a usar a taxa configurada (%.2f).", rate)
        return cfo.update_fx_rate(rate, source=source)


payout_bridge = PayoutBridge()

__all__ = [
    "PayoutBridge",
    "payout_bridge",
    "PayoutRequest",
    "PayoutResult",
    "SUPPORTED_CHANNELS",
]
