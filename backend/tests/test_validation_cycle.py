"""Testes de validação de demanda, aprovações e ciclo do CEO."""

from __future__ import annotations


def _criar_smoke(client, idea: str) -> str:
    response = client.post(
        "/validation",
        json={
            "idea": idea,
            "niche": "retalho",
            "squad_type": "infoproducts_affiliate",
            "cta": "Quero o guia completo",
            "price_anchor_usd": 12.0,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["test"]["id"]


def test_smoke_test_so_aprova_com_demanda_real(client) -> None:
    test_id = _criar_smoke(
        client, "Curso de controlo de stock para mercearias de Maputo"
    )

    # Sem eventos: continua em curso.
    inicial = client.get(f"/validation/{test_id}").json()
    assert inicial["test"]["status"] == "running"

    # Abaixo da meta: reprovado.
    client.post(f"/validation/{test_id}/event", json={"event": "click", "count": 3})
    abaixo = client.get(f"/validation/{test_id}").json()
    assert abaixo["test"]["status"] in {"running", "failed"}

    # Acima da meta de cliques e conversão: aprovado.
    client.post(f"/validation/{test_id}/event", json={"event": "click", "count": 40})
    client.post(f"/validation/{test_id}/event", json={"event": "signup", "count": 8})
    aprovado = client.get(f"/validation/{test_id}").json()
    assert aprovado["test"]["status"] == "passed"
    assert aprovado["test"]["clicks"] >= 25


def test_landing_page_publica_e_renderizada(client) -> None:
    test_id = _criar_smoke(client, "Guia de Excel financeiro para pequenas lojas de Maputo")
    page = client.get(f"/validation/{test_id}/page")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<form" in page.text.lower() or "button" in page.text.lower()


def test_aprovacao_exige_biometria(client) -> None:
    reserva = client.post(
        "/treasury/payouts/reserve",
        json={"amount_usd": 3.0, "channel": "mpesa", "reason": "teste"},
    )
    assert reserva.status_code == 200, reserva.text
    approval_id = reserva.json()["approval_id"]
    assert approval_id.startswith("apr_")

    sem_biometria = client.post(
        f"/approvals/{approval_id}/decide",
        json={"approve": True, "biometric_verified": False},
    )
    assert sem_biometria.status_code == 428

    com_biometria = client.post(
        f"/approvals/{approval_id}/decide",
        json={"approve": True, "biometric_verified": True},
    )
    assert com_biometria.status_code == 200
    assert com_biometria.json()["item"]["status"] == "approved"


def test_ciclo_do_ceo_completa_em_segundos(client) -> None:
    response = client.post("/cycle/run", json={"force_scan": False, "produce": False})
    assert response.status_code == 200
    report = response.json()
    assert report["status"] in {"completed", "completed_with_errors"}
    assert report["cycle_id"].startswith("cyc_")
    assert report["duration_seconds"] < 120
    passos = {step["step"] for step in report["steps"]}
    # O passo ``kill_switch`` só aparece com o sistema congelado; o ciclo
    # normal cobre higiene, descoberta, contabilidade e consolidação.
    assert {"hygiene", "scout", "validation", "accounting"} <= passos


def test_cemiterio_bloqueia_nicho_morto(client) -> None:
    from backend.core.shared_memory import shared_memory

    shared_memory.record_failure(
        title="Curso de criptomoedas para iniciantes em Maputo",
        niche="cripto",
        squad="infoproducts_affiliate",
        death_cause="sem procura real",
        lessons=["Audiência não compra cursos genéricos de cripto."],
        tags=["infoprodutos", "cripto"],
    )
    check = client.post(
        "/governance/cemetery/check",
        json={"idea": "Curso de criptomoedas para iniciantes em Maputo"},
    )
    assert check.status_code == 200
    body = check.json()
    # O endpoint devolve {"allowed": False, ...} quando bloqueia.
    assert body.get("allowed") is False or body.get("blocked") is True
