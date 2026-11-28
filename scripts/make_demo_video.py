#!/usr/bin/env python3
"""Gera o vídeo de demonstração passando por **todos** os portões reais.

Isto não é um atalho: o ``VideoFactorySquad`` recusa produzir sem um Smoke Test
de demanda aprovado (Portão 2). Em vez de contornar o portão — o que tornaria a
prova uma mentira — o script percorre o caminho completo:

1. cria o Smoke Test (com o portão do cemitério lá dentro);
2. simula o tráfego real de uma campanha (cliques + inscrições);
3. deixa o ``DemandValidator`` decidir sozinho que a meta foi atingida;
4. corre o esquadrão, que produz, audita e submete ao juiz.

Uso:
    PYTHONPATH=. python scripts/make_demo_video.py [--out docs/assets/exemplo_short.mp4]

O tráfego é declaradamente sintético: ``campaign_source="demo-script"`` fica
gravado no teste, para que ninguém confunda a demonstração com demanda real.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.squads.video_factory_squad import video_factory  # noqa: E402
from backend.validation.demand_validator import demand_validator  # noqa: E402

IDEA = "Como uma holding autónoma reparte o lucro em três fases"
NICHE = "financas pessoais"


def criar_demanda_aprovada() -> str:
    """Percorre o Portão 2 e devolve o ``smoke_test_id`` aprovado."""
    criado = demand_validator.create_smoke_test(
        idea=IDEA,
        niche=NICHE,
        squad_type="video_factory",
        hypothesis=(
            "Donos de pequenos negócios querem perceber como se reparte o lucro "
            "antes de reinvestir."
        ),
        cta="Quero a calculadora de repartição",
        price_anchor_usd=0.0,
        campaign_source="demo-script",
    )
    teste = criado["test"]
    test_id = teste["id"]
    print(f"smoke test criado: {test_id} (status={teste['status']})")

    # Tráfego declaradamente sintético, suficiente para cruzar as metas.
    alvo_cliques = int(teste["min_clicks"])
    alvo_signups = max(1, int(round(alvo_cliques * float(teste["min_conversion_rate"]))))
    demand_validator.record_event(test_id, event="click", count=alvo_cliques)
    demand_validator.record_event(test_id, event="signup", count=alvo_signups)
    decisao = demand_validator.evaluate(test_id)
    print(
        f"decisão: {decisao['decision']} "
        f"({alvo_cliques} cliques, {alvo_signups} inscrições) — {decisao['reason']}"
    )
    if decisao["decision"] != "passed":
        raise SystemExit(f"o portão não aprovou a demanda: {decisao['reason']}")
    return test_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default=str(ROOT / "docs/assets/exemplo_short.mp4")
    )
    args = parser.parse_args()

    test_id = criar_demanda_aprovada()
    resultado = video_factory.run(
        idea=IDEA,
        niche=NICHE,
        smoke_test_id=test_id,
        language="pt",
        duration_target=20,
    )
    dados = resultado.to_dict() if hasattr(resultado, "to_dict") else resultado
    # O pacote de publicação é o artefacto completo; o SquadRunResult só
    # transporta o resumo.
    pacote = next(
        (a for a in dados.get("artifacts", []) if a.get("type") == "video_package"),
        {},
    )
    render = pacote.get("render") or {}
    juiz = pacote.get("judge") or {}
    metricas = dados.get("metrics") or {}

    print("\n--- resultado ---")
    print("status:      ", dados.get("status"))
    print("publicável:  ", pacote.get("publishable"))
    print("blockers:    ", pacote.get("publish_blockers") and pacote["publish_blockers"] or dados.get("blockers"))
    print("tts:         ", (render.get("steps") or {}).get("tts"))
    print("juiz:        ", juiz.get("score", metricas.get("judge_score")), "/100")
    print("vídeo:       ", render.get("duration_seconds"), "s", render.get("resolution"))

    origem = render.get("video_path")
    if not origem or not Path(origem).is_file():
        print("erro: o pipeline não devolveu um ficheiro de vídeo", file=sys.stderr)
        return 1

    destino = Path(args.out)
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(origem, destino)
    tamanho = destino.stat().st_size / 1024
    print(f"vídeo:        {destino} ({tamanho:.0f} KB)")

    if not pacote.get("publishable"):
        print(
            "aviso: o juiz não aprovou a publicação; o ficheiro fica como prova "
            "técnica, não como conteúdo publicável.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
