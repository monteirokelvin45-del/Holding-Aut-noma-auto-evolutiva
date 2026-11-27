#!/usr/bin/env python3
"""Simula o ciclo de vida de um cliente APK contra a API real.

Não substitui um emulador Android — não executa o APK. O que faz é exercitar o
mesmo **protocolo** que o APK usa, pela ordem em que ele o usa, e falhar se o
contrato se partir:

  1. arranque: as 8 leituras do refreshAll() do ViewModel;
  2. ligação ao /ws/live e leitura do snapshot (backlog + kill switch);
  3. ciclo de decisão humana: criar aprovação > $5, decidir SEM biometria
     (espera 428), decidir COM biometria (espera 200);
  4. botão vermelho: engage (423 nas escritas, leituras vivas), release;
  5. reserva de saque e abate de esquadrão (os caminhos que exigem biometria);
  6. relatório: por cada passo, o resultado esperado e o obtido.

Uso:
    PYTHONPATH=. python scripts/device_sim.py [--base-url http://127.0.0.1:8000]

Saída com código 1 se algum passo não bater certo.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import urllib.error
import urllib.request

RESULTADOS: list[tuple[str, bool, str]] = []


def registar(nome: str, ok: bool, detalhe: str = "") -> None:
    RESULTADOS.append((nome, ok, detalhe))
    marca = "OK  " if ok else "FALHA"
    print(f"  [{marca}] {nome}" + (f" — {detalhe}" if detalhe else ""))


def pedir(
    base: str,
    metodo: str,
    rota: str,
    *,
    token: str,
    corpo: Any = None,
    esperado: int = 200,
) -> tuple[int, Any]:
    url = base.rstrip("/") + rota
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Automaton-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            codigo, bruto = r.status, r.read()
    except urllib.error.HTTPError as exc:
        codigo, bruto = exc.code, exc.read()
    try:
        return codigo, json.loads(bruto) if bruto else None
    except ValueError:
        return codigo, bruto[:200]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="apk-test-token")
    args = parser.parse_args()
    base, token = args.base_url, args.token

    print("\n=== 1. arranque: as leituras do refreshAll() ===")
    for rota in ("/health", "/system/status", "/treasury", "/approvals",
                 "/squads", "/agents", "/governance", "/validation"):
        codigo, corpo = pedir(base, "GET", rota, token=token)
        ok = codigo == 200 and isinstance(corpo, (dict, list))
        extra = ""
        if ok and isinstance(corpo, dict):
            extra = f"{len(corpo)} campos"
        registar(f"GET {rota}", ok, extra or f"HTTP {codigo}")
        if not ok:
            return 1  # sem backend não vale a pena continuar

    print("\n=== 2. autenticação ===")
    codigo, _ = pedir(base, "POST", "/approvals", token="",
                      corpo={"title": "sem token", "kind": "custom"})
    registar("escrita sem token recusada", codigo == 401, f"HTTP {codigo}")

    print("\n=== 3. ciclo de decisão (biometria) ===")
    codigo, criada = pedir(base, "POST", "/approvals", token=token,
                           corpo={"title": "Simulador: compra de domínio",
                                  "kind": "custom", "amount_usd": 12.0,
                                  "reason": "device_sim"})
    registar("criar aprovação de $12", codigo == 200 and bool(criada),
             f"HTTP {codigo}")
    aprox = (criada or {}).get("id") or (criada or {}).get("approval_id")
    if aprox:
        codigo, _ = pedir(base, "POST", f"/approvals/{aprox}/decide", token=token,
                          corpo={"approve": True, "biometric_verified": False})
        registar("decidir SEM biometria -> 428", codigo == 428, f"HTTP {codigo}")
        codigo, decidida = pedir(base, "POST", f"/approvals/{aprox}/decide",
                                 token=token,
                                 corpo={"approve": True, "biometric_verified": True,
                                        "note": "device_sim"})
        ok = codigo == 200 and (decidida or {}).get("ok") is True
        registar("decidir COM biometria -> 200", ok, f"HTTP {codigo}")

    print("\n=== 4. botão vermelho (kill switch) ===")
    codigo, _ = pedir(base, "POST", "/kill-switch/engage", token=token,
                      corpo={"reason": "device_sim"})
    registar("engage", codigo == 200, f"HTTP {codigo}")
    codigo, _ = pedir(base, "GET", "/treasury", token=token)
    registar("leituras continuam vivas", codigo == 200, f"HTTP {codigo}")
    codigo, _ = pedir(base, "POST", "/approvals", token=token,
                      corpo={"title": "durante paragem", "kind": "custom",
                             "amount_usd": 1.0})
    registar("escritas bloqueadas com 423", codigo == 423, f"HTTP {codigo}")
    codigo, _ = pedir(base, "POST", "/kill-switch/release", token=token)
    registar("release (não pode ser bloqueado por si próprio)", codigo == 200,
             f"HTTP {codigo}")
    codigo, _ = pedir(base, "POST", "/approvals", token=token,
                      corpo={"title": "depois do release", "kind": "custom",
                             "amount_usd": 1.0})
    registar("escritas voltam a passar", codigo == 200, f"HTTP {codigo}")

    print("\n=== 5. saque e abate ===")
    codigo, reserva = pedir(base, "POST", "/treasury/payouts/reserve", token=token,
                            corpo={"amount_usd": 5.0, "channel": "mpesa",
                                   "destination": "841234567"})
    registar("reservar saque M-Pesa", codigo in (200, 400, 409, 428),
             f"HTTP {codigo} {(reserva or {}).get('error', '') if isinstance(reserva, dict) else ''}")
    # `cause` tem mínimo de 4 caracteres (SquadKillRequest) — a validação do
    # backend está certa, o simulador é que não pode mandar "x".
    codigo, abate = pedir(base, "POST", "/squads/sq_inexistente/kill", token=token,
                          corpo={"cause": "abate simulado", "biometric_verified": True})
    # Esquadrão inexistente: o backend pode devolver 404 (NotFound) ou 400
    # (validação de negócio). Ambos são recusas legítimas; o que se verifica é
    # que NÃO é 2xx a fingir sucesso.
    ok = codigo in (400, 404)
    detalhe = (abate or {}).get("error") if isinstance(abate, dict) else ""
    registar("abate de esquadrão inexistente é recusado", ok,
             f"HTTP {codigo} {detalhe or ''}")

    print("\n=== 6. WebSocket /ws/live ===")
    try:
        import asyncio

        import websockets

        async def ler_stream() -> tuple[bool, str]:
            ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
            async with websockets.connect(f"{ws_base}/ws/live") as ws:
                bruto = await asyncio.wait_for(ws.recv(), timeout=10)
                d = json.loads(bruto)
                tem_eventos = "events" in (d.get("payload") or {})
                tem_kill = "kill_switch" in (d.get("payload") or {})
                return (d.get("type") == "snapshot" and tem_kill,
                        f"snapshot com {len(d.get('payload', {}).get('events', []))} "
                        f"eventos, kill_switch={'sim' if tem_kill else 'NAO'}")

        ok, detalhe = asyncio.run(ler_stream())
        registar("snapshot do stream", ok, detalhe)
    except Exception as exc:  # noqa: BLE001
        registar("snapshot do stream", False, f"{type(exc).__name__}: {exc}")

    falhas = sum(1 for _, ok, _ in RESULTADOS if not ok)
    print(f"\n=== {len(RESULTADOS) - falhas}/{len(RESULTADOS)} passos OK ===")
    if falhas:
        print("Passos falhados:")
        for nome, ok, det in RESULTADOS:
            if not ok:
                print(f"  - {nome}: {det}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
