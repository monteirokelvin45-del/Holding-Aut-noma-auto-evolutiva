"""Esquadrões operacionais multi-renda da holding."""

from .squad_base import BaseSquad, SquadRunResult
from .scout_hunter import ScoutHunter, ScoutOpportunity, scout_hunter
from .video_factory_squad import VideoFactorySquad, video_factory
from .infoproducts_affiliate_squad import InfoproductsSquad, infoproducts_squad
from .micro_saas_dev_squad import MicroSaasSquad, micro_saas_squad

__all__ = [
    "BaseSquad",
    "SquadRunResult",
    "ScoutHunter",
    "ScoutOpportunity",
    "scout_hunter",
    "VideoFactorySquad",
    "video_factory",
    "InfoproductsSquad",
    "infoproducts_squad",
    "MicroSaasSquad",
    "micro_saas_squad",
]
