"""Factos imutáveis sobre Moçambique, para ancorar modelos pequenos.

Porquê isto existe
------------------
Medido neste sandbox, com o Qwen3-0.6B Q4_K_M (397 MB) e nenhum contexto:

    P: Qual é a moeda oficial de Moçambique?
    R: «A moeda oficial de Moçambique é o **moeda nacional**.»

    P: Que pagamentos móveis se usam em Maputo?
    R: «Os pagamentos móveis mais comuns em Maputo são os de tipo
        **Paytm**, **Mobis** e **Mobis Pay**…»

Paytm é indiano. «Mobis» não existe. Num guião de vendas isto é pior do que um
erro de estilo: é informação falsa dita com confiança a um cliente.

Um modelo de 0,6B tem 397 MB de parâmetros para representar o mundo inteiro —
não há espaço para enciclopédias de países. A solução não é pedir-lhe para
«ter cuidado»: é dar-lhe os factos no contexto onde ele os vai ler. Chamam-lhe
ancoragem (grounding) e funciona porque o modelo deixa de precisar de os
saber de cor — passa a copiá-los.

O bloco é injectado no `system` de **todas** as chamadas ao LLM, em
`LLMRouter.complete()`, para não depender de cada esquadrão se lembrar.

Limite honesto
--------------
A ancoragem garante que o modelo *tem* os factos certos à frente. Não garante
que os use sempre — um 0,6B ainda pode ignorar o contexto. O que se mediu,
com os testes em `backend/tests/test_grounding.py`, é que nos casos testados
passa a acertar.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

#: O bloco injectado no system prompt. Curto de propósito: 4 linhas cabem na
#: janela de 2048 tokens sem roubar espaço à tarefa, e um modelo pequeno
#: perde precisão com contextos longos.
FACTOS_MOCAMBIQUE = (
    "FACTOS OBRIGATÓRIOS (NUNCA INVENTES OUTROS):\n"
    "- País: Moçambique (nunca escrevas \"Mozambique\" em inglês).\n"
    "- Moeda oficial: Metical (código: MZN, símbolo: MT). "
    "NUNCA uses RMB, BRL, EUR ou USD como moeda local.\n"
    "- Pagamentos móveis locais: M-Pesa (Vodacom) e e-Mola (Movitel). "
    "NUNCA cites Paytm nem Mobis: não existem em Moçambique.\n"
    "- Plataformas: apenas nomes que operem em Moçambique. "
    "NUNCA cites \"Mercado Livre\" (não opera aqui).\n"
    "- Vocabulário proibido: nunca uses palavras inventadas como \"mapa-voz\" "
    "nem cites nomes internos de variáveis/slugs na narração. "
    "Usa frases curtas, diretas e naturais em Português."
)

#: Marcador para não injectar duas vezes (o router e o esquadrão podem os dois
#: tentar; duplicar o bloco só gasta contexto).
MARCA = "FACTOS OBRIGATÓRIOS (NUNCA INVENTES OUTROS)"

#: Termos que, se aparecerem numa saída para o cliente, indicam alucinação.
#: Não é uma lista exaustiva de erros possíveis — é a dos que foram medidos.
TERMOS_INVENTADOS: Tuple[Tuple[str, str], ...] = (
    ("paytm", "M-Pesa ou e-Mola"),
    ("mobis pay", "M-Pesa ou e-Mola"),
    ("mobis", "M-Pesa ou e-Mola"),
    ("mpesa pay", "M-Pesa"),
    ("mapa-voz", "uma frase normal"),
    ("mapa voz", "uma frase normal"),
    ("mercado livre", "uma plataforma que opere em Moçambique"),
)

#: Moedas que nunca são a local de Moçambique.
MOEDAS_ERRADAS = ("RMB", "BRL", "EUR", "USD", "real brasileiro", "yuan")


def ancorar(system: str) -> str:
    """Devolve o system com o bloco de factos, sem duplicar."""
    base = system or ""
    if MARCA in base:
        return base
    if base.strip():
        return f"{FACTOS_MOCAMBIQUE}\n\n{base}"
    return FACTOS_MOCAMBIQUE


def problemas(texto: str) -> List[str]:
    """Lista o que está errado num texto destinado ao cliente.

    Usa-se como portão antes de publicar: um guião com «Paytm» não sai, mesmo
    que o juiz lhe dê boa nota (o juiz também é um modelo e também se engana).
    """
    if not texto:
        return []
    minusculo = texto.lower()
    achados: List[str] = []
    for termo, substituto in TERMOS_INVENTADOS:
        # Fronteira de palavra: «mobis» não deve disparar em «mobilidade».
        if re.search(rf"\b{re.escape(termo)}\b", minusculo):
            achados.append(f"termo inventado «{termo}» — usar {substituto}")
    if "mozambique" in minusculo:
        achados.append("«Mozambique» em inglês — escrever «Moçambique»")
    for moeda in MOEDAS_ERRADAS:
        if re.search(rf"\b{re.escape(moeda.lower())}\b", minusculo):
            achados.append(
                f"moeda «{moeda}» referida como local — a moeda é o Metical (MZN)"
            )
    return achados


def limpar(texto: str) -> Tuple[str, List[str]]:
    """Corrige o que é corrigível por substituição directa.

    Devolve ``(texto_corrigido, lista_de_correcções)``. Só substitui termos
    cujo substituto é inequívoco («Mozambique» → «Moçambique»); o resto fica
    para o portão de publicação decidir.
    """
    if not texto:
        return texto, []
    corrigido = texto
    feitas: List[str] = []
    if re.search(r"\bMozambique\b", corrigido):
        corrigido = re.sub(r"\bMozambique\b", "Moçambique", corrigido)
        feitas.append("Mozambique → Moçambique")
    trocas = {
        r"\bPaytm\b": "M-Pesa",
        r"\bMobis Pay\b": "e-Mola",
        r"\bMobis\b": "e-Mola",
        r"\bmapa-voz\b": "Maputo",
    }
    for padrao, novo in trocas.items():
        if re.search(padrao, corrigido, flags=re.I):
            corrigido = re.sub(padrao, novo, corrigido, flags=re.I)
            feitas.append(f"{padrao.strip(chr(92)+'b')} → {novo}")
    return corrigido, feitas


def verificar_campo(rotulo: str, texto: str) -> List[str]:
    """Problemas num campo, com o rótulo à frente (para relatórios)."""
    return [f"{rotulo}: {p}" for p in problemas(texto)]


def verificar_campos(campos: Iterable[Tuple[str, str]]) -> List[str]:
    achados: List[str] = []
    for rotulo, texto in campos:
        achados.extend(verificar_campo(rotulo, texto))
    return achados
