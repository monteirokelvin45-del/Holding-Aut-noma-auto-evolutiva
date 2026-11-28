"""Servidores MCP (Model Context Protocol) como fonte de ferramentas reais.

Porque isto existe
------------------
Declarar 49 ferramentas num registo ainda é escrever 49 ligações à mão. O
MCP resolve o problema ao contrário: um servidor expõe dezenas de tools e o
agente descobre-as em tempo de execução. É a diferença entre uma caixa de
ferramentas fechada e uma oficina onde se pode pendurar mais.

Nota importante sobre isto não ser "mais um cliente MCP"
--------------------------------------------------------
Existia o SDK oficial (``mcp`` no PyPI, MIT). **Não foi usado**, e a razão é
concreta: a versão 2.3.0 arrasta ``sse-starlette>=3.0.0``, que exige
``starlette>=0.49.1``, e este projecto está preso a ``starlette<0.42`` por
causa do FastAPI 0.115.6. Instalar o SDK partiu o backend — confirmado:

    TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'

Uma dependência *opcional* não pode derrubar o servidor. O protocolo MCP é
JSON-RPC 2.0 sobre stdio; falar directamente são ~150 linhas e zero risco
para quem já funciona. Se um dia o projecto subir de FastAPI, trocar por
``mcp`` é uma alteração local a este ficheiro.

O que este módulo **não** faz
----------------------------
Não confia em nada do que um servidor diz. Uma descrição de tool vinda de
um servidor de terceiros é conteúdo não confiável — passa pelo escudo de
prompt injection antes de chegar ao agente, tal como uma página web.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logger import get_logger

log = get_logger("core.mcp")

#: Versão do protocolo que este cliente fala.
PROTOCOLO = "2024-11-05"

#: Tempo máximo para uma resposta do servidor (segundos).
TIMEOUT_PADRAO = 25.0


# ==========================================================================
# Especificação de servidor
# ==========================================================================
@dataclass
class ServidorMCP:
    """Um servidor MCP declarado em ``backend/config/mcp_servers.json``.

    ``comando``/``args`` são o transporte stdio. ``url`` é reservado para o
    transporte HTTP (ainda não implementado — melhor dizer do que fingir).
    """

    nome: str
    comando: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    descricao: str = ""
    licenca: str = ""
    #: Variáveis de ambiente sem as quais o servidor não arranca.
    requer_env: List[str] = field(default_factory=list)
    #: `True` quando o servidor imita uma app de terceiros (risco de ban).
    terceiros: bool = False
    #: `True` quando alguma tool pode gastar dinheiro ou publicar.
    acao_sensivel: bool = False

    def faltam_env(self) -> List[str]:
        return [v for v in self.requer_env if not os.environ.get(v)]

    def pronto(self) -> bool:
        """Arranca? Comando existe e as variáveis estão definidas."""
        if self.url:
            return False  # transporte HTTP não implementado
        if not self.comando:
            return False
        if self.faltam_env():
            return False
        return shutil.which(self.comando) is not None

    def motivo(self) -> str:
        if self.url:
            return "transporte HTTP não implementado neste cliente (stdio only)"
        if not self.comando:
            return "sem comando definido"
        faltam = self.faltam_env()
        if faltam:
            return f"requer variáveis de ambiente: {', '.join(faltam)}"
        if shutil.which(self.comando) is None:
            return f"comando não encontrado: {self.comando}"
        return "pronto"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nome": self.nome,
            "descricao": self.descricao,
            "licenca": self.licenca,
            "pronto": self.pronto(),
            "motivo": self.motivo(),
            "terceiros": self.terceiros,
            "acao_sensivel": self.acao_sensivel,
        }


# ==========================================================================
# Cliente JSON-RPC sobre stdio
# ==========================================================================
class ErroMCP(RuntimeError):
    """Falha na conversa com um servidor MCP."""


class ClienteMCP:
    """Cliente mínimo: initialize → tools/list → tools/call.

    O protocolo é JSON-RPC 2.0, uma mensagem por linha. Não há nada de
    exotérico: escrever isto à mão é mais seguro do que arrastar uma árvore
    de dependências que colide com a que já existe.
    """

    def __init__(self, servidor: ServidorMCP, timeout: float = TIMEOUT_PADRAO) -> None:
        self.servidor = servidor
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._proximo_id = 0
        self.ferramentas: List[Dict[str, Any]] = []
        self.avisos: List[str] = []

    # ------------------------------------------------------------- ciclo de vida
    def __enter__(self) -> "ClienteMCP":
        self.iniciar()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.fechar()

    def iniciar(self) -> None:
        if not self.servidor.pronto():
            raise ErroMCP(
                f"servidor {self.servidor.nome} indisponível: {self.servidor.motivo()}"
            )
        ambiente = {**os.environ, **self.servidor.env}
        try:
            self._proc = subprocess.Popen(
                [self.servidor.comando, *self.servidor.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=ambiente,
            )
        except OSError as exc:
            raise ErroMCP(f"não foi possível arrancar {self.servidor.nome}: {exc}") from exc

        resposta = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOLO,
                "capabilities": {},
                "clientInfo": {"name": "automaton-holding", "version": "1.0"},
            },
        )
        info = (resposta.get("result") or {}).get("serverInfo") or {}
        log.info(
            "MCP conectado: %s (%s v%s)",
            self.servidor.nome,
            info.get("name", "?"),
            info.get("version", "?"),
        )
        self._notificar("notifications/initialized", {})

    def fechar(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                self._proc.kill()
            except OSError:
                pass
        self._proc = None

    # ------------------------------------------------------------------ JSON-RPC
    def _enviar(self, mensagem: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise ErroMCP("servidor não está a correr")
        try:
            self._proc.stdin.write(json.dumps(mensagem) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ErroMCP(f"servidor {self.servidor.nome} fechou o canal: {exc}") from exc

    def _notificar(self, metodo: str, params: Dict[str, Any]) -> None:
        self._enviar({"jsonrpc": "2.0", "method": metodo, "params": params})

    def _rpc(self, metodo: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Envia um pedido e espera pela resposta com o mesmo id.

        Com lock: dois threads a escrever no mesmo stdin intercalariam
        mensagens e o servidor perderia o fio. O cofre tem o mesmo problema
        e resolve-o da mesma maneira.
        """
        with self._lock:
            self._proximo_id += 1
            identificador = self._proximo_id
            self._enviar(
                {"jsonrpc": "2.0", "id": identificador, "method": metodo, "params": params}
            )
            limite = time.monotonic() + self.timeout
            while True:
                restante = limite - time.monotonic()
                if restante <= 0:
                    raise ErroMCP(
                        f"{self.servidor.nome}: sem resposta a {metodo} "
                        f"em {self.timeout:.0f}s"
                    )
                pronto, _, _ = select.select([self._proc.stdout], [], [], restante)
                if not pronto:
                    continue
                linha = self._proc.stdout.readline()
                if not linha:
                    raise ErroMCP(f"{self.servidor.nome}: EOF a espera de {metodo}")
                try:
                    mensagem = json.loads(linha)
                except json.JSONDecodeError:
                    # Servidores escrevem avisos no stdout; ignorar em vez de
                    # rebentar por causa de uma linha que não é JSON.
                    log.debug("MCP %s: linha não-JSON ignorada", self.servidor.nome)
                    continue
                if mensagem.get("id") != identificador:
                    continue  # notificação ou resposta antiga
                if "error" in mensagem:
                    erro = mensagem["error"]
                    raise ErroMCP(
                        f"{self.servidor.nome}: {erro.get('message', erro)}"
                    )
                return mensagem

    # -------------------------------------------------------------- ferramentas
    def listar_ferramentas(self) -> List[Dict[str, Any]]:
        """Descobre as tools do servidor.

        As descrições passam pelo escudo de prompt injection: são texto
        vindo de fora, exactamente como uma página web. Um servidor
        malicioso (ou comprometido) pode esconder instruções no campo
        ``description`` de uma tool.
        """
        resposta = self._rpc("tools/list", {})
        cruas = (resposta.get("result") or {}).get("tools") or []
        descobertas: List[Dict[str, Any]] = []
        for crua in cruas:
            nome = str(crua.get("name") or "")
            if not nome:
                continue
            descricao = str(crua.get("description") or "")
            limpa, problemas = self._escudar(descricao)
            if problemas:
                self.avisos.append(
                    f"{self.servidor.nome}.{nome}: descrição com "
                    f"{len(problemas)} padrão(ões) suspeito(s)"
                )
            descobertas.append(
                {
                    "nome": f"mcp__{self.servidor.nome}__{nome}",
                    "nome_no_servidor": nome,
                    "servidor": self.servidor.nome,
                    "descricao": limpa[:400],
                    "schema": crua.get("inputSchema") or {},
                    "terceiros": self.servidor.terceiros,
                    "acao_sensivel": self.servidor.acao_sensivel,
                }
            )
        self.ferramentas = descobertas
        return descobertas

    @staticmethod
    def _escudar(texto: str) -> tuple:
        """Passa a descrição pelo escudo anti-injection, se disponível."""
        try:
            from .security_guard import prompt_shield

            resultado = prompt_shield.scan(texto, source="mcp_tool_description")
            # ShieldResult: .text (sanitizado), .reasons (padrões), .blocked
            problemas = list(getattr(resultado, "reasons", None) or [])
            limpo = getattr(resultado, "text", None) or texto
            return str(limpo), problemas
        except Exception as exc:  # noqa: BLE001
            log.debug("Escudo indisponível para descrição MCP: %s", exc)
            return texto, []

    def chamar(self, nome_no_servidor: str, argumentos: Dict[str, Any]) -> Dict[str, Any]:
        """Chama uma tool. Devolve o conteúdo textual e os metadados."""
        resposta = self._rpc(
            "tools/call",
            {"name": nome_no_servidor, "arguments": argumentos or {}},
        )
        resultado = resposta.get("result") or {}
        textos: List[str] = []
        for bloco in resultado.get("content") or []:
            if isinstance(bloco, dict) and bloco.get("type") == "text":
                textos.append(str(bloco.get("text") or ""))
        return {
            "ok": not resultado.get("isError", False),
            "servidor": self.servidor.nome,
            "ferramenta": nome_no_servidor,
            "texto": "\n".join(textos),
            "isError": bool(resultado.get("isError", False)),
        }


