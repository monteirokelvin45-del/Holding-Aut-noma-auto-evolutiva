"""Os três tools do Agente CFO — e, sobretudo, os seus limites.

O ponto central desta suíte não é "os tools funcionam". É provar que **um
agente de IA não consegue tirar dinheiro da holding**, mesmo que decida
fazê-lo. As asserções que importam são as que verificam recusas.
"""

from __future__ import annotations

import inspect

import pytest

from backend.core.errors import ValidationBlocked
from backend.finance import cfo_tools
from backend.finance.cfo_tools import (
    CFO_TOOLS,
    dividir_lucro,
    fase_actual,
    receber_pagamento,
    registrar_saques,
)


# ==========================================================================
# dividir_lucro — calculadora, não contabilista
# ==========================================================================
def test_divisao_da_fase_1_bate_certo():
    r = dividir_lucro(valor_usd=1000, fase=1)
    assert r["simulacao"] is True
    assert r["fase"] == 1
    assert r["provisao_fiscal_usd"] == 50.0
    assert r["partes"] == {
        "owner_pocket": 95.0,        # 10% de 950
        "savings_vault": 190.0,      # 20%
        "reinvestment_pool": 665.0,  # 70%
    }


def test_as_tres_fases_vao_a_sitios_diferentes():
    """Se as fases dessem o mesmo, a regra não estaria a ser aplicada."""
    fase1 = dividir_lucro(valor_usd=1000, fase=1)["partes"]
    fase2 = dividir_lucro(valor_usd=1000, fase=2)["partes"]
    fase3 = dividir_lucro(valor_usd=1000, fase=3)["partes"]
    assert fase1["owner_pocket"] == 95.0      # 10%
    assert fase2["owner_pocket"] == 95.0      # 10%
    assert fase3["owner_pocket"] == 475.0     # 50%
    assert fase3["reinvestment_pool"] == 190.0  # 20%


def test_dividir_lucro_nao_regista_nada():
    """Um tool de simulação não pode ter efeitos no cofre."""
    fonte = inspect.getsource(dividir_lucro)
    assert "record_revenue" not in fonte
    assert dividir_lucro(valor_usd=10, fase=1)["simulacao"] is True


@pytest.mark.parametrize("fase", [0, 4, -1])
def test_fase_invalida_e_recusada(fase):
    with pytest.raises(ValidationBlocked):
        dividir_lucro(valor_usd=10, fase=fase)


def test_limiares_das_fases_documentados():
    r = fase_actual()
    assert r["limiares_usd"] == {"phase_2": 1500.0, "phase_3": 15000.0}
    assert "por facturação" in r["nota"] or "fase sobe" in r["nota"]


# ==========================================================================
# receber_pagamento — registar sim, inventar não
# ==========================================================================
def test_receita_manual_entra_e_e_dividida():
    r = receber_pagamento(100, origem="manual", squad="teste", descricao="venda")
    assert r["ok"] is True and r["registado"] is True
    dividido = r["dividido"]
    assert dividido["gross_usd"] == 100.0
    assert dividido["tax_usd"] == 5.0
    assert dividido["net_usd"] == 95.0


@pytest.mark.parametrize("valor", [0, -5])
def test_receita_nao_positiva_e_recusada(valor):
    with pytest.raises(ValidationBlocked):
        receber_pagamento(valor, origem="manual")


def test_origem_desconhecida_e_recusada():
    with pytest.raises(ValidationBlocked) as exc:
        receber_pagamento(10, origem="bitcoin")
    assert "bitcoin" in str(exc.value)
    # As origens aceites vão em `details` — é o que a app mostra ao utilizador.
    assert "paypal" in exc.value.details["aceites"]


def test_paypal_sem_order_id_e_recusado():
    """Sem prova de pagamento não se regista receita."""
    with pytest.raises(ValidationBlocked) as exc:
        receber_pagamento(10, origem="paypal")
    assert "order_id" in str(exc.value)


def test_paypal_nao_capturado_nao_regista_receita():
    """Sem credenciais, a captura é dry-run: nada entra no cofre."""
    antes = cfo_tools.cfo.snapshot()["gross_revenue_usd"]
    r = receber_pagamento(50, origem="paypal", order_id="ORDEM-INEXISTENTE")
    assert r["ok"] is False
    assert r["registado"] is False
    assert "Aprovado não é pago" in r["motivo"]
    depois = cfo_tools.cfo.snapshot()["gross_revenue_usd"]
    assert depois == antes, "a receita subiu sem captura confirmada"


# ==========================================================================
# registrar_saques — o teste que mais importa
# ==========================================================================
def test_saque_nunca_executa_so_cria_pedido():
    """Este é o invariante: o tool não move dinheiro, pede autorização."""
    receber_pagamento(500, origem="manual", squad="teste")
    r = registrar_saques(10, canal="mpesa", destino="84xxxxxxx", nota="teste")
    assert r["ok"] is True
    assert r["executado"] is False
    assert r["approval_id"].startswith("apr_")
    assert r["estado"] == "pendente"
    assert "biometria" in r["o_que_falta"]


def test_saque_acima_do_disponivel_e_recusado():
    """Não se toca no cofre de impostos nem no fundo de reinvestimento."""
    with pytest.raises(ValidationBlocked) as exc:
        registrar_saques(999999, canal="mpesa", destino="84xxxxxxx")
    assert "excede" in str(exc.value)


def test_saque_exige_canal_conhecido():
    with pytest.raises(ValidationBlocked) as exc:
        registrar_saques(1, canal="banco_imaginario")
    assert "banco_imaginario" in str(exc.value)
    assert set(exc.value.details["aceites"]) == {"mpesa", "emola", "paypal", "wise"}


def test_saque_por_paypal_avisa_que_mocambique_nao_recebe():
    """O aviso vem antes, não depois do dinheiro ficar preso."""
    receber_pagamento(100, origem="manual", squad="teste")
    r = registrar_saques(1, canal="paypal", destino="x@y.com")
    assert any("Mocambicanos" in a or "moçambicanos" in a for a in r["avisos"])


def test_mpesa_sem_destino_avisa():
    receber_pagamento(100, origem="manual", squad="teste")
    r = registrar_saques(1, canal="mpesa")
    assert any("MSISDN" in a or "destino" in a for a in r["avisos"])


# ==========================================================================
# Registo dos tools
# ==========================================================================
def test_nenhum_tool_diz_mover_dinheiro():
    for spec in CFO_TOOLS.values():
        assert spec.move_dinheiro is False, f"{spec.nome} diz mover dinheiro"


def test_a_divisao_continua_num_so_ficheiro():
    """Duplicar 10/20/70 aqui seria o início de duas verdades."""
    fonte = inspect.getsource(cfo_tools)
    for percentagem in ("0.70", "0.10", "0.20", "0.40", "0.50", "0.30"):
        assert percentagem not in fonte, f"percentagem {percentagem} duplicada"


def test_construir_tools_sem_crewai_nao_rebenta():
    tools = cfo_tools.construir_tools_crewai()
    assert len(tools) == 3
    nomes = {(t.get("nome") if isinstance(t, dict) else getattr(t, "name", "")) for t in tools}
    assert {"receber_pagamento", "dividir_lucro", "registrar_saques"} <= nomes
