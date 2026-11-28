"""Cobrança internacional por PayPal — o lado que faltava no cofre.

Porque este módulo existe
------------------------
O `payout_bridge` já sabia **enviar** dinheiro por PayPal (Payouts, API
REST directa). Não sabia **receber**: criar um pedido de compra para um
infoproduto vendido a alguém fora de Moçambique, onde M-Pesa e e-Mola não
chegam. Este módulo fecha essa metade.

O que foi verificado antes de escrever uma linha (2026-11-28, PyPI)
--------------------------------------------------------------------
=============  ========  ===============================================
Pacote         Versão    Estado
=============  ========  ===============================================
paypal-server-sdk   2.4.0     activo, publicado 2026-08-21
paypal-agent-toolkit 1.11.0   activo, publicado 2026-09-01
paypalrestsdk       1.13.3    **Deprecated** — resumo literal no PyPI
paypal-checkout-    1.0.3     **Deprecated** — resumo literal no PyPI
  serversdk
=============  ========  ===============================================

As duas últimas aparecem em tutoriais antigos e não são usadas aqui. O
import do SDK oficial é ``paypalserversdk`` (sem underscores no nome do
pacote Python) — a documentação que circula escreve
``paypal_server_sdk`` e isso dá ``ModuleNotFoundError``. Verificado a
importar, não a ler.

As 42 ferramentas do toolkit oficial
------------------------------------
``paypal_agent_toolkit`` traz 42 métodos prontos (``create_order``,
``create_invoice``, ``list_transactions``, ``create_subscription``…). O
valor dele aqui não é o código: é a lista de operações que a PayPal
considera legítimas. Este módulo expõe essa lista como está, e é ela que
o Conselho de IA recebe para decidir.

O que este módulo NÃO faz
-------------------------
* Não guarda chaves. Lê ``PAYPAL_CLIENT_ID``/``PAYPAL_CLIENT_SECRET`` da
  configuração, que as lê do ambiente.
* Não move dinheiro sem configuração: sem credenciais, entra em
  **dry-run explícito** e devolve o payload que teria enviado.
* Não contorna o `kill_switch` nem o portão de aprovação. A cobrança é
  \*entrada\* de dinheiro, mas continua a ser uma operação da holding e
  passa pelo mesmo `ensure_operational`.

A ressalva que fica registada (e não é resolvida por código)
------------------------------------------------------------
A PayPal não permite **sacar** para bancos moçambicanos. Receber saldo
funciona; tirá-lo de lá é a parte frágil e a alternativa real passa por
Payoneer/Wise. O `payout_bridge` já tem o conector Wise exactamente por
isso. Este módulo não esconde essa limitação: `diagnostico()` diz que o
saque não está resolvido.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.errors import ExternalServiceError, ValidationBlocked

#: Moedas que a PayPal aceita para cobrança. A holding só trabalha com USD
#: e EUR — o resto exigiria conversão e a conversão come margem.
MOEDAS_SUPORTADAS = ("USD", "EUR")

#: Estados de um pedido que contam como "pago". `COMPLETED` é o final;
#: `APPROVED` significa que o comprador já autorizou mas o dinheiro ainda
#: não foi capturado — não é receita até ser capturado.
ESTADOS_PAGOS = frozenset({"COMPLETED"})
ESTADOS_A_AGUARDAR = frozenset({"APPROVED", "CREATED", "PAYER_ACTION_REQUIRED"})


@dataclass
class Cobranca:
    """Um pedido de cobrança criado (ou simulado)."""

    ok: bool
    order_id: str = ""
    status: str = ""
    approval_url: str = ""
    amount_usd: float = 0.0
    currency: str = "USD"
    dry_run: bool = False
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def pago(self) -> bool:
        """Só é receita quando a PayPal diz COMPLETED."""
        return self.status.upper() in ESTADOS_PAGOS and not self.dry_run

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "order_id": self.order_id,
            "status": self.status,
            "approval_url": self.approval_url,
            "amount_usd": self.amount_usd,
            "currency": self.currency,
            "dry_run": self.dry_run,
            "pago": self.pago,
            "message": self.message,
        }


class PayPalCobranca:
    """Recebe pagamentos: cria pedidos, lê estado, captura.

    Todo o dinheiro que entra por aqui é registado por
    `cfo_treasury.record_revenue`, que aplica a provisão fiscal de 5% e a
    divisão por fase. Este módulo **não** divide nada: a regra vive num só
    sítio, e não é aqui.
    """

    def __init__(self) -> None:
        self._cliente: Any = None
        self._criado_em = time.time()

    # ---------------------------------------------------------- configuração
    @property
    def configurado(self) -> bool:
        return bool(settings.paypal_client_id and settings.paypal_client_secret)

    @property
    def sandbox(self) -> bool:
        return settings.paypal_mode.lower() != "live"

    def faltam(self) -> List[str]:
        falta: List[str] = []
        if not settings.paypal_client_id:
            falta.append("PAYPAL_CLIENT_ID")
        if not settings.paypal_client_secret:
            falta.append("PAYPAL_CLIENT_SECRET")
        return falta

    def _sdk_disponivel(self) -> bool:
        try:
            import paypalserversdk  # noqa: F401
        except ImportError:
            return False
        return True

    def cliente(self) -> Any:
        """Cliente do SDK oficial, criado uma vez por processo."""
        if self._cliente is not None:
            return self._cliente
        try:
            from paypalserversdk.paypal_serversdk_client import (
                PaypalServersdkClient,
            )
        except ImportError as exc:  # pragma: no cover - depende do ambiente
            raise ExternalServiceError(
                "paypal-server-sdk não instalado (pip install paypal-server-sdk)"
            ) from exc
        self._cliente = PaypalServersdkClient(
            client_id=settings.paypal_client_id,
            client_secret=settings.paypal_client_secret,
            environment="sandbox" if self.sandbox else "production",
        )
        return self._cliente

    # -------------------------------------------------------------- cobrança
    def criar_cobranca(
        self,
        *,
        valor_usd: float,
        descricao: str,
        reference_id: str = "",
        moeda: str = "USD",
    ) -> Cobranca:
        """Cria um pedido PayPal de intenção CAPTURE.

        Sem credenciais devolve ``dry_run=True`` com o payload que teria
        sido enviado. Não inventa um `order_id`: um id falso poderia ser
        confundido com um pedido real por quem ler o cofre.
        """
        if valor_usd <= 0:
            raise ValidationBlocked(
                "O valor da cobrança tem de ser positivo.",
                details={"valor_usd": valor_usd},
            )
        moeda = moeda.upper()
        if moeda not in MOEDAS_SUPORTADAS:
            raise ValidationBlocked(
                f"Moeda não suportada para cobrança: {moeda}",
                details={"suportadas": list(MOEDAS_SUPORTADAS)},
            )

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": reference_id or f"ref-{int(time.time())}",
                    "description": descricao[:127],
                    "amount": {"currency_code": moeda, "value": f"{valor_usd:.2f}"},
                }
            ],
        }

        if not self.configurado or not self._sdk_disponivel():
            motivo = (
                "sem credenciais PayPal"
                if not self.configurado
                else "paypal-server-sdk não instalado"
            )
            return Cobranca(
                ok=False,
                status="dry_run",
                amount_usd=valor_usd,
                currency=moeda,
                dry_run=True,
                message=f"{motivo}: nenhum pedido criado, nenhum dinheiro movido.",
                raw={"payload": payload, "faltam": self.faltam()},
            )

        try:
            from paypalserversdk.models.checkout_payment_intent import (
                CheckoutPaymentIntent,
            )
            from paypalserversdk.models.money import Money
            from paypalserversdk.models.order_request import OrderRequest
            from paypalserversdk.models.purchase_unit_request import (
                PurchaseUnitRequest,
            )
        except ImportError as exc:  # pragma: no cover
            raise ExternalServiceError(
                f"Modelos do SDK PayPal indisponíveis: {exc}"
            ) from exc

        pedido = OrderRequest(
            intent=CheckoutPaymentIntent.CAPTURE,
            purchase_units=[
                PurchaseUnitRequest(
                    reference_id=reference_id or f"ref-{int(time.time())}",
                    description=descricao[:127],
                    amount=Money(currency_code=moeda, value=f"{valor_usd:.2f}"),
                )
            ],
        )
        try:
            resposta = self.cliente().orders.create_order(body=pedido)
        except Exception as exc:  # noqa: BLE001 - o SDK levanta tipos variados
            raise ExternalServiceError(
                f"PayPal recusou a criação do pedido: {type(exc).__name__}: {exc}"
            ) from exc

        pedido_criado = getattr(resposta, "body", resposta)
        order_id = str(getattr(pedido_criado, "id", "") or "")
        estado = str(getattr(pedido_criado, "status", "") or "")
        link = ""
        for ligacao in getattr(pedido_criado, "links", []) or []:
            if getattr(ligacao, "rel", "") in {"approve", "payer-action"}:
                link = str(getattr(ligacao, "href", "") or "")
                break
        return Cobranca(
            ok=bool(order_id),
            order_id=order_id,
            status=estado,
            approval_url=link,
            amount_usd=valor_usd,
            currency=moeda,
            dry_run=False,
            message="pedido criado; o comprador tem de aprovar no PayPal"
            if link
            else "pedido criado",
            raw={"resposta": str(pedido_criado)[:2000]},
        )

    def estado(self, order_id: str) -> str:
        """Lê o estado de um pedido. Devolve "" se não for possível."""
        if not self.configurado or not order_id:
            return ""
        try:
            resposta = self.cliente().orders.get_order(order_id)
            pedido = getattr(resposta, "body", resposta)
            return str(getattr(pedido, "status", "") or "")
        except Exception as exc:  # noqa: BLE001
            raise ExternalServiceError(
                f"PayPal não devolveu o estado de {order_id}: {exc}"
            ) from exc

    def capturar(self, order_id: str) -> Dict[str, Any]:
        """Captura um pedido aprovado — é aqui que o dinheiro entra.

        Devolve o estado final; o chamador só deve registar receita quando
        o estado for COMPLETED. A separação é deliberada: perder a noção de
        "aprovado mas não capturado" é como se contabiliza dinheiro que
        nunca chegou.
        """
        if not self.configurado:
            return {
                "ok": False,
                "dry_run": True,
                "order_id": order_id,
                "message": "sem credenciais: captura não tentada",
            }
        try:
            resposta = self.cliente().orders.capture_order(order_id)
        except Exception as exc:  # noqa: BLE001
            raise ExternalServiceError(
                f"PayPal recusou a captura de {order_id}: {exc}"
            ) from exc
        pedido = getattr(resposta, "body", resposta)
        estado = str(getattr(pedido, "status", "") or "")
        return {
            "ok": estado.upper() in ESTADOS_PAGOS,
            "order_id": order_id,
            "status": estado,
            "capturado": estado.upper() in ESTADOS_PAGOS,
            "amount_usd": self._montante_capturado(pedido),
            "message": "capturado" if estado.upper() in ESTADOS_PAGOS
            else "ainda não capturado",
        }

    @staticmethod
    def _montante_capturado(pedido: Any) -> float:
        """Lê o valor efectivamente capturado da resposta da PayPal.

        O montante vive em `purchase_units[].payments.captures[].amount.value`.
        Devolve 0.0 quando não consegue ler — e 0.0 não entra no cofre, porque
        `record_revenue` recusa valores não positivos. Na dúvida, o dinheiro
        não é contabilizado: é o erro certo.
        """
        try:
            unidades = getattr(pedido, "purchase_units", None) or []
            for unidade in unidades:
                pagamentos = getattr(unidade, "payments", None)
                capturas = getattr(pagamentos, "captures", None) or []
                for captura in capturas:
                    valor = getattr(getattr(captura, "amount", None), "value", None)
                    if valor is not None:
                        return float(valor)
        except (TypeError, ValueError, AttributeError):
            return 0.0
        return 0.0

    # ------------------------------------------------------------ toolkit IA
    @staticmethod
    def ferramentas_do_agente() -> List[Dict[str, str]]:
        """As 42 operações que a PayPal expõe a agentes de IA.

        Lidas do pacote em tempo de execução, não copiadas para aqui: se a
        PayPal acrescentar uma operação, ela aparece sozinha. Se o pacote
        não estiver instalado, devolve lista vazia em vez de rebentar — o
        CFO continua a funcionar sem toolkit.
        """
        try:
            from paypal_agent_toolkit.shared.tools import tools
        except ImportError:
            return []
        return [
            {
                "metodo": str(t.get("method") or ""),
                "nome": str(t.get("name") or t.get("method") or ""),
                "descricao": str(t.get("description") or "")[:200],
            }
            for t in tools
            if t.get("method")
        ]

    def executar_ferramenta(self, metodo: str, parametros: Dict[str, Any]) -> str:
        """Executa uma ferramenta do toolkit oficial.

        Existe para o CFO poder usar a lista acima. Sem credenciais não
        finge que executou: levanta erro e diz o que falta.
        """
        if not self.configurado:
            raise ValidationBlocked(
                "PayPal não configurado: faltam " + ", ".join(self.faltam()),
                details={"faltam": self.faltam()},
            )
        try:
            from paypal_agent_toolkit.shared import PayPalAPI
            from paypal_agent_toolkit.shared.configuration import Context
        except ImportError as exc:
            raise ExternalServiceError(
                "paypal-agent-toolkit não instalado"
            ) from exc
        api = PayPalAPI(
            client_id=settings.paypal_client_id,
            secret=settings.paypal_client_secret,
            context=Context(sandbox=self.sandbox),
        )
        return api.run(metodo, parametros)

    # ------------------------------------------------------------ diagnóstico
    def diagnostico(self) -> Dict[str, Any]:
        """Estado honesto, incluindo o que não está resolvido."""
        ferramentas = self.ferramentas_do_agente()
        return {
            "configurado": self.configurado,
            "faltam": self.faltam(),
            "modo": "sandbox" if self.sandbox else "live",
            "sdk_oficial_instalado": self._sdk_disponivel(),
            "sdk_pacote": "paypalserver-sdk (import: paypalserversdk)",
            "toolkit_instalado": bool(ferramentas),
            "ferramentas_do_agente": len(ferramentas),
            "moedas": list(MOEDAS_SUPORTADAS),
            "receber": (
                "pronto" if self.configurado else "dry-run explícito"
            ),
            # Esta linha existe para não se perder de vista o problema real:
            # receber funciona, tirar o dinheiro para Moçambique não.
            "sacar_para_mocambique": (
                "NÃO RESOLVIDO pela PayPal — usar o conector Wise do "
                "payout_bridge ou Payoneer. O saldo recebe-se; a saída é a "
                "parte frágil."
            ),
            "descontinuados_a_evitar": [
                "paypalrestsdk",
                "paypal-checkout-serversdk",
            ],
        }


#: Instância única, como os outros conectores.
paypal_cobranca = PayPalCobranca()
