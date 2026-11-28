"""Cobrança PayPal: o lado internacional do cofre.

O que estes testes provam — e o que deliberadamente NÃO provam
--------------------------------------------------------------
Não há rede para a PayPal neste sandbox, e não há credenciais. Tudo o que
aqui está é o **contrato** do módulo: validação, dry-run honesto, leitura
do toolkit oficial e a separação entre "aprovado" e "pago". Nenhum teste
afirma que um pagamento foi processado, porque nenhum foi.

As versões dos pacotes foram verificadas no PyPI antes de escrever o
módulo (2026-11-28): `paypal-server-sdk` 2.4.0 e `paypal-agent-toolkit`
1.11.0 estão activos; `paypalrestsdk` e `paypal-checkout-serversdk` têm
"Deprecated" como resumo literal.
"""

from __future__ import annotations

import pytest

from backend.core.errors import ValidationBlocked
from backend.finance.paypal_cobranca import PayPalCobranca, paypal_cobranca


# --------------------------------------------------------------------------
# 1. Sem credenciais: dry-run honesto, nunca fingir um pagamento
# --------------------------------------------------------------------------
def test_sem_credenciais_e_dry_run_e_nao_inventa_order_id():
    cobranca = paypal_cobranca.criar_cobranca(
        valor_usd=19.90, descricao="Guia de freelancing (e-book)"
    )
    assert cobranca.dry_run is True
    assert cobranca.ok is False
    # Um `order_id` inventado podia ser lido como pedido real por quem
    # consultasse o cofre. Fica vazio, e o motivo fica escrito.
    assert cobranca.order_id == ""
    assert "sem credenciais" in cobranca.message
    assert cobranca.pago is False


def test_dry_run_devolve_o_payload_que_teria_enviado():
    """O payload é auditável: dá para ver o que a PayPal receberia."""
    cobranca = paypal_cobranca.criar_cobranca(
        valor_usd=5.0, descricao="Teste", reference_id="ref-abc"
    )
    payload = cobranca.raw["payload"]
    assert payload["intent"] == "CAPTURE"
    unidade = payload["purchase_units"][0]
    assert unidade["reference_id"] == "ref-abc"
    assert unidade["amount"] == {"currency_code": "USD", "value": "5.00"}
    assert "PAYPAL_CLIENT_ID" in cobranca.raw["faltam"]


# --------------------------------------------------------------------------
# 2. Validação: o que não pode entrar
# --------------------------------------------------------------------------
@pytest.mark.parametrize("valor", [0, -1, -0.01])
def test_valor_nao_positivo_e_recusado(valor):
    with pytest.raises(ValidationBlocked):
        paypal_cobranca.criar_cobranca(valor_usd=valor, descricao="x")


def test_moeda_fora_da_lista_e_recusada():
    """MZN não é cobrável por PayPal; aceitá-lo daria erro na PayPal."""
    with pytest.raises(ValidationBlocked) as exc:
        paypal_cobranca.criar_cobranca(
            valor_usd=10, descricao="x", moeda="MZN"
        )
    assert "MZN" in str(exc.value)


# --------------------------------------------------------------------------
# 3. "Aprovado" não é "pago" — a distinção que evita contabilizar fumo
# --------------------------------------------------------------------------
def test_aprovado_nao_conta_como_pago():
    from backend.finance.paypal_cobranca import Cobranca

    aprovado = Cobranca(ok=True, status="APPROVED", amount_usd=10)
    assert aprovado.pago is False
    completo = Cobranca(ok=True, status="COMPLETED", amount_usd=10)
    assert completo.pago is True
    # E um COMPLETED em dry-run nunca é receita.
    simulado = Cobranca(ok=True, status="COMPLETED", amount_usd=10, dry_run=True)
    assert simulado.pago is False


# --------------------------------------------------------------------------
# 4. Toolkit oficial: as 42 ferramentas, lidas do pacote
# --------------------------------------------------------------------------
def test_toolkit_oficial_expoe_as_ferramentas_reais():
    """As ferramentas são lidas do pacote, não copiadas para o código."""
    ferramentas = PayPalCobranca.ferramentas_do_agente()
    if not ferramentas:          # toolkit não instalado neste ambiente
        pytest.skip("paypal-agent-toolkit não instalado")
    metodos = {f["metodo"] for f in ferramentas}
    assert len(ferramentas) >= 40, f"esperava >=40 ferramentas, vi {len(ferramentas)}"
    for esperado in ("create_order", "create_invoice", "list_transactions"):
        assert esperado in metodos
    assert all(f["nome"] for f in ferramentas)


def test_toolkit_recusa_executar_sem_credenciais():
    """Não finge que executou: diz o que falta."""
    if not PayPalCobranca.ferramentas_do_agente():
        pytest.skip("paypal-agent-toolkit não instalado")
    if paypal_cobranca.configurado:
        pytest.skip("ambiente com credenciais: este caso é o do dry-run")
    with pytest.raises(ValidationBlocked) as exc:
        paypal_cobranca.executar_ferramenta("create_order", {})
    assert "PAYPAL_CLIENT_ID" in str(exc.value)


# --------------------------------------------------------------------------
# 5. Diagnóstico: o problema do saque fica escrito, não escondido
# --------------------------------------------------------------------------
def test_diagnostico_admite_que_o_saque_para_mocambique_nao_esta_resolvido():
    d = paypal_cobranca.diagnostico()
    assert d["sdk_oficial_instalado"] is True
    assert d["moedas"] == ["USD", "EUR"]
    assert "NÃO RESOLVIDO" in d["sacar_para_mocambique"]
    assert "Wise" in d["sacar_para_mocambique"]
    # Os dois pacotes descontinuados têm de estar nomeados, para ninguém
    # seguir um tutorial antigo e instalar o que a PayPal já apagou.
    assert "paypalrestsdk" in d["descontinuados_a_evitar"]


def test_o_import_do_sdk_oficial_e_paypalserversdk():
    """O nome real do pacote não é o que a documentação escreve.

    `from paypal_server_sdk.models import ...` — como aparece em vários
    exemplos — dá ModuleNotFoundError: o top_level.txt do pacote diz
    `paypalserversdk`. Este teste fixa o nome que funciona.
    """
    import importlib

    assert importlib.util.find_spec("paypalserversdk") is not None
    assert importlib.util.find_spec("paypal_server_sdk") is None


# --------------------------------------------------------------------------
# 6. A regra de divisão não é duplicada aqui
# --------------------------------------------------------------------------
def test_a_divisao_por_fase_continua_a_viver_so_no_cfo():
    """Este módulo recebe dinheiro; não o divide.

    Duplicar 10/20/70 num segundo sítio garantiria que, mais tarde, os dois
    sítios discordassem — e o cofre passaria a ter duas verdades.
    """
    import inspect

    from backend.finance import paypal_cobranca as modulo

    fonte = inspect.getsource(modulo)
    assert "0.70" not in fonte and "0.7," not in fonte
    assert "reinvestment_pool" not in fonte
