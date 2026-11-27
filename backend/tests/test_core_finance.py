"""Testes do núcleo: tesouraria, fases, juiz e regra de abate."""

from __future__ import annotations

import pytest

COPY_APROVAVEL = """Guia pratico de gestao de stock para mercearias de Maputo: 8 capitulos,
planilhas em Excel e checklist de inventario semanal.

Em 2024, 7 em cada 10 mercearias familiares em Maputo perderam dinheiro por rotura de
stock ou excesso de compras. Este guia resolve isso com um metodo simples: contar o
inventario em 3 categorias, calcular o giro a cada 14 dias e encomendar apenas o que
vende em 21 dias.

O que inclui:
1. Ficha de contagem rapida (15 minutos por semana).
2. Planilha de giro com formulas automaticas.
3. Regra de reposicao: quando o stock baixa de 30% do consumo mensal, encomendar.
4. Modelo de etiquetas de preco com margem minima de 25%.

Quem aplicou o metodo em 12 lojas de Maputo reduziu compras paradas em 18% no primeiro
mes, segundo os registos que acompanhamos em 2025.

Afiliação: alguns links desta pagina sao de afiliados e podemos receber comissao.
Resultados variam conforme a execucao de cada loja; nao garantimos rendimentos.
Preco: 12 USD com acesso imediato ao PDF e a planilha."""


def test_health(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "kill_switch" in body
    assert "vault" in body


def test_token_obrigatorio_em_escrita(client) -> None:
    """Sem header X-Automaton-Token, escritas críticas devolvem 401."""
    from fastapi.testclient import TestClient

    from backend.main import app

    # Cliente "nu": sem o header injectado pela fixture.
    anonimo = TestClient(app)
    response = anonimo.post("/kill-switch/engage", json={"reason": "sem token"})
    assert response.status_code == 401


def test_fase_e_divisao_tres_fases(client) -> None:
    from backend.finance.cfo_treasury import PHASES

    assert PHASES[1]["split"] == {
        "owner_pocket": 0.10,
        "savings_vault": 0.20,
        "reinvestment_pool": 0.70,
    }
    assert PHASES[2]["split"] == {
        "owner_pocket": 0.10,
        "savings_vault": 0.40,
        "reinvestment_pool": 0.50,
    }
    assert PHASES[3]["split"] == {
        "owner_pocket": 0.50,
        "savings_vault": 0.30,
        "reinvestment_pool": 0.20,
    }
    assert PHASES[1]["max_usd"] == 1500.0
    assert PHASES[2]["max_usd"] == 15000.0


def test_receita_retencao_fiscal_de_5_por_cento(client) -> None:
    response = client.post(
        "/treasury/revenue",
        json={
            "amount_usd": 100.0,
            "source": "gumroad",
            "squad": "sq_teste",
            "description": "venda de e-book de teste",
        },
    )
    assert response.status_code == 200
    split = response.json()["split"]
    assert split["gross_usd"] == pytest.approx(100.0, abs=1e-6)
    # Provisão fiscal imediata de 5% da receita bruta.
    assert split["tax_usd"] == pytest.approx(5.0, abs=1e-6)
    # Fase 1: 10% bolso / 20% poupança / 70% reinvestimento do líquido.
    assert split["owner_pocket_usd"] == pytest.approx(9.5, abs=1e-6)
    assert split["savings_vault_usd"] == pytest.approx(19.0, abs=1e-6)
    assert split["reinvestment_pool_usd"] == pytest.approx(66.5, abs=1e-6)
    assert split["phase"] == 1


def test_teto_de_gasto_zero_bloqueia(client) -> None:
    """Com DAILY_SPEND_CAP_USD=0.00 nenhum gasto do fundo é permitido."""
    response = client.post(
        "/treasury/spend",
        json={
            "amount_usd": 1.0,
            "agent_id": "scout_hunter",
            "purpose": "teste de trava financeira",
        },
    )
    # 402/409/422 são aceitáveis: o importante é NÃO ser 200.
    assert response.status_code != 200
    assert response.status_code in {402, 409, 422, 428}


def test_juiz_reprova_copy_enganosa_e_aprova_boa(client) -> None:
    fraca = client.post(
        "/validation/judge",
        json={
            "content": "Ganhe 10.000 dólares por dia sem fazer nada! Garantido!!!",
            "content_type": "copy",
        },
    )
    assert fraca.status_code == 200
    assert fraca.json()["passed"] is False

    boa = client.post(
        "/validation/judge",
        json={"content": COPY_APROVAVEL, "content_type": "copy"},
    )
    assert boa.status_code == 200
    assert boa.json()["score"] >= 85.0
    assert boa.json()["passed"] is True


def test_kill_switch_congela_o_ciclo(client) -> None:
    engage = client.post("/kill-switch/engage", json={"reason": "teste automatizado"})
    assert engage.status_code == 200
    assert engage.json()["state"]["engaged"] is True

    cycle = client.post("/cycle/run", json={"force_scan": False, "produce": False})
    assert cycle.status_code == 200
    assert cycle.json()["status"] == "aborted"

    release = client.post("/kill-switch/release")
    assert release.status_code == 200
    assert release.json()["state"]["engaged"] is False
