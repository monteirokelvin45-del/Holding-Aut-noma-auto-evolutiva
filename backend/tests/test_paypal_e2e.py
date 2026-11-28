"""Caminho PayPal completo, de ponta a ponta, com o SDK oficial a correr.

O que torna esta suíte diferente de `test_paypal.py`
---------------------------------------------------
Aquele ficheiro testa o contrato em dry-run. Este **não** tem dry-run: há um
servidor PayPal falso local, o SDK oficial fala HTTP com ele, e o parsing das
respostas é feito pelo código da PayPal. Foi assim que se descobriram seis
defeitos que o dry-run escondia (assinatura do cliente, enum de ambiente,
`options={}`, chave `id`, atributo de classe `environments`).

O que isto **não** prova
------------------------
Que a PayPal real aceita estes pedidos. O servidor é uma imitação e está
marcado com `X-Fake-PayPal`. A conta sandbox verdadeira é o §8 de
docs/pagamentos.md — seis passos, nenhum automatizável daqui.
"""

from __future__ import annotations

import os

import pytest

from backend.tests.fake_paypal import FakePayPal


@pytest.fixture()
def paypal_falso(monkeypatch: pytest.MonkeyPatch):
    """Servidor PayPal falso + cliente apontado para ele.

    `PAYPAL_BASE_URL` é o gancho de teste; sem ele o SDK fala com a PayPal a
    sério. As credenciais são falsas de propósito: nada aqui toca contas reais.
    """
    with FakePayPal() as fake:
        monkeypatch.setenv("PAYPAL_CLIENT_ID", "fake-client-id")
        monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "fake-secret")
        monkeypatch.setenv("PAYPAL_MODE", "sandbox")
        monkeypatch.setenv("PAYPAL_BASE_URL", fake.url)

        from backend.config.settings import settings

        # `Settings` é frozen (dataclass), por isso `monkeypatch.setattr`
        # rebenta com FrozenInstanceError. Aqui força-se e restaura-se à mão,
        # guardando os valores originais.
        originais = {
            "paypal_client_id": settings.paypal_client_id,
            "paypal_client_secret": settings.paypal_client_secret,
            "paypal_mode": settings.paypal_mode,
        }
        object.__setattr__(settings, "paypal_client_id", "fake-client-id")
        object.__setattr__(settings, "paypal_client_secret", "fake-secret")
        object.__setattr__(settings, "paypal_mode", "sandbox")

        from backend.finance import paypal_cobranca as modulo

        modulo.paypal_cobranca._cliente = None
        try:
            yield fake
        finally:
            for campo, valor in originais.items():
                object.__setattr__(settings, campo, valor)
            modulo.paypal_cobranca._cliente = None


# ==========================================================================
# 1. O caminho feliz, tal como acontece na realidade
# ==========================================================================
def test_criar_aprovar_capturar(paypal_falso):
    from backend.finance.paypal_cobranca import paypal_cobranca as pp

    cobranca = pp.criar_cobranca(valor_usd=19.90, descricao="Guia de freelancing")
    assert cobranca.status == "CREATED"
    assert cobranca.order_id.startswith("FAKE")
    assert cobranca.dry_run is False
    assert "checkoutnow" in cobranca.approval_url
    assert cobranca.pago is False, "criado não é pago"

    assert pp.estado(cobranca.order_id) == "CREATED"

    assert paypal_falso.aprovar(cobranca.order_id) is True
    assert pp.estado(cobranca.order_id) == "APPROVED"

    captura = pp.capturar(cobranca.order_id)
    assert captura["capturado"] is True
    assert captura["status"] == "COMPLETED"
    assert captura["amount_usd"] == pytest.approx(19.90)


def test_sem_aprovacao_a_paypal_recusa(paypal_falso):
    """O dinheiro só entra depois de o comprador aprovar. Não é opcional."""
    from backend.core.errors import ExternalServiceError
    from backend.finance.paypal_cobranca import paypal_cobranca as pp

    cobranca = pp.criar_cobranca(valor_usd=5.0, descricao="x")
    with pytest.raises(ExternalServiceError) as exc:
        pp.capturar(cobranca.order_id)
    # A PayPal responde ORDER_NOT_APPROVED; a mensagem tem de chegar ao log.
    assert "ORDER_NOT_APPROVED" in str(exc.value) or "422" in str(exc.value)


def test_pedido_inexistente_falha_limpo(paypal_falso):
    from backend.core.errors import ExternalServiceError
    from backend.finance.paypal_cobranca import paypal_cobranca as pp

    with pytest.raises(ExternalServiceError):
        pp.estado("NAO-EXISTE-123")


