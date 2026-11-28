"""Tesouraria e pagamentos da holding."""

import sys as _sys

from .cfo_treasury import CFOTreasury, cfo, PHASES, PHASE_2_THRESHOLD_USD, PHASE_3_THRESHOLD_USD
from .payout_bridge import PayoutBridge, payout_bridge, PayoutRequest, PayoutResult

# ``from .payout_bridge import payout_bridge`` substitui, no pacote, o nome do
# submódulo pela instância. Isso faz ``import backend.finance.payout_bridge``
# devolver a instância (footgun que já apanhámos em testes). Aqui expomos o
# módulo verdadeiro num alias explícito, sem quebrar a API pública.
payout_bridge_module = _sys.modules[__name__ + ".payout_bridge"]

__all__ = [
    "CFOTreasury",
    "cfo",
    "PHASES",
    "PHASE_2_THRESHOLD_USD",
    "PHASE_3_THRESHOLD_USD",
    "PayoutBridge",
    "payout_bridge_module",
    "payout_bridge",
    "PayoutRequest",
    "PayoutResult",
]
