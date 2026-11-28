#!/usr/bin/env python3
"""Toca num botao pelo TEXTO, dentro da area dele, e nao no centro do texto.

Porque existe: no emulador, o toque no centro do texto «PARAR TUDO» de um
AlertDialog do Material3 nao accionou o botao — o backend nunca recebeu o
POST /kill-switch/engage. O centro do texto pode cair num ponto que a
composicao nao trata como clique (padding, area morta entre a Row e o
Button). O que funciona e tocar num ponto claramente DENTRO da area pintada
e afastado do vizinho («Cancelar» esta a esquerda).

Uso:
    python3 scripts/tocar_botao.py <ui.xml> <texto> [--fracao 0.75]

Devolve "x y" no stdout, ou nada (codigo 0) se o texto nao existir.
"""

from __future__ import annotations

import re
import sys
from xml.etree import ElementTree as ET


def caixa(no: ET.Element) -> tuple[int, int, int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", no.get("bounds", ""))
    return tuple(map(int, m.groups())) if m else None  # type: ignore[return-value]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    caminho, alvo = sys.argv[1], sys.argv[2]
    fracao = 0.75
    if "--fracao" in sys.argv:
        fracao = float(sys.argv[sys.argv.index("--fracao") + 1])

    try:
        raiz = ET.parse(caminho).getroot()
    except Exception:
        return 0

    # O no com o texto EXACTO mais pequeno (o texto e nao o cartao inteiro).
    candidatos = [
        b
        for n in raiz.iter()
        if (n.get("text") or "").strip() == alvo
        for b in [caixa(n)]
        if b
    ]
    if not candidatos:
        return 0
    x1, y1, x2, y2 = min(
        candidatos, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])
    )
    # Ponto dentro da area, a `fracao` da largura e a meia altura.
    x = int(x1 + (x2 - x1) * fracao)
    y = (y1 + y2) // 2
    print(x, y)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