# ==========================================================================
# Catálogo configurável
# ==========================================================================
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "mcp_servers.json"


def carregar_servidores(caminho: Optional[Path] = None) -> List[ServidorMCP]:
    """Lê ``mcp_servers.json``. Ausente = lista vazia, não erro.

    Um ficheiro de configuração em falta não pode impedir o arranque do
    backend: o MCP é um extra, não a espinha.
    """
    caminho = caminho or CONFIG_PATH
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("mcp_servers.json ilegível (%s); MCP desactivado.", exc)
        return []

    servidores: List[ServidorMCP] = []
    for raw in dados.get("servers") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        servidores.append(
            ServidorMCP(
                nome=str(raw["name"]),
                comando=str(raw.get("command") or ""),
                args=[str(a) for a in (raw.get("args") or [])],
                env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
                url=str(raw.get("url") or ""),
                descricao=str(raw.get("description") or ""),
                licenca=str(raw.get("license") or ""),
                requer_env=[str(v) for v in (raw.get("requires_env") or [])],
                terceiros=bool(raw.get("third_party", False)),
                acao_sensivel=bool(raw.get("sensitive_action", False)),
            )
        )
    return servidores


def catalogo() -> Dict[str, Any]:
    """Estado do MCP: o que está configurado e o que arranca de facto."""
    servidores = carregar_servidores()
    prontos = [s for s in servidores if s.pronto()]
    return {
        "configurado": bool(servidores),
        "total": len(servidores),
        "prontos": len(prontos),
        "servidores": [s.to_dict() for s in servidores],
        "transporte": "stdio (JSON-RPC 2.0); HTTP ainda não implementado",
        "cliente": "próprio, sem SDK — ver docstring do módulo",
        "nota": (
            "`pronto=false` significa que falta o comando ou uma variável de "
            "ambiente. Não é um erro: é o estado real até alguém configurar "
            "as chaves."
        ),
    }


