"""RH de IA: avalia ROI de cada agente, optimiza prompts e propõe substituições.

Cada agente do CrewAI tem uma ficha em ``squad_metrics.json`` (chave
``agents``) com:
  * chamadas, tokens, custo, sucessos e reprovações do Juiz Eval;
  * latência média e taxa de sucesso;
  * o *system prompt* actual (editável pelo RH) e o histórico de versões.

O RH actua quando:
  * ``success_rate < HR_MIN_SUCCESS_RATE`` (0.60) -> reescreve o prompt com as
    lições do cemitério e as causas de reprovação mais frequentes;
  * custo por sucesso > limite definido -> reduz agressividade do modelo
    (força tarefas mais curtas) e propõe troca para o modelo rápido;
  * reprovações repetidas do Juiz -> propõe **substituição de código** por
    repositório do GitHub (pesquisa real via API, com auditoria de licença).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.clock import utcnow_iso
from ..core.llm_router import LLMRequest, llm_router
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.security_guard import assert_commercial_licenses
from ..core.shared_memory import shared_memory
from ..core.vault_store import SQUAD_METRICS, vault

log = get_logger("governance.hr")

BASE_PROMPTS: Dict[str, str] = {
    "scout_hunter": (
        "És o Agente Caçador da Automaton Holding. Encontras brechas lucrativas "
        "em GitHub Trending, Reddit, TikTok, YouTube e marketplaces. "
        "Cita sempre a fonte e o sinal de demanda. Nunca propões nada que conste "
        "no cemitério de negócios."
    ),
    "demand_validator": (
        "És o Agente Validador. Antes de qualquer produção, desenhas um Smoke "
        "Test gratuito com meta mínima de cliques e conversão. Se a meta falhar, "
        "a produção é bloqueada. Justifica sempre com números."
    ),
    "video_factory": (
        "És o Produtor do Esquadrão de Vídeos Curtos. Escreves guiões de 9:16 "
        "com gancho nos primeiros 3 segundos, áudio limpo e CTA claro. "
        "Só usas vozes e footage com licença comercial permitida."
    ),
    "infoproducts_affiliate": (
        "És o Produtor de Infoprodutos e SEO. Escreves e-books, artigos de "
        "review e funis de afiliados optimizados para conversão e AdSense. "
        "Nunca prometes resultados irreais e declaras sempre os links pagos."
    ),
    "micro_saas_dev": (
        "És o Engenheiro de Micro-SaaS. Projectas ferramentas web mínimas, "
        "seguras e monetizáveis com código completo, testado e licenciado."
    ),
    "eval_judge": (
        "És o Juiz Interno. Avalias entregas de 0 a 100 em retenção, clareza, "
        "bugs e SEO. Abaixo de 85 reprovas com motivos accionáveis."
    ),
    "cfo_treasury": (
        "És o CFO. Aplicas 5% de imposto e a divisão por fases. Nunca autorizas "
        "despesa fora do fundo de reinvestimento."
    ),
}

MIN_ATTEMPTS_BEFORE_ACTION = 3


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class AgentReview:
    agent_id: str
    success_rate: float
    cost_per_success_usd: float
    avg_score: float
    attempts: int
    decision: str  # healthy | rewrite_prompt | replace_code | retire
    reasons: List[str]
    new_prompt: Optional[str] = None
    candidate_repositories: Optional[List[Dict[str, Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "success_rate": round(self.success_rate, 4),
            "cost_per_success_usd": round(self.cost_per_success_usd, 6),
            "avg_score": round(self.avg_score, 2),
            "attempts": self.attempts,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "new_prompt": self.new_prompt,
            "candidate_repositories": self.candidate_repositories or [],
        }


class HRDepartment:
    """Avalia e reescreve os agentes da holding."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ fichas
    def _data(self) -> Dict[str, Any]:
        data = vault.read(SQUAD_METRICS)
        if not isinstance(data.get("agents"), dict):
            data["agents"] = {}
        for agent_id, prompt in BASE_PROMPTS.items():
            entry = data["agents"].get(agent_id)
            if entry is None:
                data["agents"][agent_id] = self._blank_agent(agent_id, prompt)
        return data

    @staticmethod
    def _blank_agent(agent_id: str, prompt: str) -> Dict[str, Any]:
        now = utcnow_iso()
        return {
            "id": agent_id,
            "status": "active",
            "system_prompt": prompt,
            "prompt_version": 1,
            "prompt_history": [{"version": 1, "prompt": prompt, "at": now, "by": "bootstrap"}],
            "calls": 0,
            "tokens": 0,
            "cost_usd": 0.0,
            "successes": 0,
            "failures": 0,
            "judge_scores": [],
            "failure_reasons": [],
            "rewrites": 0,
            "created_at": now,
            "updated_at": now,
            "reviews": [],
        }

    def agents(self) -> List[Dict[str, Any]]:
        data = self._data()
        return list(data["agents"].values())

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        agents = self._data()["agents"]
        if agent_id not in agents:
            agents[agent_id] = self._blank_agent(
                agent_id, BASE_PROMPTS.get(agent_id, "Agente operacional da holding.")
            )
        return agents[agent_id]

    # ---------------------------------------------------------------- registo
    def record_execution(
        self,
        agent_id: str,
        *,
        success: bool,
        tokens: int = 0,
        cost_usd: float = 0.0,
        judge_score: Optional[float] = None,
        failure_reason: str = "",
    ) -> Dict[str, Any]:
        """Actualiza a ficha de desempenho de um agente (após cada tarefa)."""
        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            data.setdefault("agents", {})
            entry = data["agents"].get(agent_id) or self._blank_agent(
                agent_id, BASE_PROMPTS.get(agent_id, "Agente operacional da holding.")
            )
            entry["calls"] = int(entry.get("calls", 0)) + 1
            entry["tokens"] = int(entry.get("tokens", 0)) + max(0, int(tokens))
            entry["cost_usd"] = round(_num(entry.get("cost_usd", 0.0)) + max(0.0, _num(cost_usd)), 6)
            if success:
                entry["successes"] = int(entry.get("successes", 0)) + 1
            else:
                entry["failures"] = int(entry.get("failures", 0)) + 1
                if failure_reason:
                    reasons = entry.setdefault("failure_reasons", [])
                    reasons.append(failure_reason[:240])
                    entry["failure_reasons"] = reasons[-50:]
            if judge_score is not None:
                scores = entry.setdefault("judge_scores", [])
                scores.append(round(float(judge_score), 2))
                entry["judge_scores"] = scores[-100:]
            entry["updated_at"] = utcnow_iso()
            data["agents"][agent_id] = entry
            result = dict(entry)
        return result

    # ---------------------------------------------------------------- avaliação
    def review_agent(self, agent_id: str, *, auto_rewrite: bool = False) -> AgentReview:
        """Diagnostica um agente e (opcionalmente) aplica a reescrita do prompt."""
        entry = self.get_agent(agent_id)
        calls = int(entry.get("calls", 0))
        successes = int(entry.get("successes", 0))
        failures = int(entry.get("failures", 0))
        cost = _num(entry.get("cost_usd", 0.0))
        scores = [float(s) for s in entry.get("judge_scores", []) if isinstance(s, (int, float))]

        attempts = successes + failures
        success_rate = successes / attempts if attempts else 1.0
        cost_per_success = cost / successes if successes else 0.0
        avg_score = sum(scores) / len(scores) if scores else 0.0

        reasons: List[str] = []
        decision = "healthy"
        new_prompt: Optional[str] = None
        candidates: Optional[List[Dict[str, Any]]] = None

        if attempts >= MIN_ATTEMPTS_BEFORE_ACTION:
            if success_rate < settings.hr_min_success_rate:
                decision = "rewrite_prompt"
                reasons.append(
                    f"taxa de sucesso {success_rate:.0%} < {settings.hr_min_success_rate:.0%}"
                )
            if scores and avg_score < settings.eval_min_score:
                decision = "rewrite_prompt" if decision == "healthy" else decision
                reasons.append(
                    f"nota média do Juiz {avg_score:.1f} < {settings.eval_min_score:.0f}"
                )
            if (
                successes
                and cost_per_success > settings.hr_max_cost_per_success_usd
                and settings.hr_max_cost_per_success_usd > 0
            ):
                reasons.append(
                    f"custo por sucesso ${cost_per_success:.4f} acima do limite"
                )
            if failures >= 6 and successes == 0:
                decision = "replace_code"
                reasons.append("6+ falhas consecutivas sem qualquer sucesso")
            if scores and len(scores) >= 5 and avg_score < settings.eval_min_score * 0.6:
                decision = "replace_code"
                reasons.append("reprovação crónica do Juiz (< 60% do limiar)")

        if decision in {"rewrite_prompt", "replace_code"}:
            new_prompt = self._draft_prompt(entry, reasons)
            if decision == "replace_code":
                candidates = self._search_github_candidates(agent_id)
                if not candidates:
                    decision = "rewrite_prompt"
                    reasons.append("sem repositório candidato elegível; prompt reescrito")
            if auto_rewrite and new_prompt:
                self._apply_prompt(agent_id, new_prompt, reasons)
                reasons.append("prompt aplicado automaticamente")

        review = AgentReview(
            agent_id=agent_id,
            success_rate=success_rate,
            cost_per_success_usd=cost_per_success,
            avg_score=avg_score,
            attempts=attempts,
            decision=decision,
            reasons=reasons or ["dentro dos parâmetros"],
            new_prompt=new_prompt,
            candidate_repositories=candidates,
        )
        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            record = data.setdefault("agents", {}).get(agent_id)
            if record is not None:
                reviews = record.setdefault("reviews", [])
                reviews.append(review.to_dict())
                record["reviews"] = reviews[-20:]
                record["last_review"] = review.decision
                record["updated_at"] = utcnow_iso()
        if decision != "healthy":
            event_bus.publish(
                "agent_review",
                agent="hr_department",
                message=(
                    f"RH: {agent_id} -> {decision}. {'; '.join(review.reasons)[:200]}"
                ),
                level="warning",
                payload=review.to_dict(),
            )
        return review

    def review_all(self, *, auto_rewrite: bool = False) -> Dict[str, Any]:
        reviews = [
            self.review_agent(agent_id, auto_rewrite=auto_rewrite)
            for agent_id in self._data()["agents"].keys()
        ]
        return {
            "reviews": [review.to_dict() for review in reviews],
            "actions_needed": [
                review.agent_id for review in reviews if review.decision != "healthy"
            ],
            "generated_at": utcnow_iso(),
        }

    # ------------------------------------------------------------- reescrita
    def _draft_prompt(self, entry: Dict[str, Any], reasons: List[str]) -> str:
        """Gera um prompt melhorado (LLM quando disponível; regras caso contrário)."""
        current = str(entry.get("system_prompt", "")).strip()
        failures = list(entry.get("failure_reasons", []) or [])[-8:]
        lessons = shared_memory.lessons_for(str(entry.get("id", "")), limit=6)
        try:
            request = LLMRequest(
                prompt=(
                    "Reescreve o system prompt de um agente de IA corporativo.\n"
                    f"PROMPT ACTUAL:\n{current}\n\n"
                    f"PROBLEMAS DETECTADOS: {'; '.join(reasons)}\n"
                    f"CAUSAS DE FALHA RECENTES: {'; '.join(str(f) for f in failures) or 'n/d'}\n"
                    f"LIÇÕES DO CEMITÉRIO: {'; '.join(lessons) or 'n/d'}\n\n"
                    "Requisitos: português de Moçambique, instruções accionáveis, "
                    "exigir citação de fontes, proibir produção sem validação de "
                    "demanda, exigir licenças comerciais permitidas. "
                    "Responde só com o novo prompt (máx. 220 palavras)."
                ),
                system="És o director de RH de IA. Escreves prompts precisos e auditáveis.",
                task="reasoning",
                max_tokens=700,
                temperature=0.3,
                agent_id="hr_department",
                context={"squad": "governance"},
            )
            response = llm_router.complete(request)
            drafted = (response.text or "").strip()
            if response.is_llm and len(drafted) > 120 and "[MOTOR LOCAL" not in drafted:
                return drafted[:4000]
        except Exception as exc:
            log.warning("Reescrita via LLM falhou (%s); a usar regras.", exc)

        # Fallback determinístico e auditável.
        additions = [
            "Antes de agir, consulta SEMPRE a memória corporativa (post-mortem) e "
            "não repitas nichos que já falharam.",
            "Responde em JSON quando o formato for pedido e cita a fonte de cada "
            "afirmação factual.",
            "Nunca proposes produção sem aprovação do Agente Validador e nota "
            f">= {settings.eval_min_score:.0f} no Juiz Eval.",
            "Usa apenas ferramentas e modelos com licença MIT/Apache-2.0/BSD/LGPL; "
            "proibido conteúdo CC-BY-NC.",
            "Mantém o custo em $0.00: escolhe o caminho que não gasta tokens extra.",
        ]
        if reasons:
            additions.append(
                "Corrige especificamente: " + "; ".join(reasons)[:300] + "."
            )
        if failures:
            additions.append(
                "Evita os erros recentes: "
                + "; ".join(str(f)[:80] for f in list(failures)[-3:])
                + "."
            )
        return (current + "\n\n[REVISÃO RH " + utcnow_iso()[:10] + "]\n" + "\n".join(
            f"- {line}" for line in additions
        ))[:4000]

    def _apply_prompt(self, agent_id: str, new_prompt: str, reasons: List[str]) -> None:
        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            entry = data.setdefault("agents", {}).get(agent_id)
            if entry is None:
                return
            version = int(entry.get("prompt_version", 1)) + 1
            entry["system_prompt"] = new_prompt
            entry["prompt_version"] = version
            entry["rewrites"] = int(entry.get("rewrites", 0)) + 1
            history = entry.setdefault("prompt_history", [])
            history.append(
                {
                    "version": version,
                    "prompt": new_prompt,
                    "at": utcnow_iso(),
                    "by": "hr_department",
                    "reasons": reasons,
                }
            )
            entry["prompt_history"] = history[-15:]
            entry["updated_at"] = utcnow_iso()
        log.warning("Prompt de %s reescrito (v%d).", agent_id, version)
        event_bus.publish(
            "agent_prompt_rewritten",
            agent="hr_department",
            message=f"Prompt de {agent_id} actualizado para a versão {version}.",
            payload={"agent_id": agent_id, "version": version},
        )

    def rollback_prompt(self, agent_id: str, version: int) -> Dict[str, Any]:
        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            entry = data.setdefault("agents", {}).get(agent_id)
            if entry is None:
                raise ValueError(f"Agente {agent_id} não existe.")
            for item in entry.get("prompt_history", []):
                if int(item.get("version", -1)) == int(version):
                    entry["system_prompt"] = item.get("prompt", "")
                    entry["prompt_version"] = int(version)
                    entry["updated_at"] = utcnow_iso()
                    return dict(entry)
        raise ValueError(f"Versão {version} não encontrada para {agent_id}.")

    # ------------------------------------------------------------ substituição
    def _search_github_candidates(self, agent_id: str) -> List[Dict[str, Any]]:
        """Procura repositórios alternativos com licença comercial permitida."""
        if not settings.github_token:
            log.info("GITHUB_TOKEN ausente: pesquisa de substitutos desactivada.")
            return []
        queries = {
            "scout_hunter": "web research agent crawler",
            "video_factory": "short video generator automation",
            "infoproducts_affiliate": "seo content generator affiliate",
            "micro_saas_dev": "ai software engineer agent",
            "eval_judge": "llm output evaluation judge",
        }
        query = queries.get(agent_id, f"{agent_id} agent")
        url = (
            "https://api.github.com/search/repositories?q="
            + urllib.parse.quote(query)
            + "&sort=stars&order=desc&per_page=8"
        )
        request = urllib.request.Request(url)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", settings.scout_user_agent)
        request.add_header("Authorization", f"Bearer {settings.github_token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            log.warning("Pesquisa GitHub falhou: %s", exc)
            return []

        allowed_licenses = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "lgpl-3.0", "mpl-2.0", "isc"}
        candidates: List[Dict[str, Any]] = []
        for repo in payload.get("items", []) or []:
            license_info = repo.get("license") or {}
            spdx = str(license_info.get("spdx_id", "NOASSERTION"))
            if spdx.lower() not in allowed_licenses:
                continue
            if assert_commercial_licenses([repo.get("full_name", "")])["violations"]:
                continue
            candidates.append(
                {
                    "full_name": repo.get("full_name"),
                    "html_url": repo.get("html_url"),
                    "stars": int(repo.get("stargazers_count", 0)),
                    "license": spdx,
                    "description": (repo.get("description") or "")[:200],
                    "last_push": repo.get("pushed_at"),
                }
            )
        candidates.sort(key=lambda c: c["stars"], reverse=True)
        return candidates[:3]

    # ---------------------------------------------------------------- relatório
    def report(self) -> Dict[str, Any]:
        data = self._data()
        agents = []
        for agent_id, entry in data["agents"].items():
            calls = int(entry.get("calls", 0))
            successes = int(entry.get("successes", 0))
            scores = [float(s) for s in entry.get("judge_scores", []) if isinstance(s, (int, float))]
            agents.append(
                {
                    "id": agent_id,
                    "calls": calls,
                    "success_rate": round(successes / calls, 4) if calls else None,
                    "avg_judge_score": round(sum(scores) / len(scores), 2) if scores else None,
                    "cost_usd": round(_num(entry.get("cost_usd", 0.0)), 6),
                    "tokens": int(entry.get("tokens", 0)),
                    "prompt_version": int(entry.get("prompt_version", 1)),
                    "rewrites": int(entry.get("rewrites", 0)),
                    "last_review": entry.get("last_review", "nunca"),
                    "status": entry.get("status", "active"),
                }
            )
        agents.sort(key=lambda a: str(a["id"]))
        return {
            "agents": agents,
            "summary": {
                "total_agents": len(agents),
                "needing_attention": len(
                    [a for a in agents if a.get("last_review") not in (None, "healthy", "nunca")]
                ),
                "total_tokens": sum(a["tokens"] for a in agents),
                "total_cost_usd": round(sum(a["cost_usd"] for a in agents), 6),
            },
            "thresholds": {
                "min_success_rate": settings.hr_min_success_rate,
                "min_judge_score": settings.eval_min_score,
            },
            "generated_at": utcnow_iso(),
        }


hr_department = HRDepartment()

__all__ = ["HRDepartment", "hr_department", "AgentReview", "BASE_PROMPTS"]