# ==========================================================================
# 2. Retry: o caso que estragava o livro-razão
# ==========================================================================
def test_capturar_duas_vezes_e_idempotente(paypal_falso):
    """Um retry devolve ORDER_ALREADY_CAPTURED — e isso não é falha.

    Tratar a recusa como erro deixava a receita por registar para sempre,
    porque a segunda tentativa é exactamente a que acontece quando a rede
    falha na primeira.
    """
    from backend.finance.paypal_cobranca import paypal_cobranca as pp

    cobranca = pp.criar_cobranca(valor_usd=12.5, descricao="x")
    paypal_falso.aprovar(cobranca.order_id)
    primeira = pp.capturar(cobranca.order_id)
    segunda = pp.capturar(cobranca.order_id)

    assert primeira["capturado"] is True
    assert segunda["capturado"] is True, "o retry devia ser tratado como sucesso"
    assert segunda["amount_usd"] == pytest.approx(12.5), "o montante vem do pedido"
    assert "retry" in segunda["message"]


def test_receita_com_a_mesma_referencia_nao_duplica(paypal_falso):
    """O cofre não pode acreditar que recebeu o dobro."""
    from backend.finance.cfo_tools import receber_pagamento
    from backend.finance.cfo_treasury import cfo
    from backend.finance.paypal_cobranca import paypal_cobranca as pp

    cobranca = pp.criar_cobranca(valor_usd=19.90, descricao="e-book")
    paypal_falso.aprovar(cobranca.order_id)

    primeiro = receber_pagamento(19.90, origem="paypal", order_id=cobranca.order_id)
    segundo = receber_pagamento(19.90, origem="paypal", order_id=cobranca.order_id)

    assert primeiro["dividido"]["gross_usd"] == pytest.approx(19.90)
    assert segundo["dividido"]["gross_usd"] == pytest.approx(19.90)
    assert primeiro["dividido"]["event_id"] == segundo["dividido"]["event_id"], (
        "o segundo registo devia devolver o evento existente, não criar outro"
    )
    assert cfo.snapshot()["gross_revenue_usd"] == pytest.approx(19.90), (
        "receita acumulada devia ser 19.90, não 39.80"
    )


def test_a_divisao_por_fase_aplica_se_a_receita_da_paypal(paypal_falso):
    """O dinheiro da PayPal entra no cofre pelas mesmas regras de todos."""
    from backend.finance.cfo_tools import receber_pagamento
    from backend.finance.paypal_cobranca import paypal_cobranca as pp

    cobranca = pp.criar_cobranca(valor_usd=100.0, descricao="curso")
    paypal_falso.aprovar(cobranca.order_id)
    r = receber_pagamento(100.0, origem="paypal", order_id=cobranca.order_id, squad="infoprodutos")
    d = r["dividido"]
    assert d["tax_usd"] == pytest.approx(5.0)          # 5% de provisão fiscal
    assert d["net_usd"] == pytest.approx(95.0)
    assert d["phase"] in (1, 2, 3)


def test_valor_divergente_usa_o_da_paypal(paypal_falso):
    """Se o agente disser 50 e a PayPal capturar 19.90, manda a PayPal."""
    from backend.finance.cfo_tools import receber_pagamento
    from backend.finance.paypal_cobranca import paypal_cobranca as pp

    cobranca = pp.criar_cobranca(valor_usd=19.90, descricao="e-book")
    paypal_falso.aprovar(cobranca.order_id)
    r = receber_pagamento(50.0, origem="paypal", order_id=cobranca.order_id)
    assert r["dividido"]["gross_usd"] == pytest.approx(19.90), (
        "o valor inventado pelo agente não pode sobrepor-se ao capturado"
    )


# ==========================================================================
# 3. O servidor falso não é décor: tem de recusar o que a PayPal recusa
# ==========================================================================
def test_o_falso_recusa_valor_mal_formado(paypal_falso):
    """Prova que o servidor de teste não aceita tudo (senão não testa nada)."""
    import json
    import urllib.request

    pedido = urllib.request.Request(
        f"{paypal_falso.url}/v2/checkout/orders",
        data=json.dumps(
            {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": "USD", "value": "19.9"}}]}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(pedido)
    assert exc.value.code == 422


def test_o_falso_nao_aceita_capturar_sem_aprovacao(paypal_falso):
    import json
    import urllib.request

    with urllib.request.urlopen(
        urllib.request.Request(
            f"{paypal_falso.url}/v2/checkout/orders",
            data=json.dumps(
                {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": "USD", "value": "1.00"}}]}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    ) as resposta:
        order_id = json.loads(resposta.read())["id"]

    pedido = urllib.request.Request(
        f"{paypal_falso.url}/v2/checkout/orders/{order_id}/capture",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(pedido)
    assert exc.value.code == 422


def test_um_so_token_oauth_para_todas_as_chamadas(paypal_falso):
    """O SDK reaproveita o token; pedir um por chamada seria desperdício."""
    from backend.finance.paypal_cobranca import paypal_cobranca as pp

    for valor in (1.0, 2.0, 3.0):
        pp.criar_cobranca(valor_usd=valor, descricao=f"item {valor}")
    assert len(paypal_falso.estado.tokens_emitidos) == 1
