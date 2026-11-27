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


@pytest.fixture(scope="session", autouse=True)
def vault_isolation() -> Iterator[Path]:
    """Isola o cofre num directório temporário durante toda a sessão."""
    tmp = Path(tempfile.mkdtemp(prefix="automaton-test-vault-"))
    os.environ["VAULT_DIR"] = str(tmp)
    os.environ.setdefault("API_ACCESS_TOKEN", "test-token")
    os.environ.setdefault("APP_ENV", "test")
    # Recarrega a configuração para apanhar o VAULT_DIR novo.
    for module in [m for m in list(sys.modules) if m.startswith("backend.")]:
        if module != "backend":
            sys.modules.pop(module, ignore_errors=True)
    importlib.invalidate_caches()
    yield tmp


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
