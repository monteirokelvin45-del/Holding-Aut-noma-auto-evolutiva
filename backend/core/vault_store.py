"""Cofre de estado da holding: leitura/escrita transacional de JSON.

Porque não basta ``json.dump``:
  * Vários processos (API FastAPI, ciclo do CEO, cron do GitHub Actions) podem
    tocar no mesmo ficheiro. Usamos ``fcntl.flock`` sobre um ``.lock`` dedicado.
  * Uma escrita interrompida corromperia a contabilidade. Escrevemos para um
    temporário, fazemos ``fsync`` e só depois ``os.replace`` (atómico em POSIX).
  * Ficheiros ausentes/corrompidos degradam para o esquema por omissão e
    preservam o ficheiro corrompido como ``<nome>.corrupt-<ts>`` para auditoria.

Os quatro ficheiros do cofre são: ``ledger.json``, ``post_mortem_memory.json``,
``squad_metrics.json`` e ``pending_approvals.json``.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from ..config.settings import settings
from .clock import utcnow_iso
from .logger import get_logger

log = get_logger("core.vault")

LEDGER = "ledger.json"
POST_MORTEM = "post_mortem_memory.json"
SQUAD_METRICS = "squad_metrics.json"
PENDING_APPROVALS = "pending_approvals.json"

VAULT_FILES = (LEDGER, POST_MORTEM, SQUAD_METRICS, PENDING_APPROVALS)


# --------------------------------------------------------------------------
# Esquemas por omissão
# --------------------------------------------------------------------------
def default_ledger() -> Dict[str, Any]:
    now = utcnow_iso()
    return {
        "schema_version": 1,
        "created_at": now,
        "updated_at": now,
        "phase": 1,
        "current_split": {
            "owner_pocket": 0.10,
            "savings_vault": 0.20,
            "reinvestment_pool": 0.70,
            "tax_reserve_rate": settings.tax_reserve_rate,
        },
        "gross_revenue_usd": 0.0,
        "net_revenue_usd": 0.0,
        "tax_reserve_usd": 0.0,
        "owner_pocket_usd": 0.0,
        "savings_vault_usd": 0.0,
        "reinvestment_pool_usd": 0.0,
        "spent_from_reinvestment_usd": 0.0,
        "paid_out_usd": 0.0,
        "daily_spend_usd": {},
        "revenue_events": [],
        "ledger_entries": [],
        "payouts": [],
        "phase_history": [
            {"phase": 1, "entered_at": now, "monthly_revenue_usd": 0.0}
        ],
        "fx": {
            "usd_mzn": settings.usd_mzn_rate,
            "source": "static_config",
            "updated_at": now,
        },
    }


def default_post_mortem() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": utcnow_iso(),
        "embedding_model": "hash-tf-v1",
        "embedding_dim": 128,
        "entries": [],
    }


def default_squad_metrics() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": utcnow_iso(),
        "kill_policy": {
            "min_traction_days": 7,
            "min_views_in_traction_window": 200,
            "first_dollar_deadline_days": 21,
            "min_conversion_rate": 0.005,
        },
        "squads": {},
    }


def default_pending_approvals() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": utcnow_iso(),
        "sequence": 0,
        "items": [],
    }


DEFAULTS: Dict[str, Callable[[], Dict[str, Any]]] = {
    LEDGER: default_ledger,
    POST_MORTEM: default_post_mortem,
    SQUAD_METRICS: default_squad_metrics,
    PENDING_APPROVALS: default_pending_approvals,
}


class VaultStore:
    """Acesso concorrente-seguro aos ficheiros JSON do cofre."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else Path(settings.vault_path)
        self._thread_locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - ambiente read-only
            log.error("Não foi possível criar o directório do cofre %s: %s", self.root, exc)

    # ------------------------------------------------------------------ paths
    def path_for(self, name: str) -> Path:
        if "/" in name or name.startswith("."):
            raise ValueError(f"Nome de ficheiro de cofre inválido: {name!r}")
        return self.root / name

    def exists(self, name: str) -> bool:
        return self.path_for(name).is_file()

    def _thread_lock(self, name: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._thread_locks.get(name)
            if lock is None:
                lock = threading.RLock()
                self._thread_locks[name] = lock
            return lock

    def _default_for(self, name: str) -> Dict[str, Any]:
        factory = DEFAULTS.get(name)
        if factory is None:
            return {}
        return factory()

    # ------------------------------------------------------------------- leitura
    def read(self, name: str) -> Dict[str, Any]:
        """Lê um ficheiro do cofre, reparando se necessário.

        Nunca lança por ficheiro ausente/corrompido: devolve o esquema por
        omissão e registra o incidente.

        A reparação (criar/quarentena) é feita **sem** ``flock`` aninhado: se
        outro processo estiver a escrever, a leitura devolve o esquema por
        omissão e a próxima escrita consolida-o. Isto evita o deadlock de
        ``flock`` reentrante em file descriptors distintos.
        """
        path = self.path_for(name)
        if not path.is_file():
            data = self._default_for(name)
            self._try_repair(name, data)
            return data
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.error("Cofre corrompido em %s: %s — a restaurar esquema.", path, exc)
            self._quarantine(path)
            data = self._default_for(name)
            self._try_repair(name, data)
            return data
        if not isinstance(raw, dict):
            log.error("Cofre %s não contém um objecto JSON — a restaurar.", path)
            self._quarantine(path)
            data = self._default_for(name)
            self._try_repair(name, data)
            return data
        return self._repair(name, raw)

    def _try_repair(self, name: str, data: Dict[str, Any]) -> None:
        """Grava o esquema por omissão; falhas (ex.: lock ocupado) não são fatais."""
        try:
            self.write(name, data)
        except OSError as exc:  # pragma: no cover - ambiente read-only/corrida
            log.warning("Não foi possível reparar %s agora: %s", name, exc)

    def _read_unlocked(self, name: str) -> Dict[str, Any]:
        """Leitura pura, sem reparação nem obtenção de ``flock``.

        Só deve ser usada por quem já detém o lock do ficheiro (``transaction``).
        """
        path = self.path_for(name)
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.error("Cofre ilegível em %s (%s) — a usar esquema por omissão.", path, exc)
            return self._default_for(name)
        if not isinstance(raw, dict):
            return self._default_for(name)
        return self._repair(name, raw)

    def _repair(self, name: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Preenche chaves em falta sem destruir dados válidos do operador."""
        default = self._default_for(name)
        if not default:
            return raw
        merged = copy.deepcopy(raw)
        for key, value in default.items():
            if key not in merged:
                merged[key] = value
        return merged

    # ------------------------------------------------------------------- escrita
    def write(self, name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Escrita atómica + lock exclusivo. Devolve o que ficou gravado."""
        path = self.path_for(name)
        if not isinstance(data, dict):
            raise ValueError(f"O cofre {name} só aceita objectos JSON.")
        payload = copy.deepcopy(data)
        payload.setdefault("schema_version", 1)
        payload["updated_at"] = utcnow_iso()

        with self._thread_lock(name):
            lock_path = path.with_suffix(path.suffix + ".lock")
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with open(lock_path, "a+", encoding="utf-8") as lock_handle:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                    try:
                        self._atomic_write(path, payload)
                    finally:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                log.error("Falha de I/O ao gravar %s: %s", path, exc)
                raise
        return payload

    def _atomic_write(self, path: Path, payload: Dict[str, Any]) -> None:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            try:
                dir_fd = os.open(str(directory), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _quarantine(self, path: Path) -> None:
        stamp = utcnow_iso().replace(":", "").replace("-", "")
        target = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            shutil.copy2(path, target)
            log.warning("Cópia de auditoria do cofre corrompido: %s", target)
        except OSError:
            pass

    # -------------------------------------------------------------- transacção
    @contextmanager
    def transaction(self, name: str) -> Iterator[Dict[str, Any]]:
        """Contexto de leitura-modificação-escrita com lock exclusivo.

        Uso::

            with vault.transaction(LEDGER) as ledger:
                ledger["owner_pocket_usd"] += 1.0

        Se o bloco lançar, nada é gravado (rollback natural).
        """
        with self._thread_lock(name):
            path = self.path_for(name)
            lock_path = path.with_suffix(path.suffix + ".lock")
            lock_handle = None
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_handle = open(lock_path, "a+", encoding="utf-8")
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                # ``read()`` NÃO pode ser usado aqui: ao reparar um ficheiro
                # ausente/corrompido chama ``write()``, que tenta adquirir o
                # mesmo ``flock`` exclusivo noutro file descriptor — em Linux
                # isso bloqueia para sempre (deadlock). Lemos "sem lock", que é
                # seguro porque já temos o lock do processo e do ficheiro, e a
                # reparação é feita em memória, gravada no fim da transacção.
                data = self._read_unlocked(name)
                # Nota: não há cópia de segurança a restaurar. A escrita só
                # acontece depois do `yield` regressar, por isso uma excepção
                # no bloco impede-a — o ficheiro fica intacto. O `deepcopy`
                # que aqui existia nunca foi usado; era trabalho a fingir.
                yield data
                data["schema_version"] = data.get("schema_version", 1)
                data["updated_at"] = utcnow_iso()
                self._atomic_write(path, data)
            except BaseException:
                log.debug("Transacção em %s revertida.", name)
                raise
            finally:
                if lock_handle is not None:
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                    lock_handle.close()

    # ------------------------------------------------------------------ helpers
    def list_files(self) -> List[str]:
        try:
            return sorted(
                p.name
                for p in self.root.iterdir()
                if p.is_file() and p.suffix == ".json"
            )
        except OSError:
            return []

    def health(self) -> Dict[str, Any]:
        """Estado do cofre para o endpoint ``/health`` e para o APK."""
        report: Dict[str, Any] = {"root": str(self.root), "files": {}}
        for name in VAULT_FILES:
            path = self.path_for(name)
            entry: Dict[str, Any] = {"exists": path.is_file()}
            if path.is_file():
                try:
                    entry["bytes"] = path.stat().st_size
                    with path.open("r", encoding="utf-8") as handle:
                        json.load(handle)
                    entry["valid_json"] = True
                except (OSError, json.JSONDecodeError) as exc:
                    entry["valid_json"] = False
                    entry["error"] = str(exc)
            report["files"][name] = entry
        return report


# Instância partilhada (singleton) usada por todo o backend.
vault = VaultStore()

__all__ = [
    "VaultStore",
    "vault",
    "LEDGER",
    "POST_MORTEM",
    "SQUAD_METRICS",
    "PENDING_APPROVALS",
    "VAULT_FILES",
    "default_ledger",
    "default_post_mortem",
    "default_squad_metrics",
    "default_pending_approvals",
]
