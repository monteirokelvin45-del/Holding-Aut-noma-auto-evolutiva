"""Contrato HTTP das rotas novas, e a auditoria do que a app consome.

Estes testes olham para o que fica *fora* do processo: o que o APK recebe, o
que acontece com o token em falta, e se as rotas de leitura deixam escapar
alguma coisa. Um endpoint que rebenta só se descobre no telemóvel do
Presidente.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from backend.main import app

TOKEN = "test-token"


@pytest.fixture(scope="module")
def cliente() -> TestClient:
    return TestClient(app)


# ==========================================================================
# 1. Rotas de leitura: sem token, sempre disponíveis
# ==========================================================================
@pytest.mark.parametrize(
    "rota",
    ["/paypal", "/paypal/ferramentas", "/finance/cfo-tools", "/agents/tools"],
)
def test_rotas_de_leitura_respondem(cliente: TestClient, rota: str) -> None:
    resposta = cliente.get(rota)
    assert resposta.status_code == 200, f"{rota} -> {resposta.status_code}"
    assert isinstance(resposta.json(), dict)


def test_paypal_declara_o_problema_do_saque(cliente: TestClient) -> None:
    """O aviso de que a PayPal não paga a Moçambique tem de estar na API."""
    dados = cliente.get("/paypal").json()
    assert "NÃO RESOLVIDO" in dados["sacar_para_mocambique"]
    assert dados["moedas"] == ["USD", "EUR"]


def test_ferramentas_do_agente_tem_contagem_coerente(cliente: TestClient) -> None:
    dados = cliente.get("/paypal/ferramentas").json()
    assert dados["total"] == len(dados["ferramentas"])
    if dados["ferramentas"]:
        nomes = {f["metodo"] for f in dados["ferramentas"]}
        assert "create_order" in nomes


def test_agents_tools_nao_mente(cliente: TestClient) -> None:
    dados = cliente.get("/agents/tools").json()
    global_ = dados["estado_global"]
    assert global_["prontas"] + global_["degradadas"] + global_["em_falta"] == global_["total"]
    for falta in ("metagpt_pipeline",):
        if falta in global_["lista_em_falta"]:
            assert global_["detalhe_em_falta"][falta]["motivo"]


# ==========================================================================
# 2. Rotas de escrita: exigem token
# ==========================================================================
def test_cobranca_sem_token_e_recusada(cliente: TestClient) -> None:
    resposta = cliente.post(
        "/paypal/cobrancas", json={"valor_usd": 10, "descricao": "teste"}
    )
    assert resposta.status_code in (401, 403), (
        f"sem token devia recusar, devolveu {resposta.status_code}"
    )


def test_cobranca_com_token_em_dry_run(cliente: TestClient) -> None:
    resposta = cliente.post(
        "/paypal/cobrancas",
        json={"valor_usd": 19.90, "descricao": "Guia de freelancing"},
        headers={"X-Automaton-Token": TOKEN},
    )
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["dry_run"] is True
    assert corpo["pago"] is False
    assert corpo["order_id"] == "", "não pode inventar um order_id"


@pytest.mark.parametrize(
    "payload",
    [
        {"valor_usd": 0, "descricao": "zero"},
        {"valor_usd": -5, "descricao": "negativo"},
        {"valor_usd": 10, "descricao": "x"},           # descrição demasiado curta
        {"valor_usd": 10, "descricao": "ok", "moeda": "MZN"},
    ],
)
def test_cobranca_invalida_e_recusada(cliente: TestClient, payload: Dict[str, Any]) -> None:
    resposta = cliente.post(
        "/paypal/cobrancas", json=payload, headers={"X-Automaton-Token": TOKEN}
    )
    assert response_code_invalido(resposta.status_code), (
        f"{payload} devia ser recusado, devolveu {resposta.status_code}: {resposta.text[:120]}"
    )


def response_code_invalido(codigo: int) -> bool:
    return 400 <= codigo < 500


def test_captura_de_pedido_inexistente_nao_inventa_receita(cliente: TestClient) -> None:
    resposta = cliente.post(
        "/paypal/capturar",
        json={"order_id": "NAO-EXISTE", "registar_receita": True},
        headers={"X-Automaton-Token": TOKEN},
    )
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["ok"] is False
    assert corpo["receita_registada"] is None


# ==========================================================================
# 3. A app: nenhum endpoint pode devolver segredos
# ==========================================================================
def test_nenhuma_rota_expoe_credenciais(cliente: TestClient) -> None:
    """Segredos em respostas JSON são a fuga clássica por descuido."""
    suspeitos = ("secret", "token", "password", "api_key", "credential")
    for rota in ("/paypal", "/paypal/ferramentas", "/finance/cfo-tools", "/agents/tools"):
        texto = cliente.get(rota).text.lower()
        for palavra in suspeitos:
            if palavra in texto:
                # Só é aceitável se for um *nome de campo* como
                # `paypal_client_secret: false`, nunca um valor.
                dados = json.loads(cliente.get(rota).text)
                assert not _tem_valor_de_segredo(dados, palavra), (
                    f"{rota} parece expor {palavra}"
                )


def _tem_valor_de_segredo(dados: Any, palavra: str) -> bool:
    """Procura a palavra como chave com valor não-booleano e não-vazio."""
    if isinstance(dados, dict):
        for chave, valor in dados.items():
            if palavra in str(chave).lower() and isinstance(valor, str) and valor:
                if len(valor) > 8:  # `"paypal"` não é um segredo
                    return True
            if _tem_valor_de_segredo(valor, palavra):
                return True
    elif isinstance(dados, list):
        return any(_tem_valor_de_segredo(item, palavra) for item in dados)
    return False


# ==========================================================================
# 4. OpenAPI: as rotas novas estão documentadas
# ==========================================================================
def test_as_rotas_novas_aparecem_no_openapi(cliente: TestClient) -> None:
    especificacao = cliente.get("/openapi.json").json()
    caminhos = set(especificacao["paths"])
    for rota in ("/paypal", "/paypal/ferramentas", "/paypal/cobrancas",
                 "/paypal/capturar", "/finance/cfo-tools", "/agents/tools"):
        assert rota in caminhos, f"{rota} não está no OpenAPI"
