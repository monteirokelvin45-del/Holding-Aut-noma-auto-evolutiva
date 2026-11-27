#!/usr/bin/env python3
"""Verifica que o APK e a API continuam a falar a mesma língua.

Extrai os modelos ``@Serializable`` de ``ApiService.kt`` e compara-os com a
resposta real de cada rota do FastAPI. Não precisa de Android SDK nem de
emulador — só do backend a importar.

Uso:
    PYTHONPATH=. python scripts/check_apk_contract.py [--regenerate-fixtures]

Sem ``--regenerate-fixtures`` é só leitura e devolve código de saída 1 se
houver campos que o Kotlin espera e a API não envia (o que faria o APK mostrar
zeros sem erro nenhum).

Com ``--regenerate-fixtures`` reescreve ``android_apk/app/src/test/resources/
fixtures/*.json`` com a saída actual — é assim que os testes JVM de contrato
ficam sincronizados com o backend.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("API_ACCESS_TOKEN", "test-token")
os.environ.setdefault("APP_ENV", "test")

API_KT = ROOT / "android_apk/app/src/main/java/com/automaton/holding/data/ApiService.kt"
FIXTURES = ROOT / "android_apk/app/src/test/resources/fixtures"

# model Kotlin -> (rota, caminho até ao objecto dentro da resposta)
CONTRATOS: dict[str, tuple[str, tuple[str, ...]]] = {
    "HealthResponse": ("/health", ()),
    "KillSwitchState": ("/health", ("kill_switch",)),
    "TreasuryResponse": ("/treasury", ()),
    "TreasurySnapshot": ("/treasury", ("snapshot",)),
    "Balances": ("/treasury", ("snapshot", "balances_usd")),
    "PhaseSplit": ("/treasury", ("snapshot", "split")),
    "PhaseMeta": ("/treasury", ("snapshot", "phase_meta")),
    "PhaseThresholds": ("/treasury", ("snapshot", "phase_thresholds_usd")),
    "ChannelsStatus": ("/treasury", ("channels",)),
    "FxInfo": ("/treasury", ("channels", "fx")),
    "ApprovalsResponse": ("/approvals", ()),
    "ApprovalDeck": ("/approvals", ("deck",)),
    "PortfolioResponse": ("/squads", ()),
    "PortfolioSummary": ("/squads", ("summary",)),
    "AiCatalog": ("/agents", ()),
    "BridgeInfo": ("/agents", ("bridge",)),
    "GovernanceResponse": ("/governance", ()),
    "HrReport": ("/governance", ("hr",)),
    "HrSummary": ("/governance", ("hr", "summary")),
    "JudgeReport": ("/governance", ("judge",)),
    "LegalReport": ("/governance", ("legal",)),
    "AccountingPayload": ("/governance", ("accounting",)),
    "ValidationReport": ("/validation", ()),
    "ValidationPayload": ("/validation", ("report",)),
    "ValidationSummary": ("/validation", ("report", "summary")),
    "SystemStatusResponse": ("/system/status", ()),
    "TreasuryCounters": ("/system/status", ("treasury",)),
    "ApprovalsCounters": ("/system/status", ("approvals",)),
}


def kotlin_models() -> dict[str, set[str]]:
    """Lê ``data class X(...)`` e devolve ``{X: {nomes JSON}}``."""
    fonte = API_KT.read_text(encoding="utf-8")
    modelos: dict[str, set[str]] = {}
    for achado in re.finditer(r"data class (\w+)\((.*?)\n\)", fonte, re.S):
        nome, corpo = achado.group(1), achado.group(2)
        campos: set[str] = set()
        for linha in corpo.splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("//"):
                continue
            serial = re.search(r'@SerialName\("([^"]+)"\)', linha)
            ident = re.search(r"(?:val|var)\s+(\w+)\s*:", linha)
            if ident:
                campos.add(serial.group(1) if serial else ident.group(1))
        modelos[nome] = campos
    return modelos


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regenerate-fixtures", action="store_true")
    args = parser.parse_args()

    from fastapi.testclient import TestClient

    from backend.main import app

    modelos = kotlin_models()
    if not modelos:
        print("erro: nenhum modelo encontrado em ApiService.kt", file=sys.stderr)
        return 1

    cliente = TestClient(app)
    headers = {"X-Automaton-Token": os.environ["API_ACCESS_TOKEN"]}
    respostas: dict[str, dict] = {}

    def resposta(rota: str) -> dict:
        if rota not in respostas:
            r = cliente.get(rota, headers=headers)
            if r.status_code >= 400:
                raise SystemExit(f"erro: GET {rota} -> HTTP {r.status_code}: {r.text[:200]}")
            respostas[rota] = r.json()
        return respostas[rota]

    print(f"APK: {len(modelos)} modelos, {sum(len(c) for c in modelos.values())} campos\n")
    falhas = 0
    for modelo, (rota, caminho) in CONTRATOS.items():
        if modelo not in modelos:
            print(f"AVISO  {modelo:<22} não existe em ApiService.kt (renomeado?)")
            falhas += 1
            continue
        no = resposta(rota)
        for parte in caminho:
            if not isinstance(no, dict) or parte not in no:
                print(f"FALHA  {modelo:<22} {rota}: caminho '{'.'.join(caminho)}' ausente")
                falhas += 1
                no = {}
                break
            no = no[parte]
        if not isinstance(no, dict):
            continue
        ausentes = modelos[modelo] - set(no)
        if ausentes:
            falhas += 1
            print(f"FALHA  {modelo:<22} {rota}: a API não envia {sorted(ausentes)}")
        else:
            print(f"ok     {modelo:<22} {rota}")

    if args.regenerate_fixtures:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        nomes = {
            "health": "/health", "system_status": "/system/status",
            "treasury": "/treasury", "approvals": "/approvals",
            "squads": "/squads", "agents": "/agents",
            "governance": "/governance", "validation": "/validation",
        }
        for nome, rota in nomes.items():
            texto = json.dumps(resposta(rota), ensure_ascii=False, indent=2)
            (FIXTURES / f"{nome}.json").write_text(texto, encoding="utf-8")
            print(f"fixture {nome}.json ({len(texto)} bytes)")
        try:
            with cliente.websocket_connect("/ws/live") as ws:
                primeiro = ws.receive_text()
            (FIXTURES / "ws_live_snapshot.json").write_text(primeiro, encoding="utf-8")
            print("fixture ws_live_snapshot.json")
        except Exception as exc:  # noqa: BLE001
            print(f"aviso: WebSocket não produziu snapshot ({exc})")

    print(f"\n{'FALHAS: ' + str(falhas) if falhas else 'contrato íntegro'}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
