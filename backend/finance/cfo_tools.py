"""Os três tools que o Agente CFO pode usar — como funções de verdade.

O problema que este ficheiro resolve
------------------------------------
``config/agents.yaml`` já declarava ``tools: [ledger_tools, payout_bridge,
paypal_cobranca, fx_rates]`` desde o início, mas o ``crew_bridge`` só lê esses
nomes como *metadado*: nunca instanciava nada. Ou seja, a linha do YAML era
documentação — um agente a quem se diz "tens uma ferramenta de saques" e que
não tem ferramenta nenhuma.

Aqui os três tools são funções reais, com contrato testável, e são também o
que o CrewAI consome (``crewai.tools.tool`` quando disponível, dicionário de
especificação quando não).

O que este ficheiro **não** faz
-------------------------------
Não divide lucros. A regra 10/20/70 vive em ``cfo_treasury`` (e muda com a
fase — 10/40/50 na fase 2, 50/30/20 na fase 3). ``dividir_lucro`` aqui é
apenas uma porta para essa única implementação. Duplicar as percentagens
daria duas verdades sobre o mesmo cofre, e mais cedo ou mais tarde elas
discordariam — em silêncio, que é como os erros de dinheiro são piores.

O que este ficheiro **não deixa** fazer
---------------------------------------
Nenhum tool move dinheiro sem passar pelas guardas que já existiam:

* ``receber_pagamento`` com ``origem="paypal"`` só regista receita se a
  captura disser ``COMPLETED``; se a PayPal não devolver o montante, recusa
  em vez de inventar um valor.
* ``registrar_saques`` **nunca** executa pagamento. Cria um pedido pendente
  (``payout``, crítico) que o Presidente aprova com biometria no telemóvel.
  Um agente de IA não movimenta dinheiro para fora da holding; pede.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.approvals import approval_gate
from ..core.errors import ApprovalRequired, ValidationBlocked
from ..core.logger import get_logger
from .cfo_treasury import cfo, phase_for_monthly_revenue, split_for_phase
from .paypal_cobranca import paypal_cobranca
from .payout_bridge import SUPPORTED_CHANNELS, PayoutRequest, payout_bridge

log = get_logger("finance.cfo_tools")


# ==========================================================================
# Tool 1 — receber_pagamento
# ==========================================================================
def receber_pagamento(
    valor_usd: float,
    *,
    origem: str,
    squad: str = "unknown",
    descricao: str = "",
    referencia: str = "",
    order_id: str = "",
    moeda: str = "USD",
) -> Dict[str, Any]:
    """Regista dinheiro que entrou, vindo de qualquer um dos canais.

    ``origem``:
        ``"paypal"``      — exige ``order_id``; captura e só regista se a
                            PayPal confirmar ``COMPLETED``.
        ``"mpesa"``/``"emola"``/``"manual"`` — registo directo do valor.

    Devolve a divisão aplicada (provisão fiscal + fase) e os saldos.
    """
    origem_norm = (origem or "").strip().lower()
    try:
        valor = round(float(valor_usd), 6)
    except (TypeError, ValueError):
        raise ValidationBlocked("valor_usd inválido.", details={"valor_usd": valor_usd})
    if valor <= 0:
        raise ValidationBlocked(
            "Só se registam entradas positivas; para saídas há registrar_saques.",
            details={"valor_usd": valor},
        )

    if origem_norm == "paypal":
        if not order_id:
            raise ValidationBlocked(
                "Receita PayPal exige order_id: sem ele não há como provar "
                "que o dinheiro entrou.",
                details={"origem": "paypal"},
            )
        captura = paypal_cobranca.capturar(order_id)
        if not captura.get("capturado"):
            return {
                "ok": False,
                "registado": False,
                "motivo": (
                    "PayPal não confirmou a captura; nada foi registado. "
                    "Aprovado não é pago."
                ),
                "captura": captura,
            }
        valor_capturado = float(captura.get("amount_usd") or 0)
        if valor_capturado > 0:
            # O valor da PayPal manda: é o que existe de facto. O valor
            # pedido pelo agente serve de conferência, não de verdade.
            if abs(valor_capturado - valor) > 0.01:
                log.warning(
                    "Receita PayPal %s: agente pediu %.2f, PayPal capturou "
                    "%.2f — registo pelo valor da PayPal.",
                    order_id, valor, valor_capturado,
                )
            valor = valor_capturado
        referencia = referencia or order_id
    elif origem_norm not in {"mpesa", "emola", "manual", "wise"}:
        raise ValidationBlocked(
            f"Origem de receita desconhecida: {origem!r}.",
            details={"origem": origem, "aceites": ["paypal", "mpesa", "emola", "wise", "manual"]},
        )

    divisao = cfo.record_revenue(
        valor,
        source=origem_norm,
        squad=squad,
        description=descricao or f"Receita {origem_norm}",
        currency=moeda,
        external_ref=referencia,
    )
    return {
        "ok": True,
        "registado": True,
        "origem": origem_norm,
        "dividido": divisao.to_dict(),
        "fase": cfo.snapshot().get("phase"),
        "nota": "Divisão aplicada por cfo_treasury (regra única, por fase).",
    }


# ==========================================================================
# Tool 2 — dividir_lucro
# ==========================================================================
def dividir_lucro(*, valor_usd: float, fase: Optional[int] = None) -> Dict[str, Any]:
    """Mostra como um valor se divide — **sem** registar nem mover nada.

    É uma calculadora, de propósito: quem regista receita real é
    ``receber_pagamento``. Um tool que simula não deve ter efeitos.
    """
    try:
        valor = round(float(valor_usd), 6)
    except (TypeError, ValueError):
        raise ValidationBlocked("valor_usd inválido.")
    if valor <= 0:
        raise ValidationBlocked("O valor a dividir deve ser positivo.")

    bruto = valor
    # A taxa vem do livro-razão, não de uma constante local: se o Presidente
    # alterar a provisão fiscal, a simulação acompanha.
    estado_actual = cfo.snapshot()
    tax_rate = float(
        (estado_actual.get("split") or {}).get("tax_reserve_rate")
        or estado_actual.get("tax_reserve_rate")
        or 0.05
    )
    tax = round(bruto * tax_rate, 6)
    liquido = round(bruto - tax, 6)

    if fase is None:
        fase_usada = int(
            estado_actual.get("computed_phase") or estado_actual.get("phase") or 1
        )
        origem_fase = "facturação mensal actual"
    else:
        fase_usada = int(fase)
        origem_fase = "indicada pelo chamador"
        if fase_usada not in (1, 2, 3):
            raise ValidationBlocked("Fase deve ser 1, 2 ou 3.")

    partes = split_for_phase(fase_usada)
    return {
        "ok": True,
        "simulacao": True,
        "bruto_usd": bruto,
        "taxa_provisao_fiscal": tax_rate,
        "provisao_fiscal_usd": tax,
        "liquido_usd": liquido,
        "fase": fase_usada,
        "origem_da_fase": origem_fase,
        "partes": {nome: round(liquido * fracção, 6) for nome, fracção in partes.items()},
        "percentagens": {nome: round(fracção * 100, 2) for nome, fracção in partes.items()},
        "nota": "Nada foi registado. Para entrar no cofre, usar receber_pagamento.",
    }


def fase_actual(monthly_revenue_usd: Optional[float] = None) -> Dict[str, Any]:
    """Fase da holding e o que a mudaria."""
    estado = cfo.snapshot()
    receita = (
        float(monthly_revenue_usd)
        if monthly_revenue_usd is not None
        else float(estado.get("monthly_revenue_usd") or 0)
    )
    return {
        "fase_actual": int(estado.get("phase", 1)),
        "fase_pela_receita": phase_for_monthly_revenue(receita),
        "facturacao_mensal_usd": round(receita, 2),
        "limiares_usd": estado.get("phase_thresholds_usd"),
        "divisao_da_fase_actual": split_for_phase(int(estado.get("phase", 1))),
        "nota": "A fase sobe por facturação mensal; nunca por decisão de um agente.",
    }


# ==========================================================================
# Tool 3 — registrar_saques
# ==========================================================================
def registrar_saques(
    valor_usd: float,
    *,
    canal: str,
    destino: str = "",
    nota: str = "",
    solicitado_por: str = "agente_cfo",
) -> Dict[str, Any]:
    """Cria um **pedido** de saque pendente de aprovação biométrica.

    Este tool nunca envia dinheiro. Cria uma aprovação ``payout`` (crítica),
    reserva os fundos do bolso do Presidente e devolve o que falta para o
    dinheiro sair: o Presidente aprovar no telemóvel.

    Sem ``approval_id`` aprovado com biometria, ``payout_bridge`` recusa a
    execução — a guarda está no sítio certo, não aqui.
    """
    canal_norm = (canal or "").strip().lower()
    if canal_norm not in SUPPORTED_CHANNELS:
        raise ValidationBlocked(
            f"Canal de saque desconhecido: {canal!r}",
            details={"aceites": sorted(SUPPORTED_CHANNELS)},
        )
    try:
        valor = round(float(valor_usd), 6)
    except (TypeError, ValueError):
        raise ValidationBlocked("valor_usd inválido.")
    if valor <= 0:
        raise ValidationBlocked("O saque deve ser positivo.")

    entidade = cfo.snapshot()
    disponivel = float(entidade.get("distributable_owner_usd") or 0)
    if valor > disponivel + 0.01:
        raise ValidationBlocked(
            "O saque excede o que está disponível para o Presidente "
            "(não se toca no cofre de impostos nem no fundo de reinvestimento).",
            details={"pedido_usd": valor, "disponivel_usd": round(disponivel, 2)},
        )

    approval_id = approval_gate.request(
        title=f"Saque {canal_norm} de ${valor:.2f}",
        kind="payout",
        amount_usd=valor,
        reason=nota or f"Pedido de saque por {canal_norm}",
        requested_by=solicitado_por,
        metadata={"channel": canal_norm, "destination": destino},
    )
    return {
        "ok": True,
        "executado": False,
        "approval_id": approval_id,
        "canal": canal_norm,
        "valor_usd": valor,
        "estado": "pendente",
        "o_que_falta": (
            "Aprovação com biometria no telemóvel. Só depois o payout_bridge "
            "executa. Um agente não move dinheiro para fora da holding."
        ),
        "avisos": _avisos_de_saque(canal_norm, destino),
    }


def _avisos_de_saque(canal: str, destino: str) -> List[str]:
    """Os problemas conhecidos de cada canal, ditos antes e não depois."""
    avisos: List[str] = []
    if canal == "paypal":
        avisos.append(
            "PayPal não transfere para bancos moçambicanos. O saldo sai para "
            "Payoneer/Wise, ou fica em PayPal para pagar fornecedores."
        )
    if canal in {"mpesa", "emola"} and not destino:
        avisos.append(
            "Sem número de destino o saque falha no conector: M-Pesa/e-Mola "
            "precisam do MSISDN (ex.: 84xxxxxxx)."
        )
    if canal == "wise":
        avisos.append("Wise exige conta bancária verificada em nome do titular.")
    return avisos


# ==========================================================================
# Registo — o que o CrewAI (e o /agents) vê
# ==========================================================================
@dataclass(frozen=True)
class ToolSpec:
    nome: str
    descricao: str
    funcao: Callable[..., Any]
    move_dinheiro: bool
    requer_aprovacao: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nome": self.nome,
            "descricao": self.descricao,
            "move_dinheiro": self.move_dinheiro,
            "requer_aprovacao": self.requer_aprovacao,
        }


CFO_TOOLS: Dict[str, ToolSpec] = {
    "receber_pagamento": ToolSpec(
        "receber_pagamento",
        "Regista dinheiro recebido (PayPal/M-Pesa/e-Mola/Wise/manual) e "
        "aplica a provisão fiscal e a divisão por fase.",
        receber_pagamento,
        move_dinheiro=False,
        requer_aprovacao=False,
    ),
    "dividir_lucro": ToolSpec(
        "dividir_lucro",
        "Simula a divisão de um valor pela regra da fase, sem registar nada.",
        dividir_lucro,
        move_dinheiro=False,
        requer_aprovacao=False,
    ),
    "registrar_saques": ToolSpec(
        "registrar_saques",
        "Cria um pedido de saque pendente de aprovação biométrica. "
        "Nunca envia dinheiro.",
        registrar_saques,
        move_dinheiro=False,      # reserva, não envia
        requer_aprovacao=True,
    ),
}


def construir_tools_crewai() -> List[Any]:
    """Devolve os tools como objectos CrewAI, se o CrewAI estiver instalado.

    Sem CrewAI, devolve o dicionário de especificação — o mesmo contrato,
    sem a orquestração. O CFO não fica sem ferramentas por falta do extra.
    """
    try:
        from crewai.tools import tool as crewai_tool  # type: ignore
    except Exception:  # noqa: BLE001 - CrewAI é opcional por desenho
        log.info("CrewAI ausente: tools do CFO em modo especificação.")
        return [spec.to_dict() for spec in CFO_TOOLS.values()]

    construidos: List[Any] = []
    for spec in CFO_TOOLS.values():
        try:
            construidos.append(
                crewai_tool(spec.nome)(spec.funcao)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Tool %s não construída por CrewAI: %s", spec.nome, exc)
            construidos.append(spec.to_dict())
    return construidos


def diagnostico() -> Dict[str, Any]:
    """Estado dos tools e das guardas que os limitam."""
    return {
        "tools": [spec.to_dict() for spec in CFO_TOOLS.values()],
        "nenhum_move_dinheiro_sozinho": True,
        "saques_requerem_biometria": True,
        "divisao_vive_em": "backend/finance/cfo_treasury.py",
        "canais_de_saque": sorted(SUPPORTED_CHANNELS),
        "paypal_configurado": bool(getattr(paypal_cobranca, "configurado", False)),
        "crewai_instalado": _crewai_instalado(),
    }


def _crewai_instalado() -> bool:
    try:
        import crewai  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True
