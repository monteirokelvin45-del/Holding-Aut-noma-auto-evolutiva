"""Suíte de testes do backend da Automaton Holding Corp.

Corre com:  pytest backend/tests -q

Todos os testes usam um cofre temporário (``VAULT_DIR`` apontado para
``tmp_path``), para nunca contaminarem o estado real da holding. O cofre real
é restaurado pela fixture automática ``_vault_isolation``.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# O isolamento tem de acontecer *antes* de qualquer módulo ``backend.*`` ser
# importado (os módulos de teste importam ``backend.squads...`` durante a
# coleção, que antecede qualquer fixture). Por isso é feito aqui, ao importar
# o conftest.
_TEST_VAULT = Path(tempfile.mkdtemp(prefix="automaton-test-vault-"))
os.environ["VAULT_DIR"] = str(_TEST_VAULT)
os.environ.setdefault("API_ACCESS_TOKEN", "test-token")
os.environ.setdefault("APP_ENV", "test")
# O heartbeat do /ws/live é de 25 s; em teste isso significa esperar meio
# minuto por ligação fechada. 0,2 s mantém o comportamento, não a espera.
os.environ.setdefault("WS_HEARTBEAT_SECONDS", "0.2")
os.environ.setdefault("GENAI_VIDEO_ENABLED", "false")
os.environ.setdefault("RESEARCH_ENABLED", "true")

for _name in [m for m in list(sys.modules) if m.startswith("backend.")]:
    del sys.modules[_name]
importlib.invalidate_caches()


@pytest.fixture(scope="session", autouse=True)
def vault_isolation() -> Iterator[Path]:
    """Confirma o cofre temporário usado por toda a sessão de testes."""
    # Reafirma no caso de outro conftest/plugin ter mexido no ambiente.
    os.environ["VAULT_DIR"] = str(_TEST_VAULT)
    yield _TEST_VAULT


@pytest.fixture(autouse=True)
def _kill_switch_liberado(vault_isolation: Path) -> Iterator[None]:
    """Cada teste começa com o sistema operacional.

    O kill switch é estado global persistido. Um teste que o accione e falhe a
    meio deixa o interruptor ligado e os testes seguintes passam a receber 423
    em vez do que esperam — falhas em cascata que parecem bugs noutros sítios.
    Já aconteceu: um teste do ciclo abortado derrubou quatro testes
    aparentemente não relacionados.
    """
    from backend.core.security_guard import kill_switch

    kill_switch.release(actor="pytest-setup")
    yield
    kill_switch.release(actor="pytest-teardown")


@pytest.fixture(scope="session")
def client(vault_isolation: Path):
    """Cliente FastAPI com token de teste."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from backend.main import app

    headers = {"X-Automaton-Token": os.environ["API_ACCESS_TOKEN"]}
    # Sem gestor de contexto: não corre o ``lifespan`` (que ligaria o polling do
    # Telegram e o event loop partilhado). Os handlers são síncronos e testam-se
    # exactamente igual, sem dependências de rede.
    yield TestClient(app, headers=headers)


@pytest.fixture(scope="session")
def auth_headers() -> dict[str, str]:
    return {"X-Automaton-Token": os.environ["API_ACCESS_TOKEN"]}


# --------------------------------------------------------------------------- #
# Rede desligada nos testes
# --------------------------------------------------------------------------- #
# Porquê: o GITHUB_TOKEN existe em qualquer execução do Actions (é o que dá
# LLM real ao cron). Mas nos testes, isso fazia cada chamada de agente tentar
# o GitHub Models a sério e ficar ~30 s à espera do timeout. A suíte passou de
# 2 s para 224 s sem uma única asserção a mais — e o CI ficava refém da rede.
#
# A regra: um teste unitário não fala com a internet. Quem quer testar um
# provider de verdade marca o teste explicitamente.
@pytest.fixture(autouse=True)
def _sem_rede_para_llm(monkeypatch):
    import os

    from backend.core import llm_router as mod

    for var in (
        "GITHUB_MODELS_TOKEN", "GITHUB_TOKEN", "GROQ_API_KEY",
        "GEMINI_API_KEY", "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    # O singleton já leu o ambiente no import; limpa-se o que ele guardou.
    # `settings` é uma dataclass frozen, por isso monkeypatch.setattr não serve
    # (FrozenInstanceError) — object.__setattr__ contorna, e o valor anterior é
    # reposto no teardown.
    campos = ("github_models_token", "groq_api_key", "gemini_api_key",
              "openrouter_api_key")
    anteriores = {c: getattr(mod.settings, c, "") for c in campos}
    for campo in campos:
        object.__setattr__(mod.settings, campo, "")
    try:
        yield
    finally:
        for campo, valor in anteriores.items():
            object.__setattr__(mod.settings, campo, valor)
