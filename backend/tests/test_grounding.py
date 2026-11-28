"""Ancoragem de factos: o antídoto para o Qwen3-0.6B inventar Moçambique.

Antes da ancoragem, medido neste sandbox:

    P: Qual é a moeda oficial de Moçambique?
    R: «A moeda oficial de Moçambique é o **moeda nacional**.»

    P: Que pagamentos móveis se usam em Maputo?
    R: «...**Paytm**, **Mobis** e **Mobis Pay**...»

Paytm é indiano, «Mobis» não existe. Depois de injectar o bloco de factos no
system prompt, as mesmas perguntas passaram a responder «Metical (MZN)» e
«M-Pesa e e-Mola».
"""

from __future__ import annotations

import pytest

from backend.core import grounding


def test_o_bloco_entra_no_system_e_nao_duplica():
    base = "És um agente operacional."
    uma = grounding.ancorar(base)
    assert grounding.MARCA in uma
    assert base in uma
    # Chamar outra vez não acrescenta um segundo bloco (gastaria contexto).
    assert grounding.ancorar(uma).count(grounding.MARCA) == 1


def test_ancorar_sem_system_tambem_funciona():
    """Um pedido sem system também tem de levar factos."""
    assert grounding.MARCA in grounding.ancorar("")


def test_ancorar_respeita_a_ordem_factos_primeiro():
    """Os factos vão à frente do resto: um 0,6B dá mais peso ao início."""
    resultado = grounding.ancorar("INSTRUÇÃO ESPECÍFICA")
    assert resultado.index(grounding.MARCA) < resultado.index("INSTRUÇÃO ESPECÍFICA")


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("Paga com Paytm", "paytm"),
        ("Usa o Mobis Pay", "mobis pay"),
        ("A moeda é o RMB", "RMB"),
        ("Guia para Mozambique", "Mozambique"),
        ("Diz mapa-voz na narração", "mapa-voz"),
    ],
)
def test_detecta_o_que_foi_medido_como_alucinacao(texto, esperado):
    problemas = grounding.problemas(texto)
    assert any(esperado.lower() in p.lower() for p in problemas), problemas


def test_texto_correcto_nao_levanta_problemas():
    bom = (
        "Em Moçambique a moeda é o Metical (MZN). Os pagamentos móveis são "
        "M-Pesa e e-Mola. Em Maputo, muitos freelancers cobram em dólares "
        "para proteger o rendimento da inflação."
    )
    # Nota: «dólares» é legítimo aqui — é a moeda de cobrança para o
    # estrangeiro, não a moeda local. Só bloqueia USD como moeda local.
    assert grounding.problemas(bom) == []


def test_limpar_corrige_o_que_e_inequivoco():
    corrigido, feitas = grounding.limpar(
        "Paga com Paytm em Mozambique, no mapa-voz."
    )
    assert "Paytm" not in corrigido
    assert "Mozambique" not in corrigido
    assert "M-Pesa" in corrigido
    assert "Moçambique" in corrigido
    assert len(feitas) >= 2


def test_limpar_nao_toca_em_palavras_parecidas():
    """«mobilidade» não é «Mobis»; a fronteira de palavra tem de ser respeitada."""
    texto = "A mobilidade urbana em Maputo é um desafio."
    assert grounding.problemas(texto) == []
    corrigido, feitas = grounding.limpar(texto)
    assert corrigido == texto
    assert feitas == []


def test_o_router_ancora_todas_as_chamadas(monkeypatch):
    """A ancoragem é feita no router, não confiada a cada esquadrão.

    Se ficasse a cargo dos esquadrões, bastava um esquecer-se para os factos
    desaparecerem — e o modelo voltava a inventar «Moobis Pay».
    """
    from backend.config.settings import settings as cfg
    from backend.core import llm_router as mod

    vistas = []

    def falso_post(url, payload, headers, timeout=30):
        vistas.append(payload)
        return {
            "choices": [{"message": {"content": "Metical (MZN)"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

    # Guardar e repor os valores ORIGINAIS: fixar `["local"]` no fim
    # contamina os testes de providers que correm a seguir (foi o que
    # aconteceu com test_github_models_*).
    antes_chave = cfg.groq_api_key
    antes_ordem = list(cfg.llm_provider_order)
    object.__setattr__(cfg, "groq_api_key", "gsk-teste")
    monkeypatch.setattr(mod, "_http_post_json", falso_post)
    object.__setattr__(cfg, "llm_provider_order", ["groq"])
    try:
        router = mod.LLMRouter()
        router.complete(mod.LLMRequest(prompt="Qual é a moeda?", system="Sê breve."))
    finally:
        object.__setattr__(cfg, "groq_api_key", antes_chave)
        object.__setattr__(cfg, "llm_provider_order", antes_ordem)

    sistema = vistas[0]["messages"][0]["content"]
    assert grounding.MARCA in sistema
    assert "Sê breve." in sistema


def test_guiao_do_video_e_limpo_antes_de_ser_narrado():
    """O portão dos esquadrões corrige e reporta — não confia no juiz."""
    from backend.squads.video_factory_squad import VideoFactorySquad

    (titulo, hook, body, cta), problemas = VideoFactorySquad._verificar_factos(
        "Guia para Mozambique",
        "Paga com Paytm?",
        ["Usa Mobis Pay no mapa-voz"],
        "Quero o guia",
    )
    assert titulo == "Guia para Moçambique"
    assert "Paytm" not in hook and "M-Pesa" in hook
    assert "Mobis" not in body[0]
    assert problemas == []
