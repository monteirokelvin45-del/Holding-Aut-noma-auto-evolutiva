"""Caos e concorrência no cofre.

Testes de contrato dizem "o caminho feliz funciona". Estes perguntam outra
coisa: **o que acontece quando duas coisas acontecem ao mesmo tempo?** Num
cofre, uma corrida não dá erro visível — dá um saldo errado, e descobre-se
no fecho do mês.

O que se procura aqui
---------------------
* 20 threads a registar a mesma referência ao mesmo tempo → uma só entrada;
* saques concorrentes não podem exceder o bolso do Presidente;
* um valor inválido no meio de vinte válidos não pode estragar os outros;
* o cofre sobrevive a um ficheiro truncado (JSON a meio de uma escrita).

Nada aqui depende de rede ou de tempo: corre em segundos, sempre igual.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.core.errors import ValidationBlocked
from backend.finance.cfo_treasury import cfo


# ==========================================================================
# 1. Idempotência sob concorrência
# ==========================================================================
def test_vinte_threads_com_a_mesma_referencia_registam_uma_vez():
    """A mesma cobrança chegando 20 vezes ao mesmo tempo.

    É o cenário real de um retry em rajada: a rede falha, o cliente repete,
    e as repetições chegam juntas. Sem bloqueio nem dedupe, o cofre
    acreditava ter recebido 20 vezes.
    """
    antes = cfo.snapshot()["gross_revenue_usd"]
    referencia = "CORRIDA-PAYPAL-001"
    erros: list = []

    def registar() -> None:
        try:
            cfo.record_revenue(
                10.0, source="paypal", external_ref=referencia, description="corrida"
            )
        except Exception as exc:  # noqa: BLE001
            erros.append(exc)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lambda _: registar(), range(20)))

    assert not erros, f"threads levantaram: {erros[:3]}"
    depois = cfo.snapshot()["gross_revenue_usd"]
    assert depois - antes == pytest.approx(10.0), (
        f"esperava +10.00, obteve +{depois - antes:.2f}"
    )


def test_referencias_diferentes_contam_todas():
    """O outro lado: dedupe não pode engolir receita legítima."""
    antes = cfo.snapshot()["gross_revenue_usd"]
    for i in range(5):
        cfo.record_revenue(
            2.0, source="paypal", external_ref=f"LEGIT-{i}", description=f"item {i}"
        )
    depois = cfo.snapshot()["gross_revenue_usd"]
    assert depois - antes == pytest.approx(10.0)


def test_a_mesma_referencia_em_origens_diferentes_sao_dois_pagamentos():
    """O id `123` de um saque M-Pesa não é o `123` de uma cobrança PayPal.

    Sem separar por origem, a deduplicação bloqueia receita legítima: dois
    pagamentos distintos com o mesmo número de referência, e um deles
    desaparece do cofre sem aviso.
    """
    antes = cfo.snapshot()["gross_revenue_usd"]
    cfo.record_revenue(7.0, source="paypal", external_ref="REF-IGUAL")
    cfo.record_revenue(3.0, source="mpesa", external_ref="REF-IGUAL")
    depois = cfo.snapshot()["gross_revenue_usd"]
    assert depois - antes == pytest.approx(10.0), (
        f"esperava 10.00 (duas origens), obteve {depois - antes:.2f}"
    )


# ==========================================================================
# 2. Receita inválida no meio de válidas
# ==========================================================================
def test_uma_receita_invalida_nao_estraga_as_outras():
    """Cada registo é atómico: um falha, os outros entram."""
    antes = cfo.snapshot()["gross_revenue_usd"]
    validas = 0
    for valor in (1.0, -5.0, 2.0, 0, 3.0):
        try:
            cfo.record_revenue(valor, source="manual", description="lote")
            validas += 1
        except ValidationBlocked:
            continue
    depois = cfo.snapshot()["gross_revenue_usd"]
    assert validas == 3
    assert depois - antes == pytest.approx(6.0), "os inválidos não podem deixar rasto"


# ==========================================================================
# 3. Corrida nos saques
# ==========================================================================
def test_saques_concorrentes_nao_excedem_o_bolso():
    """Duas aprovações do mesmo valor não podem debitar duas vezes.

    Não é um cenário artificial: o utilizador carrega duas vezes no botão,
    a app reenvia o pedido, e ambos chegam ao backend.
    """
    cfo.record_revenue(1000.0, source="manual", description="capital")
    disponivel = cfo.snapshot()["distributable_owner_usd"]
    pedido = round(disponivel / 2, 2)

    resultados: list = []

    def reservar() -> None:
        try:
            resultado = cfo.reserve_for_payout(
                pedido, channel="mpesa", destination="84xxxxxxx"
            )
            resultados.append(("ok", resultado.get("payout")))
        except Exception as exc:  # noqa: BLE001
            resultados.append(("erro", type(exc).__name__))

    threads = [threading.Thread(target=reservar) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sucessos = [r for r in resultados if r[0] == "ok"]
    # Metade do bolso, 4 vezes ao mesmo tempo: no máximo 2 podem passar.
    assert len(sucessos) <= 2, f"4 saques de metade do bolso passaram: {len(sucessos)}"
    # E o saldo nunca pode ficar negativo.
    assert cfo.snapshot()["distributable_owner_usd"] >= 0


# ==========================================================================
# 4. Cofre corrompido
# ==========================================================================
def test_cofre_com_json_truncado_nao_rebenta_o_sistema():
    """Um ficheiro a meio de uma escrita (crash, disco cheio) não pode
    deixar a holding sem arrancar. O cofre recupera ou diz claramente."""
    from backend.config.settings import settings
    from backend.core.vault_store import vault as cofre

    ficheiro = Path(settings.vault_path) / "ledger.json"
    original = ficheiro.read_text(encoding="utf-8") if ficheiro.exists() else None
    try:
        ficheiro.parent.mkdir(parents=True, exist_ok=True)
        ficheiro.write_text('{"revenue_events": [{"id": "trunc', encoding="utf-8")
        # O importante: a leitura devolve algo utilizável, não uma excepção
        # que impede o arranque.
        dados = cofre.read("ledger")
        assert isinstance(dados, dict)
    finally:
        if original is not None:
            ficheiro.write_text(original, encoding="utf-8")


# ==========================================================================
# 5. Câmbio e arredondamento
# ==========================================================================
def test_centavos_nao_se_perdem_em_cem_registos():
    """100 × $0.01 tem de dar exactamente $1.00.

    Arredondamentos sucessivos em float são o clássico "faltam 3 cêntimos"
    no fim do mês.
    """
    antes = cfo.snapshot()["gross_revenue_usd"]
    for i in range(100):
        cfo.record_revenue(0.01, source="manual", external_ref=f"CENT-{i}")
    depois = cfo.snapshot()["gross_revenue_usd"]
    assert round(depois - antes, 2) == pytest.approx(1.00, abs=0.005), (
        f"100 centavos deram {depois - antes:.6f}"
    )


@pytest.mark.parametrize("moeda", ["MZN", "ZAR", "GBP", "xyz", ""])
def test_moeda_fora_da_lista_e_recusada(moeda):
    """Aceitar MZN aqui daria um erro na PayPal, já depois de prometer
    ao cliente que o pagamento estava criado."""
    with pytest.raises(ValidationBlocked):
        cfo.record_revenue(10.0, source="manual", currency=moeda)


def test_o_snapshot_e_json_serializavel():
    """O estado tem de poder ir para a app sem passar por `default=str`.

    Se algo aqui falhar, o APK recebe um erro em vez do cofre — e o erro
    aparece no ecrã do Presidente, não no log de quem programou.
    """
    estado = cfo.snapshot()
    texto = json.dumps(estado, allow_nan=False)
    assert isinstance(texto, str) and len(texto) > 10
    reconstruido = json.loads(texto)
    assert set(reconstruido) == set(estado)
