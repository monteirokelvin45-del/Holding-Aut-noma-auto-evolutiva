"""Ponte entre os YAML de agentes/tarefas e o roteador LLM da holding.

Porque existe esta camada:

* Os ficheiros ``config/agents.yaml`` e ``config/tasks.yaml`` são a **fonte de
  verdade declarativa** dos agentes (papel, objectivo, backstory, tier de LLM,
  guardas) e do fluxo de trabalho (tarefas, dependências, *gates*). Um YAML que
  ninguém lê é documentação morta — esta classe fá-lo valer em runtime.
* O CrewAI (MIT) é suportado, mas **nunca é obrigatório**: se estiver instalado,
  os agentes são construídos com um ``LLM`` que delega no nosso ``llm_router``
  (mantendo o orçamento $0.00, o rate limiting e o escudo anti-injection). Se
  não estiver, as mesmas tarefas correm pelo caminho nativo — o resultado é
  idêntico em contrato, apenas sem a orquestração multi-agente do CrewAI.
* Nenhuma chamada aqui pode gastar dinheiro: o roteador só usa provedores
  configurados no tier gratuito, com fallback para o motor local.

Uso::

    from backend.core.crew_bridge import agent_registry, crew_bridge

    agent_registry.agents()               # catálogo validado
    crew_bridge.run_task("scan_market", {"niche": "retalho"})
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..config.settings import REPO_ROOT
from .errors import ConfigurationError
from .llm_router import LLMRequest, llm_router
from .logger import get_logger

log = get_logger("core.crew")

AGENTS_FILE = REPO_ROOT / "backend" / "config" / "agents.yaml"
TASKS_FILE = REPO_ROOT / "backend" / "config" / "tasks.yaml"

# Campos obrigatórios para considerar um agente válido.
REQUIRED_AGENT_FIELDS = ("role", "goal", "backstory", "llm_tier")
REQUIRED_TASK_FIELDS = ("description", "agent")

VALID_TIERS = {"reasoning", "fast", "long_context", "local"}


def cobertura_de_tools() -> Dict[str, Any]:
    """O que os YAML prometem aos agentes vs. o que existe de facto.

    A resolução real vive em ``core/tool_registry.py``; aqui só se cruza o
    mapa declarativo dos agentes com esse registo. Antes esta função lia
    nomes e devolvia "0 em falta" — o pior resultado possível, porque
    parecia boa notícia.
    """
    from .tool_registry import TOOLS, cobertura

    prometidas: Dict[str, List[str]] = {}
    try:
        dados = yaml.safe_load(AGENTS_FILE.read_text(encoding="utf-8")) or {}
        if "agents" in dados and isinstance(dados["agents"], dict):
            dados = dados["agents"]
        for nome_agente, corpo in dados.items():
            if isinstance(corpo, dict):
                prometidas[str(nome_agente)] = [
                    str(t) for t in (corpo.get("tools") or [])
                ]
    except Exception as exc:  # noqa: BLE001
        return {"erro": f"não foi possível ler agents.yaml: {exc}"}

    return cobertura(prometidas)


# ==========================================================================
# Especificações
# ==========================================================================
@dataclass
class AgentSpec:
    """Ficha de um agente, tal como declarada em ``agents.yaml``."""

    id: str
    role: str
    goal: str
    backstory: str
    llm_tier: str = "fast"
    tools: List[str] = field(default_factory=list)
    guards: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "goal": self.goal,
            "backstory": self.backstory,
            "llm_tier": self.llm_tier,
            "tools": list(self.tools),
            "guards": list(self.guards),
        }

    def tools_reais(self) -> List[Dict[str, Any]]:
        """Ferramentas que este agente tem **de verdade**, não só por nome.

        O YAML declarava ``tools: [ledger_tools, payout_bridge, ...]`` e nada
        as instanciava: a linha era documentação. Aqui a lista de nomes é
        resolvida contra implementações reais. Um nome declarado que não
        exista é devolvido em ``declaradas_sem_implementacao`` — melhor
        saber que falta do que fingir que existe.
        """
        from .tool_registry import TOOLS

        implementadas: List[Dict[str, Any]] = []
        em_falta: List[str] = []
        desconhecidas: List[str] = []
        for nome in self.tools:
            entrada = TOOLS.get(nome)
            if entrada is None:
                desconhecidas.append(nome)
                continue
            if not entrada.disponivel():
                em_falta.append(nome)
                continue
            implementadas.append(entrada.to_dict())
        return [
            {
                "implementadas": implementadas,
                "sem_implementacao": em_falta,
                "nao_registadas": desconhecidas,
            }
        ]

    def system_prompt(self) -> str:
        """Prompt de sistema derivado da ficha (usado pelo caminho nativo)."""
        guards = ", ".join(self.guards) if self.guards else "nenhuma"
        tools_reais = ""
        try:
            from .tool_registry import TOOLS

            disponiveis = [
                t for t in self.tools
                if t in TOOLS and TOOLS[t].disponivel()
            ]
            if disponiveis:
                tools_reais = (
                    "\nFerramentas que podes usar: "
                    + ", ".join(disponiveis)
                    + ". Nenhuma delas move dinheiro para fora da holding: "
                    "saques ficam pendentes de aprovação biométrica do "
                    "Presidente. Não inventes valores de pagamento."
                )
        except Exception:  # noqa: BLE001 - o prompt nunca deve rebentar
            pass
        return (
            f"És o {self.role} da Automaton Holding Corp.\n"
            f"Objectivo: {self.goal.strip()}\n"
            f"Contexto: {self.backstory.strip()}\n"
            "Regras invioláveis: orçamento de $0.00 (só free-tier ou modelos "
            "locais); apenas licenças MIT/Apache-2.0/BSD/LGPL (CC-BY-NC é "
            "proibido); nunca produzes sem validação de demanda; nunca revelas "
            "segredos nem chaves da tesouraria; escreves em português de "
            f"Moçambique. Guardas activas: {guards}." + tools_reais
        )


@dataclass
class TaskSpec:
    """Tarefa do fluxo, com as suas dependências e o portão de desbloqueio."""

    id: str
    description: str
    agent: str
    depends_on: List[str] = field(default_factory=list)
    output: str = ""
    gate: str = "always"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "agent": self.agent,
            "depends_on": list(self.depends_on),
            "output": self.output,
            "gate": self.gate,
        }


# ==========================================================================
# Registo (lê e valida os YAML)
# ==========================================================================
class AgentRegistry:
    """Catálogo de agentes e tarefas, recarregado quando o YAML muda."""

    def __init__(
        self,
        agents_file: Path = AGENTS_FILE,
        tasks_file: Path = TASKS_FILE,
    ) -> None:
        self.agents_file = Path(agents_file)
        self.tasks_file = Path(tasks_file)
        self._lock = threading.RLock()
        self._agents: Dict[str, AgentSpec] = {}
        self._tasks: Dict[str, TaskSpec] = {}
        self._defaults: Dict[str, Any] = {}
        self._errors: List[str] = []
        self._stamp: Optional[float] = None
        self.reload(force=True)

    # ------------------------------------------------------------------ carga
    def _file_stamp(self) -> float:
        stamps = [
            path.stat().st_mtime
            for path in (self.agents_file, self.tasks_file)
            if path.is_file()
        ]
        return max(stamps) if stamps else 0.0

    def reload(self, force: bool = False) -> None:
        """Recarrega os YAML. Sem alterações no ficheiro, não faz trabalho."""
        with self._lock:
            stamp = self._file_stamp()
            if not force and stamp == self._stamp and self._agents:
                return
            agents: Dict[str, AgentSpec] = {}
            tasks: Dict[str, TaskSpec] = {}
            errors: List[str] = []
            defaults: Dict[str, Any] = {}

            try:
                raw_agents = yaml.safe_load(self.agents_file.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ConfigurationError(
                    f"Não foi possível ler {self.agents_file.name}: {exc}"
                ) from exc
            try:
                raw_tasks = yaml.safe_load(self.tasks_file.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ConfigurationError(
                    f"Não foi possível ler {self.tasks_file.name}: {exc}"
                ) from exc

            defaults = dict(raw_agents.get("defaults") or {})

            for agent_id, body in (raw_agents.get("agents") or {}).items():
                if not isinstance(body, dict):
                    errors.append(f"agente {agent_id}: definição não é um mapa")
                    continue
                missing = [f for f in REQUIRED_AGENT_FIELDS if not body.get(f)]
                if missing:
                    errors.append(f"agente {agent_id}: falta {', '.join(missing)}")
                    continue
                tier = str(body.get("llm_tier", "fast"))
                if tier not in VALID_TIERS:
                    errors.append(
                        f"agente {agent_id}: llm_tier inválido ({tier}); "
                        f"usa {sorted(VALID_TIERS)}"
                    )
                    tier = "fast"
                agents[agent_id] = AgentSpec(
                    id=agent_id,
                    role=str(body["role"]),
                    goal=str(body["goal"]),
                    backstory=str(body["backstory"]),
                    llm_tier=tier,
                    tools=[str(t) for t in (body.get("tools") or [])],
                    guards=[str(g) for g in (body.get("guards") or [])],
                )

            for task_id, body in (raw_tasks.get("tasks") or {}).items():
                if not isinstance(body, dict):
                    errors.append(f"tarefa {task_id}: definição não é um mapa")
                    continue
                missing = [f for f in REQUIRED_TASK_FIELDS if not body.get(f)]
                if missing:
                    errors.append(f"tarefa {task_id}: falta {', '.join(missing)}")
                    continue
                agent_id = str(body["agent"])
                if agent_id not in agents:
                    errors.append(
                        f"tarefa {task_id}: agente inexistente ({agent_id})"
                    )
                tasks[task_id] = TaskSpec(
                    id=task_id,
                    description=str(body["description"]).strip(),
                    agent=agent_id,
                    depends_on=[str(d) for d in (body.get("depends_on") or [])],
                    output=str(body.get("output") or ""),
                    gate=str(body.get("gate") or "always"),
                )

            # Integridade das dependências (ciclos e referências órfãs).
            for task in tasks.values():
                for dependency in task.depends_on:
                    if dependency not in tasks:
                        errors.append(
                            f"tarefa {task.id}: dependência inexistente ({dependency})"
                        )
            cycle = self._find_cycle(tasks)
            if cycle:
                errors.append("dependências circulares: " + " → ".join(cycle))

            self._agents = agents
            self._tasks = tasks
            self._defaults = defaults
            self._errors = errors
            self._stamp = stamp

            if errors:
                log.warning("Registo de agentes com %d problema(s).", len(errors))
            else:
                log.info(
                    "Registo de agentes carregado: %d agentes, %d tarefas.",
                    len(agents), len(tasks),
                )

    @staticmethod
    def _find_cycle(tasks: Dict[str, TaskSpec]) -> List[str]:
        """Devolve o primeiro ciclo encontrado em ``depends_on`` (ou lista vazia)."""
        state: Dict[str, int] = {}

        def visit(node: str, path: List[str]) -> List[str]:
            if state.get(node) == 1:
                return path[path.index(node):] + [node] if node in path else [node]
            if state.get(node) == 2:
                return []
            state[node] = 1
            for dependency in tasks.get(node, TaskSpec("", "", "")).depends_on:
                if dependency in tasks:
                    found = visit(dependency, path + [node])
                    if found:
                        return found
            state[node] = 2
            return []

        for task_id in tasks:
            found = visit(task_id, [])
            if found:
                return found
        return []

    # ------------------------------------------------------------------ acesso
    def agents(self) -> List[AgentSpec]:
        with self._lock:
            return list(self._agents.values())

    def tasks(self) -> List[TaskSpec]:
        with self._lock:
            return list(self._tasks.values())

    def agent(self, agent_id: str) -> AgentSpec:
        with self._lock:
            if agent_id not in self._agents:
                raise ConfigurationError(
                    f"Agente desconhecido: {agent_id}",
                    details={"known": sorted(self._agents)},
                )
            return self._agents[agent_id]

    def task(self, task_id: str) -> TaskSpec:
        with self._lock:
            if task_id not in self._tasks:
                raise ConfigurationError(
                    f"Tarefa desconhecida: {task_id}",
                    details={"known": sorted(self._tasks)},
                )
            return self._tasks[task_id]

    def defaults(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._defaults)

    def errors(self) -> List[str]:
        with self._lock:
            return list(self._errors)

    @staticmethod
    def _display_path(path: Path) -> str:
        """Caminho relativo à raiz do repo; absoluto se estiver fora dele."""
        try:
            return str(Path(path).resolve().relative_to(REPO_ROOT))
        except ValueError:
            return str(path)

    def validate(self) -> Dict[str, Any]:
        """Relatório de validação (usado pelo endpoint e pelos testes)."""
        self.reload()
        return {
            "ok": not self.errors(),
            "agents": len(self.agents()),
            "tasks": len(self.tasks()),
            "errors": self.errors(),
            "tiers": sorted({a.llm_tier for a in self.agents()}),
            "gates": sorted({t.gate for t in self.tasks()}),
            "files": {
                "agents": self._display_path(self.agents_file),
                "tasks": self._display_path(self.tasks_file),
            },
        }


# ==========================================================================
# Ponte CrewAI ↔ LLM Router
# ==========================================================================
class CrewBridge:
    """Executa tarefas declaradas no YAML, com ou sem CrewAI instalado."""

    def __init__(self, registry: Optional[AgentRegistry] = None) -> None:
        self.registry = registry or agent_registry
        self._lock = threading.RLock()

    # ------------------------------------------------------- disponibilidade
    def crewai_available(self) -> bool:
        try:
            import crewai  # noqa: F401
        except Exception:
            return False
        return True

    def availability(self) -> Dict[str, Any]:
        return {
            "crewai_installed": self.crewai_available(),
            "native_engine": "llm_router+heuristic",
            "mode": "crewai" if self.crewai_available() else "nativo",
            "providers": [
                name
                for name, data in llm_router.status()["providers"].items()
                if data["available"]
            ],
        }

    # ------------------------------------------------------------- LLM wrapper
    def build_crewai_llm(self, spec: AgentSpec) -> Any:
        """Constrói um ``BaseLLM`` do CrewAI que delega no nosso roteador.

        Isto é essencial: garante que o CrewAI **não** instancia o seu próprio
        cliente LiteLLM (que exigiria chaves pagas), mantendo o orçamento, o
        rate limiting e o escudo de prompt injection num único ponto.
        """
        try:
            from crewai.llm import BaseLLM  # type: ignore
        except Exception as exc:  # pragma: no cover - depende do ambiente
            raise ConfigurationError(
                "CrewAI não está instalado; usa o caminho nativo."
            ) from exc

        bridge = self

        class RouterLLM(BaseLLM):  # type: ignore[misc]
            """Adaptador fino: messages → LLMRequest → llm_router.complete()."""

            def __init__(self) -> None:
                model_name = bridge.model_for_tier(spec.llm_tier)
                try:
                    super().__init__(model=model_name)
                except Exception:
                    # Assinaturas variam entre versões do CrewAI.
                    super().__init__()

            def call(  # type: ignore[override]
                self,
                messages: Any,
                tools: Any = None,
                callbacks: Any = None,
                available_functions: Any = None,
                from_task: Any = None,
                from_agent: Any = None,
                **kwargs: Any,
            ) -> str:
                system_parts: List[str] = [spec.system_prompt()]
                user_parts: List[str] = []
                if isinstance(messages, str):
                    user_parts.append(messages)
                else:
                    for message in messages or []:
                        role = ""
                        content = ""
                        if isinstance(message, dict):
                            role = str(message.get("role", ""))
                            content = str(message.get("content", ""))
                        else:
                            role = str(getattr(message, "role", ""))
                            content = str(getattr(message, "content", ""))
                        if role == "system":
                            system_parts.append(content)
                        else:
                            user_parts.append(content)

                response = llm_router.complete(
                    LLMRequest(
                        prompt="\n\n".join(part for part in user_parts if part),
                        system="\n\n".join(part for part in system_parts if part),
                        task=spec.llm_tier,
                        agent_id=spec.id,
                    )
                )
                return response.text

            def get_context_window_size(self) -> int:  # type: ignore[override]
                return 8192

        return RouterLLM()

    @staticmethod
    def _tools_crewai(spec: AgentSpec) -> List[Any]:
        """Objectos de ferramenta para o CrewAI, apenas os realmente prontos.

        Ferramentas ``degradada`` também entram: funcionam em modo reduzido e
        o agente deve saber disso, em vez de não ter nada. As ``em_falta``
        ficam de fora — não há o que entregar.
        """
        try:
            from ..finance.cfo_tools import CFO_TOOLS, construir_tools_crewai
            from .tool_registry import TOOLS
        except Exception:  # noqa: BLE001
            return []

        disponiveis = [
            nome for nome in spec.tools
            if nome in TOOLS and TOOLS[nome].disponivel()
        ]
        if not disponiveis:
            return []
        # Os tools do CFO têm adaptador CrewAI próprio (com nome e descrição);
        # os outros objectos (kill_switch, llm_router...) passam-se tal como
        # são. O CrewAI usa-os se tiverem a interface que espera.
        prontos: List[Any] = []
        if any(nome in CFO_TOOLS for nome in disponiveis):
            try:
                prontos.extend(construir_tools_crewai())
            except Exception as exc:  # noqa: BLE001
                log.warning("Tools do CFO não construídas: %s", exc)
        for nome in disponiveis:
            if nome in CFO_TOOLS:
                continue
            funcao = TOOLS[nome].funcao()
            if funcao is not None:
                prontos.append(funcao)
        return prontos

    def model_for_tier(self, tier: str) -> str:
        """Modelo concreto para o tier, priorizando provedores configurados."""
        from ..config.settings import settings

        if tier == "long_context" and settings.gemini_api_key:
            return f"gemini/{settings.gemini_model}"
        if tier == "fast" and settings.groq_api_key:
            return f"groq/{settings.groq_model_fast}"
        if tier == "reasoning" and settings.groq_api_key:
            return f"groq/{settings.groq_model_reasoning}"
        if settings.ollama_enabled:
            return f"ollama/{settings.ollama_model}"
        return "local/heuristic-v1"

    # ----------------------------------------------------------------- execução
    def _native_prompt(self, task: TaskSpec, spec: AgentSpec, inputs: Dict[str, Any]) -> str:
        payload = json.dumps(inputs, ensure_ascii=False, indent=2, default=str)
        output_hint = f"\n\nFormato esperado da resposta:\n{task.output}" if task.output else ""
        return (
            f"Tarefa «{task.id}»: {task.description}\n\n"
            f"Entradas:\n{payload}{output_hint}\n\n"
            "Responde de forma concreta e accionável, com números e fontes "
            "quando existirem."
        )

    def run_task(
        self,
        task_id: str,
        inputs: Optional[Dict[str, Any]] = None,
        *,
        require_json: bool = False,
    ) -> Dict[str, Any]:
        """Executa uma tarefa declarada em ``tasks.yaml``.

        Com CrewAI instalado, delega o raciocínio ao agente do YAML; sem ele,
        usa o roteador directamente com o mesmo prompt de sistema. Nos dois
        casos devolve o mesmo contrato auditável.
        """
        task = self.registry.task(task_id)
        spec = self.registry.agent(task.agent)
        payload = dict(inputs or {})

        request = LLMRequest(
            prompt=self._native_prompt(task, spec, payload),
            system=spec.system_prompt(),
            task=spec.llm_tier,
            max_tokens=1400,
            temperature=0.4,
            json_mode=require_json,
            agent_id=spec.id,
            context={"task": task.id, "gate": task.gate, **payload},
        )

        engine = "nativo"
        with self._lock:
            if self.crewai_available():
                try:  # pragma: no cover - requer crewai instalado
                    from crewai import Agent, Crew, Task  # type: ignore

                    crew_agent = Agent(
                        role=spec.role,
                        goal=spec.goal.strip(),
                        backstory=spec.backstory.strip(),
                        llm=self.build_crewai_llm(spec),
                        verbose=False,
                        allow_delegation=False,
                        # As ferramentas que o YAML declara e o registo
                        # confirma. Sem isto o agente do CrewAI recebia um
                        # prompt a dizer «podes usar registrar_saques» e não
                        # tinha função nenhuma para chamar — o mesmo defeito
                        # de antes, só numa camada mais funda.
                        tools=self._tools_crewai(spec),
                    )
                    crew_task = Task(
                        description=self._native_prompt(task, spec, payload),
                        expected_output=task.output or "resposta em texto",
                        agent=crew_agent,
                    )
                    output = Crew(agents=[crew_agent], tasks=[crew_task]).kickoff()
                    text = str(getattr(output, "raw", output))
                    engine = "crewai"
                    return {
                        "task": task.to_dict(),
                        "agent": spec.id,
                        "engine": engine,
                        "text": text,
                        "provider": "crewai",
                        "is_llm": True,
                        "gate": task.gate,
                    }
                except Exception as exc:
                    log.warning(
                        "CrewAI falhou na tarefa %s (%s); a usar o caminho nativo.",
                        task.id, exc,
                    )

            response = llm_router.complete(request)

        return {
            "task": task.to_dict(),
            "agent": spec.id,
            "engine": engine,
            "text": response.text,
            "provider": response.provider,
            "model": response.model,
            "is_llm": response.is_llm,
            "latency_ms": response.latency_ms,
            "usage": response.usage.to_dict(),
            "fallback_chain": list(response.fallback_chain),
            "gate": task.gate,
            "generated_at": response.created_at,
        }

    def run_pipeline(
        self,
        task_ids: Optional[List[str]] = None,
        inputs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Corre uma sequência de tarefas respeitando ``depends_on``.

        Ordem topológica estável; cada tarefa recebe como contexto o texto
        produzido pelas suas dependências.
        """
        wanted = list(task_ids or [t.id for t in self.registry.tasks()])
        all_tasks = {t.id: t for t in self.registry.tasks()}
        executed: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        visiting: set = set()
        done: set = set()

        def resolve(task_id: str) -> None:
            # Cada tarefa entra no pipeline exactamente uma vez, mesmo que seja
            # dependência de várias outras (diamante) — sem duplicar chamadas
            # ao modelo.
            if task_id in done or task_id in visiting:
                return
            if task_id not in all_tasks:
                raise ConfigurationError(f"Tarefa inexistente no pipeline: {task_id}")
            visiting.add(task_id)
            for dependency in all_tasks[task_id].depends_on:
                resolve(dependency)
            visiting.discard(task_id)
            done.add(task_id)
            order.append(task_id)

        for task_id in wanted:
            resolve(task_id)

        results: List[Dict[str, Any]] = []
        for task_id in order:
            task = all_tasks[task_id]
            context = dict(inputs or {})
            context["dependencias"] = {
                dep: (executed.get(dep, {}).get("text", "")[:600])
                for dep in task.depends_on
            }
            try:
                result = self.run_task(task_id, context)
                executed[task_id] = result
                results.append(result)
            except Exception as exc:  # uma tarefa falhar não deve matar o pipeline
                log.warning("Tarefa %s falhou no pipeline: %s", task_id, exc)
                results.append({
                    "task": task.to_dict(),
                    "agent": task.agent,
                    "engine": "nativo",
                    "text": "",
                    "provider": "error",
                    "is_llm": False,
                    "error": str(exc),
                    "gate": task.gate,
                })

        return {
            "tasks_requested": len(wanted),
            "tasks_executed": len(results),
            "order": order,
            "used_llm": any(r.get("is_llm") for r in results),
            "results": results,
        }


# Instâncias partilhadas (importadas por main.py e pelos testes).
agent_registry = AgentRegistry()
crew_bridge = CrewBridge(agent_registry)
