"""PayPal falso, local — para provar o caminho completo sem internet.

Não é um mock de conveniência. É um servidor HTTP que responde com o mesmo
formato JSON que a PayPal responde em sandbox, e é o **SDK oficial** que faz
o parsing. A diferença importa: um mock que devolve objectos Python já
construídos testa o *nosso* código contra as *nossas* expectativas. Este
testa o nosso código contra o parser real do `paypal-server-sdk`, que é onde
os erros de nomes de campos aparecem.

O que este servidor imita
-------------------------
* ``POST /v1/oauth2/token``          → token de acesso (client_credentials)
* ``POST /v2/checkout/orders`` → cria pedido, ``status=CREATED``,
  com link ``approve``;
* ``GET  /v2/checkout/orders/{id}`` → estado (CREATED/APPROVED);
* ``POST /v2/checkout/orders/{id}/capture`` → ``COMPLETED`` com
  ``purchase_units[].payments.captures[].amount.value``.

O que este servidor **não** faz
-------------------------------
Não valida assinaturas, não conhece contas reais, e não move dinheiro. É
um ensaio. Está marcado com o cabeçalho ``X-Fake-PayPal`` em todas as
respostas, para nunca ser confundido com tráfego verdadeiro nos logs.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


class EstadoFake:
    """Pedidos em memória. Reinicia com o servidor."""

    def __init__(self) -> None:
        self.pedidos: Dict[str, Dict[str, Any]] = {}
        self.sequencia = 0
        # Sufixo único por servidor: os ids da PayPal real nunca se repetem,
        # e ids repetidos entre instâncias de teste escondiam colisões de
        # deduplicação que em produção não existem.
        import uuid

        self.instancia = uuid.uuid4().hex[:6].upper()
        self.lock = threading.Lock()
        self.tokens_emitidos: List[str] = []
        self.pedidos_api: List[Tuple[str, str]] = []

    def novo_pedido(self, corpo: Dict[str, Any], base: str) -> Dict[str, Any]:
        with self.lock:
            self.sequencia += 1
            order_id = f"FAKE{self.instancia}{self.sequencia:04d}"
        unidades = corpo.get("purchase_units") or [{}]
        valor = (unidades[0].get("amount") or {}).get("value", "0.00")
        moeda = (unidades[0].get("amount") or {}).get("currency_code", "USD")
        pedido = {
            "id": order_id,
            "status": "CREATED",
            "intent": corpo.get("intent", "CAPTURE"),
            "purchase_units": [
                {
                    "reference_id": unidades[0].get("reference_id", "default"),
                    "amount": {"currency_code": moeda, "value": valor},
                }
            ],
            "links": [
                {
                    "href": f"{base}/checkoutnow?token={order_id}",
                    "rel": "approve",
                    "method": "GET",
                },
                {
                    "href": f"{base}/v2/checkout/orders/{order_id}",
                    "rel": "self",
                    "method": "GET",
                },
            ],
        }
        self.pedidos[order_id] = pedido
        return pedido

    def aprovar(self, order_id: str) -> bool:
        """O que o comprador faria ao carregar em 'Pagar'."""
        pedido = self.pedidos.get(order_id)
        if not pedido or pedido["status"] != "CREATED":
            return False
        pedido["status"] = "APPROVED"
        return True

    def capturar(self, order_id: str) -> Optional[Dict[str, Any]]:
        pedido = self.pedidos.get(order_id)
        if not pedido:
            return None
        # A PayPal recusa capturar o mesmo pedido duas vezes (ORDER_ALREADY_CAPTURED).
        if pedido["status"] == "COMPLETED":
            return None
        if pedido["status"] != "APPROVED":
            return None
        valor = pedido["purchase_units"][0]["amount"]
        captura_id = "CAP" + order_id
        pedido["status"] = "COMPLETED"
        pedido["purchase_units"][0]["payments"] = {
            "captures": [
                {
                    "id": captura_id,
                    "status": "COMPLETED",
                    "amount": dict(valor),
                    "final_capture": True,
                }
            ]
        }
        return {
            "id": order_id,
            "status": "COMPLETED",
            "payer": {"email_address": "comprador@exemplo.mz"},
            "purchase_units": pedido["purchase_units"],
        }


def _servir(estado: EstadoFake) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- utilidades
        def _json(self, codigo: int, corpo: Any) -> None:
            dados = json.dumps(corpo).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dados)))
            self.send_header("X-Fake-PayPal", "1")
            self.end_headers()
            self.wfile.write(dados)

        def _erro(self, codigo: int, nome: str, mensagem: str) -> None:
            self._json(
                codigo,
                {"name": nome, "message": mensagem, "details": [], "debug_id": "fake"},
            )

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            """Regista método+rota de *todos* os pedidos, inclusive os 501.

            `BaseHTTPRequestHandler` chama isto antes do despacho, por isso
            apanha também os métodos que não implementamos — foi assim que se
            descobriu que o SDK faz um pedido que não é POST nem GET.
            """
            estado.pedidos_api.append((str(self.command), str(self.path)))

        def log_message(self, *args: Any) -> None:  # silencia o servidor
            return

        def _drenar(self) -> None:
            """Lê o corpo do pedido **mesmo quando não é JSON**.

            Sem isto, os 29 bytes de `grant_type=client_credentials` ficavam
            no socket e eram interpretados como o início do pedido seguinte:
            o método aparecia como "grant_type=client_credentialsPOST". Um
            servidor de teste com bug de pipelining dá erros que parecem
            estar no código testado.
            """
            tamanho = int(self.headers.get("Content-Length") or 0)
            if tamanho:
                self.rfile.read(tamanho)

        def _corpo(self) -> Dict[str, Any]:
            tamanho = int(self.headers.get("Content-Length") or 0)
            if not tamanho:
                return {}
            try:
                return json.loads(self.rfile.read(tamanho) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _base(self) -> str:
            host = self.headers.get("Host", "127.0.0.1")
            return f"http://{host}"

        # -------------------------------------------------------------- rotas
        def do_POST(self) -> None:  # noqa: N802
            caminho = urlparse(self.path).path

            if caminho == "/v1/oauth2/token":
                self._drenar()
                # O SDK pede um token antes de qualquer chamada.
                token = f"A21AA-fake-{len(estado.tokens_emitidos) + 1}"
                estado.tokens_emitidos.append(token)
                self._json(
                    200,
                    {
                        "scope": "https://uri.paypal.com/services/payments/checkout",
                        "access_token": token,
                        "token_type": "Bearer",
                        "app_id": "FAKEAPP",
                        "expires_in": 32400,
                    },
                )
                return

            if caminho == "/v2/checkout/orders":
                corpo = self._corpo()
                unidades = corpo.get("purchase_units") or []
                if not unidades:
                    self._erro(400, "INVALID_REQUEST", "purchase_units em falta")
                    return
                valor = (unidades[0].get("amount") or {}).get("value", "")
                if not re.fullmatch(r"\d+\.\d{2}", str(valor)):
                    self._erro(
                        422, "UNPROCESSABLE_ENTITY",
                        f"valor inválido: {valor!r} (esperado 12.34)",
                    )
                    return
                self._json(201, estado.novo_pedido(corpo, self._base()))
                return

            m = re.fullmatch(r"/v2/checkout/orders/([^/]+)/capture", caminho)
            if m:
                order_id = m.group(1)
                if order_id not in estado.pedidos:
                    self._erro(404, "RESOURCE_NOT_FOUND", f"pedido {order_id} não existe")
                    return
                if estado.pedidos[order_id]["status"] == "CREATED":
                    self._erro(
                        422, "ORDER_NOT_APPROVED",
                        "Payer has not yet approved the Order for payment.",
                    )
                    return
                capturado = estado.capturar(order_id)
                if capturado is None:
                    self._erro(422, "ORDER_ALREADY_CAPTURED", "Order already captured.")
                    return
                self._json(201, capturado)
                return

            if caminho == "/__fake/aprovar":
                corpo = self._corpo()
                ok = estado.aprovar(str(corpo.get("order_id", "")))
                self._json(200 if ok else 404, {"aprovado": ok})
                return

            self._drenar()
            self._erro(404, "RESOURCE_NOT_FOUND", f"rota falsa inexistente: {caminho}")

        def do_GET(self) -> None:  # noqa: N802
            caminho = urlparse(self.path).path
            m = re.fullmatch(r"/v2/checkout/orders/([^/]+)", caminho)
            if m:
                pedido = estado.pedidos.get(m.group(1))
                if pedido is None:
                    self._erro(404, "RESOURCE_NOT_FOUND", "pedido não existe")
                    return
                self._json(200, pedido)
                return
            if caminho == "/__fake/estado":
                self._json(
                    200,
                    {
                        "pedidos": estado.pedidos,
                        "chamadas": estado.pedidos_api,
                        "tokens": len(estado.tokens_emitidos),
                    },
                )
                return
            self._erro(404, "RESOURCE_NOT_FOUND", f"rota falsa inexistente: {caminho}")

    return Handler


class FakePayPal:
    """Servidor PayPal falso, para usar como context manager em teste."""

    def __init__(self, porta: int = 0) -> None:
        self.estado = EstadoFake()
        self._servidor = ThreadingHTTPServer(
            ("127.0.0.1", porta), _servir(self.estado)
        )
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host, porta = self._servidor.server_address[:2]
        return f"http://{host}:{porta}"

    def aprovar(self, order_id: str) -> bool:
        """Simula o comprador a aprovar no PayPal."""
        return self.estado.aprovar(order_id)

    def __enter__(self) -> "FakePayPal":
        self._thread = threading.Thread(target=self._servidor.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._servidor.shutdown()
        self._servidor.server_close()