def descobrir(nome: str) -> Dict[str, Any]:
    """Arranca um servidor, lista as tools e fecha. Para diagnóstico."""
    servidor = next((s for s in carregar_servidores() if s.nome == nome), None)
    if servidor is None:
        raise ErroMCP(f"servidor MCP não configurado: {nome!r}")
    with ClienteMCP(servidor) as cliente:
        ferramentas = cliente.listar_ferramentas()
        return {
            "servidor": nome,
            "ferramentas": ferramentas,
            "total": len(ferramentas),
            "avisos": cliente.avisos,
        }


def executar(nome_servidor: str, nome_ferramenta: str, argumentos: Dict[str, Any]) -> Dict[str, Any]:
    """Chama uma tool num servidor MCP. Abre e fecha a ligação por chamada.

    Abrir por chamada é mais lento e mais seguro: um servidor que fica
    pendurado entre chamadas é um processo a mais que ninguém vigia. Quando
    o volume justificar, mantém-se uma sessão viva — mas mede-se primeiro.
    """
    servidor = next(
        (s for s in carregar_servidores() if s.nome == nome_servidor), None
    )
    if servidor is None:
        raise ErroMCP(f"servidor MCP não configurado: {nome_servidor!r}")
    # Uma tool sensível (gasta dinheiro / publica) não corre por aqui: passa
    # obrigatoriamente pelo approval_gate. Ver agent_tools.py.
    if servidor.acao_sensivel:
        raise ErroMCP(
            f"{nome_servidor} está marcado como acção sensível: tem de passar "
            "pelo approval_gate, não por execução directa."
        )
    with ClienteMCP(servidor) as cliente:
        return cliente.chamar(nome_ferramenta, argumentos)
