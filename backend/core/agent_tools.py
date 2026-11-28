"""As ferramentas dos agentes, com as guardas que o Jurídico exigiu.

A regra que este ficheiro implementa
------------------------------------
    «Toda tool que gasta dinheiro ou publica conteúdo passa pelo
     `pedir_aprovacao` antes.»

Uma ferramenta de agente pode fazer três tipos de coisa:

* **ler** — consultar ficheiros, listar repos, ver métricas. Não muda o
  mundo, corre directamente.
* **escrever** — criar um rascunho, um ficheiro interno. Muda coisas
  reversíveis; corre, mas fica registado.
* **sensível** — gasta dinheiro, publica para fora, ou fala em nome da
  holding. **Não corre.** Cria um pedido pendente e para.

A terceira categoria é a que interessa. Um agente que publica um vídeo
errado num canal público não pode ser corrigido com um `git revert` — e um
que gasta 200 dólares em anúncios também não.

Como se decide a categoria
--------------------------
Por regras sobre o nome e a descrição da ferramenta, e por uma lista
explícita de verbos. Não é infalível: um servidor MCP malicioso pode chamar
a sua ferramenta de `read_file` e apagar um repositório. Por isso há duas
travessias de segurança além da heurística:

1. O próprio servidor pode ser marcado ``sensitive_action`` em
   ``mcp_servers.json`` — aí **todas** as suas tools passam a sensíveis.
2. A heurística prefere sempre o lado seguro: em dúvida, sensível.

Guardar o que se recusou é tão importante como o que se permitiu: o registo
de decisões é o que permite a quem revê ver se a regra está a ser aplicada
com bom senso ou a bloquear tudo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config.settings import settings
from .approvals import approval_gate
from .logger import get_logger
from .tool_registry import TOOLS

log = get_logger("core.agent_tools")


# ==========================================================================
# Classificação
# ==========================================================================
#: Verbos que implicam gastar dinheiro ou comprometer a holding.
VERBOS_SENSIVEIS = frozenset(
    {
        "pay", "pagar", "purchase", "comprar", "charge", "cobrar", "checkout",
        "refund", "reembolsar", "transfer", "transferir", "payout", "saque",
        "withdraw", "levantar", "spend", "gastar", "facturar",
        "publish", "publicar", "post", "deploy", "send", "enviar", "email",
        "tweet", "upload", "enviar_video", "broadcast", "release", "grant",
        "delete", "apagar", "remove", "remover", "kill", "matar", "revoke",
        "hire", "contratar", "fire", "despedir", "subscribe", "subscrever",
        # `create_payment` escapava como "escrita": `create` sozinho não é
        # sensível (criar uma nota não é criar uma cobrança), mas junto de
        # dinheiro é. Ver SUFIXOS_SENSIVEIS abaixo.
        "create_payment", "create_order", "create_invoice", "create_subscription",
        "create_charge", "create_refund", "criar_cobranca", "criar_pedido",
    }
)

#: Substantivos que tornam sensível um verbo inofensivo. `create_note` é
#: escrita; `create_payment` não é. Sem isto, `create_*` passava ao lado.
SUBSTANTIVOS_SENSIVEIS = (
    "payment", "pagamento", "order", "pedido", "invoice", "factura",
    "charge", "cobranca", "cobrança", "subscription", "subscricao",
    "refund", "reembolso", "transfer", "transferencia", "payout", "saque",
    "withdraw", "levantamento", "purchase", "compra", "billing",
)

#: Verbos de leitura (manuais e baratos).
VERBOS_LEITURA = frozenset(
    {
        "read", "ler", "list", "listar", "get", "obter", "search", "procurar",
        "find", "procurar", "describe", "descrever", "status", "health",
        "metrics", "metricas", "count", "contar", "inspect", "ver", "view",
        "fetch", "query", "consultar", "check", "verificar", "scan", "info",
    }
)

#: Ferramentas do registo interno que já se sabe serem sensíveis.
SENSIVEIS_CONHECIDAS = frozenset(
    {
        "registrar_saques",      # cria pedido de saque (e já pede aprovação)
        "payout_bridge",         # executa saques
        "paypal_cobranca",       # cria cobranças em nome da holding
        "telegram_bot",          # fala em nome da holding com pessoas
        "kill_switch",           # para subsistemas
        "cycle_runner",          # corre um ciclo que pode publicar
    }
)

ACOES = ("ler", "escrever", "sensivel")


@dataclass
class Decisao:
    """O que se decidiu sobre uma ferramenta, e porquê."""

    ferramenta: str
    acao: str
    motivo: str
    requer_aprovacao: bool = False
    servidor: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ferramenta": self.ferramenta,
            "acao": self.acao,
            "motivo": self.motivo,
            "requer_aprovacao": self.requer_aprovacao,
            "servidor": self.servidor,
        }


def classificar(nome: str, descricao: str = "", *, servidor: str = "",
                sensivel_por_servidor: bool = False) -> Decisao:
    """Decide se uma ferramenta é de leitura, escrita ou sensível.

    A ordem das regras importa: a mais restritiva ganha. Um nome como
    ``send_report`` tem "send" (sensível) e "report" (leitura) — e fica
    sensível. É o lado certo para errar.
    """
    apelido = nome.lower().replace("__", " ").replace("_", " ").replace("-", " ")
    palavras = set(apelido.split())

    if sensivel_por_servidor:
        return Decisao(
            ferramenta=nome,
            acao="sensivel",
            motivo="o servidor está marcado como sensitive_action",
            requer_aprovacao=True,
            servidor=servidor,
        )

    if nome in SENSIVEIS_CONHECIDAS or nome.split("__")[-1] in SENSIVEIS_CONHECIDAS:
        return Decisao(
            ferramenta=nome,
            acao="sensivel",
            motivo="consta da lista de ferramentas sensíveis conhecidas",
            requer_aprovacao=True,
            servidor=servidor,
        )

    # Substantivo de dinheiro + verbo de movimento/escrita = sensível.
    # `create_payment` é sensível; `get_order_details` e `list_transactions`
    # são leitura e não podem ser bloqueados — bloquear consultas é a forma
    # mais rápida de o utilizador desligar a regra toda.
    substantivos = sorted({s for s in SUBSTANTIVOS_SENSIVEIS if s in apelido})
    if substantivos and not (palavras & VERBOS_LEITURA):
        return Decisao(
            ferramenta=nome,
            acao="sensivel",
            motivo=f"mexe em dinheiro: {', '.join(substantivos)}",
            requer_aprovacao=True,
            servidor=servidor,
        )

    sensiveis = palavras & VERBOS_SENSIVEIS
    if sensiveis:
        return Decisao(
            ferramenta=nome,
            acao="sensivel",
            motivo=f"verbo de acção sensível: {', '.join(sorted(sensiveis))}",
            requer_aprovacao=True,
            servidor=servidor,
        )

    # Uma descrição que fala de dinheiro ou publicação também conta: há
    # ferramentas com nomes inocentes (`run`, `apply`) que fazem isso.
    texto = (descricao or "").lower()
    gatilhos = [
        g for g in ("payment", "pagamento", "charge", "publish", "publicar",
                    "send email", "envia email", "spend", "gasta")
        if g in texto
    ]
    if gatilhos:
        return Decisao(
            ferramenta=nome,
            acao="sensivel",
            motivo=f"a descrição refere: {', '.join(gatilhos)}",
            requer_aprovacao=True,
            servidor=servidor,
        )

    if palavras & VERBOS_LEITURA:
        return Decisao(
            ferramenta=nome,
            acao="ler",
            motivo="verbo de leitura",
            servidor=servidor,
        )

    return Decisao(
        ferramenta=nome,
        acao="escrever",
        motivo="sem verbo reconhecido: tratada como escrita (reversível)",
        servidor=servidor,
    )


# ==========================================================================
# Execução com guardas
# ==========================================================================
def pedir_aprovacao(
    *,
    ferramenta: str,
    argumentos: Dict[str, Any],
    motivo: str,
    valor_usd: float = 0.0,
    solicitado_por: str = "agente",
    kind: str = "spend",
) -> str:
    """Cria um pedido pendente e devolve o id.

    Não executa nada. O pedido espera pelo Presidente — é essa a diferença
    entre uma ferramenta que pede e uma que faz.
    """
    seguro_kind = kind if kind in {
        "spend", "payout", "production", "license", "custom"
    } else "custom"
    return approval_gate.request(
        title=f"{ferramenta} ({motivo})"[:120],
        kind=seguro_kind,
        amount_usd=max(0.0, float(valor_usd or 0.0)),
        reason=motivo,
        requested_by=solicitado_por,
        metadata={
            "ferramenta": ferramenta,
            "argumentos": {
                k: (str(v)[:200] if not isinstance(v, (int, float, bool)) else v)
                for k, v in (argumentos or {}).items()
            },
        },
    )


def executar(
    nome: str,
    argumentos: Optional[Dict[str, Any]] = None,
    *,
    agente: str = "agente",
    valor_usd: float = 0.0,
    descricao: str = "",
) -> Dict[str, Any]:
    """Ponto único por onde um agente usa uma ferramenta.

    Devolve sempre um dicionário com ``executado``: um agente que recebe
    ``None`` e continua a inventar resultados é pior do que um que recebe
    um "não" claro.
    """
    argumentos = argumentos or {}
    decisao = classificar(nome, descricao)

    if decisao.acao == "sensivel":
        approval_id = pedir_aprovacao(
            ferramenta=nome,
            argumentos=argumentos,
            motivo=decisao.motivo,
            valor_usd=valor_usd,
            solicitado_por=agente,
            kind="payout" if ("saque" in nome or "payout" in nome) else "spend",
        )
        log.warning(
            "Ferramenta sensível %s pedida por %s -> aprovação %s",
            nome, agente, approval_id,
        )
        return {
            "executado": False,
            "motivo": decisao.motivo,
            "approval_id": approval_id,
            "o_que_falta": (
                "O Presidente tem de aprovar. Uma ferramenta que gasta "
                "dinheiro ou publica não corre sozinha."
            ),
            "decisao": decisao.to_dict(),
        }

    entrada = TOOLS.get(nome)
    if entrada is None:
        return {
            "executado": False,
            "motivo": f"ferramenta não registada: {nome!r}",
            "decisao": decisao.to_dict(),
        }
    funcao = entrada.funcao()
    if funcao is None:
        return {
            "executado": False,
            "motivo": entrada.motivo_em_falta or "sem implementação",
            "decisao": decisao.to_dict(),
        }

    try:
        resultado = funcao(**argumentos)
    except TypeError as exc:
        return {
            "executado": False,
            "motivo": f"argumentos não batem com a assinatura: {exc}",
            "decisao": decisao.to_dict(),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("Ferramenta %s falhou: %s", nome, exc)
        return {
            "executado": False,
            "motivo": f"{type(exc).__name__}: {exc}",
            "decisao": decisao.to_dict(),
        }
    return {"executado": True, "resultado": resultado, "decisao": decisao.to_dict()}


# ==========================================================================
# Diagnóstico
# ==========================================================================
def auditar(ferramentas: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Classifica uma lista de ferramentas (do registo ou de um MCP)."""
    decisoes = [
        classificar(
            str(f.get("nome") or ""),
            str(f.get("descricao") or ""),
            servidor=str(f.get("servidor") or ""),
            sensivel_por_servidor=bool(f.get("acao_sensivel")),
        )
        for f in ferramentas
    ]
    contagem = {acao: 0 for acao in ACOES}
    for decisao in decisoes:
        contagem[decisao.acao] += 1
    return {
        "total": len(decisoes),
        "contagem": contagem,
        "pedem_aprovacao": [d.to_dict() for d in decisoes if d.requer_aprovacao],
        "nota": (
            "`sensivel` não bloqueia para sempre: cria um pedido pendente. "
            "A ferramenta existe; quem decide se corre é o Presidente."
        ),
    }


def diagnostico() -> Dict[str, Any]:
    """Estado das guardas: limiar, orçamento e o que está classificado."""
    registo = [
        {
            "nome": nome,
            "descricao": entrada.descricao,
            "acao_sensivel": nome in SENSIVEIS_CONHECIDAS,
        }
        for nome, entrada in TOOLS.items()
    ]
    resultado = auditar(registo)
    resultado.update(
        {
            "limiar_aprovacao_usd": settings.approval_threshold_usd,
            "tecto_diario_usd": settings.daily_spend_cap_usd,
            "regra": (
                "Gastar dinheiro ou publicar conteúdo exige aprovação do "
                "Presidente. Ler e escrever internamente não."
            ),
        }
    )
    return resultado
