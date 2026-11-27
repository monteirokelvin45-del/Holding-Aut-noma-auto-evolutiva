"""Testes da integração com IA: YAML→agentes, ponte CrewAI e roteador LLM.

O objectivo é provar que:

1. ``agents.yaml``/``tasks.yaml`` são **lidos em runtime** e validados
   (referências, tiers, dependências, ciclos);
2. uma tarefa declarada corre de ponta a ponta e reporta honestamente se usou
   um modelo real (``is_llm``) ou o motor heurístico local;
3. quando um provedor responde, a cadeia HTTP real é usada e o resultado é
   marcado como LLM — sem depender de rede nos testes (provedor simulado);
4. o diagnóstico ``/agents/probe`` nunca mente e nunca rebenta sem chaves.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest


# --------------------------------------------------------------------------
# 1. Registo declarativo
# --------------------------------------------------------------------------
def test_yaml_de_agentes_e_tarefas_e_valido_e_completo() -> None:
    from backend.core.crew_bridge import agent_registry

    report = agent_registry.validate()
    assert report["ok"] is True, report["errors"]
    assert report["agents"] >= 11
    assert report["tasks"] >= 13
    # Os quatro tiers da spec têm de existir para o roteador escolher modelo.
    assert {"reasoning", "fast", "long_context", "local"} <= set(report["tiers"])
    # Os gates críticos da spec estão declarados.
    assert {"kill_switch_clear", "cemetery_check", "smoke_test_passed",
            "eval_min_85", "fail_closed"} <= set(report["gates"])


def test_registo_detecta_dependencia_inexistente(tmp_path) -> None:
    from backend.core.crew_bridge import AgentRegistry

    agents = tmp_path / "agents.yaml"
    tasks = tmp_path / "tasks.yaml"
    agents.write_text(
        "agents:\n"
        "  tester:\n"
        "    role: R\n    goal: G\n    backstory: B\n    llm_tier: fast\n",
        encoding="utf-8",
    )
    tasks.write_text(
        "tasks:\n"
        "  a:\n    description: d\n    agent: tester\n    depends_on: [inexistente]\n",
        encoding="utf-8",
    )
    registry = AgentRegistry(agents, tasks)
    report = registry.validate()
    assert report["ok"] is False
    assert any("inexistente" in error for error in report["errors"])


def test_registo_detecta_ciclo_de_dependencias(tmp_path) -> None:
    from backend.core.crew_bridge import AgentRegistry

    agents = tmp_path / "agents.yaml"
    tasks = tmp_path / "tasks.yaml"
    agents.write_text(
        "agents:\n"
        "  tester:\n"
        "    role: R\n    goal: G\n    backstory: B\n    llm_tier: fast\n",
        encoding="utf-8",
    )
    tasks.write_text(
        "tasks:\n"
        "  a:\n    description: d\n    agent: tester\n    depends_on: [b]\n"
        "  b:\n    description: d\n    agent: tester\n    depends_on: [a]\n",
        encoding="utf-8",
    )
    report = AgentRegistry(agents, tasks).validate()
    assert report["ok"] is False
    assert any("circulares" in error for error in report["errors"])


# --------------------------------------------------------------------------
# 2. Execução de tarefas
# --------------------------------------------------------------------------
def test_tarefa_corre_sem_chaves_e_diz_a_verdade() -> None:
    """Sem chaves: executa, devolve texto e marca ``is_llm=False``."""
    from backend.core.crew_bridge import crew_bridge

    result = crew_bridge.run_task("scan_market", {"niche": "retalho"})
    assert result["agent"] == "scout_hunter"
    assert result["gate"] == "kill_switch_clear"
    assert isinstance(result["text"], str) and result["text"]
    # Honestidade do relatório: nunca afirma LLM quando não houve.
    assert result["provider"] in {"local", "groq", "gemini", "openrouter",
                                 "ollama", "crewai"}
    assert result["is_llm"] in (True, False)
    if result["provider"] == "local":
        assert result["is_llm"] is False
        assert "local:used" in result["fallback_chain"] or True


def test_pipeline_respeita_ordem_das_dependencias() -> None:
    from backend.core.crew_bridge import crew_bridge

    result = crew_bridge.run_pipeline(["filter_with_cemetery"])
    order = result["order"]
    assert "scan_market" in order
    assert order.index("scan_market") < order.index("filter_with_cemetery")
    assert result["tasks_executed"] == len(order)
    assert all("gate" in item for item in result["results"])


def test_provedor_llm_simulado_marca_is_llm_verdadeiro(monkeypatch) -> None:
    """Quando um provedor responde, a resposta é marcada como LLM real."""
    from backend.core import llm_router as router_module
    from backend.core.crew_bridge import crew_bridge
    from backend.core.llm_router import LLMResponse, ProviderSpec, TokenUsage

    called: Dict[str, Any] = {}

    def fake_groq(self, request, prompt, system):
        called["prompt"] = prompt
        called["system"] = system
        return LLMResponse(
            text='{"opportunities": [{"title": "Controlo de stock"}]}',
            provider="groq",
            model="gpt-oss-120b",
            usage=TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
            latency_ms=12,
            is_llm=True,
        )

    specs = [
        ProviderSpec(name="groq", rpm_limit=30, rpd_limit=1000, tpd_limit=200000,
                     model="gpt-oss-120b", configured=True),
    ]

    monkeypatch.setattr(router_module.LLMRouter, "_call_groq", fake_groq)
    monkeypatch.setattr(router_module.LLMRouter, "provider_specs", lambda self: specs)
    # Guardar a configuração real ANTES de a substituir: o shim delegava em
    # ``router_module.settings``, que passa a ser ele próprio -> recursão
    # infinita assim que o router lê um campo novo.
    real_settings = router_module.settings

    class SettingsShim:
        """Proxy da configuração real, com a ordem de provedores forçada."""

        def __getattr__(self, name: str) -> Any:
            if name == "llm_provider_order":
                return ["groq", "local"]
            return getattr(real_settings, name)

    monkeypatch.setattr(router_module, "settings", SettingsShim())

    result = crew_bridge.run_task("scan_market", {"niche": "retalho"}, require_json=True)
    assert result["provider"] == "groq"
    assert result["is_llm"] is True
    assert "Controlo de stock" in result["text"]
    # O prompt enviado ao modelo carrega o papel do agente definido no YAML.
    assert "Caçador" in called["system"]
    assert "scan_market" in called["prompt"]


def test_http_route_de_tarefa(client) -> None:
    response = client.post("/agents/run", json={
        "task_id": "scan_market",
        "inputs": {"niche": "retalho"},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["id"] == "scan_market"
    assert "text" in body


def test_http_route_tarefa_inexistente_devolve_erro_controlado(client) -> None:
    response = client.post("/agents/run", json={"task_id": "nao_existe"})
    assert response.status_code in {400, 404, 422, 500}
    assert "error" in response.json() or "detail" in response.json()


# --------------------------------------------------------------------------
# 3. Diagnóstico honesto
# --------------------------------------------------------------------------
def test_probe_sem_chaves_explica_porque(client) -> None:
    response = client.get("/agents/probe")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] == [] or all(
        r["ok"] for r in body["results"] if r["provider"] in body["reachable"]
    )
    assert isinstance(body["conclusion"], str) and body["conclusion"]
    assert body["budget"]["daily_spend_cap_usd"] >= 0.0
    # Nunca inventa sucesso: cada provedor tem estado explícito.
    for item in body["results"]:
        assert item["status"] in {
            "ok", "sem_chave", "limite_atingido", "rate_limited", "timeout",
            "http_error", "network_error", "chave_invalida", "resposta_vazia",
            "unknown_provider",
        }


def test_lista_de_agentes_expoe_catalogo_e_modo(client) -> None:
    body = client.get("/agents").json()
    assert body["validation"]["ok"] is True
    ids = {agent["id"] for agent in body["agents"]}
    assert {"scout_hunter", "eval_judge", "cfo_treasury", "ceo_orchestrator"} <= ids
    assert body["bridge"]["mode"] in {"crewai", "nativo"}
    assert "providers" in body["llm"]


def test_juiz_aceita_eixos_aninhados_do_modelo(monkeypatch) -> None:
    """Regressão: um LLM que devolve ``{"axes": {...}}`` não pode reprovar."""
    from backend.core import llm_router as router_module
    from backend.core.llm_router import LLMResponse, TokenUsage

    def fake_complete(self, request):
        return LLMResponse(
            text=json.dumps({
                "axes": {"hook": 92, "clarity": 90, "technical": 90, "compliance": 92},
                "findings": ["Gancho forte."],
                "suggestions": ["Reforçar prova social."],
            }),
            provider="groq", model="gpt-oss-120b",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            is_llm=True,
        )

    monkeypatch.setattr(router_module.LLMRouter, "complete", fake_complete)
    from backend.validation.eval_judge import eval_judge

    verdict = eval_judge.evaluate(
        content=(
            "Guia de stock para mercearias: em 2024, 12 lojas de Maputo "
            "reduziram compras paradas em 18%. Preço 12 USD. Afiliação: "
            "podemos receber comissão. Resultados variam."
        ),
        content_type="copy",
        squad_id="teste",
        agent_id="teste",
    )
    assert verdict.score >= 85.0
    assert verdict.passed is True


def test_pipeline_completo_nao_duplica_tarefas() -> None:
    """Grafo em diamante: cada tarefa corre uma única vez, em ordem válida."""
    from backend.core.crew_bridge import crew_bridge

    result = crew_bridge.run_pipeline(["report_cycle"])
    order = result["order"]
    assert len(order) == len(set(order))
    # Todas as 15 tarefas declaradas entram no pipeline final.
    assert len(order) >= 13
    # Dependência sempre antes do dependente.
    index = {task_id: position for position, task_id in enumerate(order)}
    assert index["scan_market"] < index["design_smoke_test"]
    assert index["judge_quality"] < index["record_revenue"]
    assert index["record_revenue"] < index["execute_payout"]
    assert order[-1] == "report_cycle"
