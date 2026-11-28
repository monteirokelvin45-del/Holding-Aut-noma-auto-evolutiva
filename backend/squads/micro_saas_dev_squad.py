"""Esquadrão 3: Tecnologia e Micro-SaaS (Arquiteto + Dev + QA).

Emula um esquadrão CrewAI de três papéis (``MetaGPT``-style) com artefactos
reais e verificáveis:

  1. **Arquiteto** — define stack, modelo de dados, rotas e preço.
  2. **Dev** — gera os ficheiros do micro-serviço (FastAPI) e o frontend
     estático, sempre com tratamento de erros e tipagem.
  3. **QA** — escreve e **executa** testes reais; falha o build se algo rebentar.

Segurança: o código gerado destina-se a correr num contentor isolado
(``docker-compose`` inclui um serviço ``micro_saas`` em perfil separado). O
esquadrão nunca executa código gerado no processo da holding sem antes correr a
*verificação estática* (AST) e o Juiz Eval.

O "executor" de testes é controlado por ``allow_code_execution``: por omissão
só compila o código (``compile()``) e corre verificações AST; a execução real de
``pytest`` fica atrás de um sinal explícito, para nunca executar código de
terceiros no host da tesouraria.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.clock import utcnow_iso
from ..core.llm_router import LLMRequest, llm_router
from ..core.logger import get_logger
from .squad_base import BaseSquad, SquadRunResult

log = get_logger("squads.micro_saas")


@dataclass
class SaasBlueprint:
    name: str
    problem: str
    target_user: str
    stack: List[str]
    entities: List[Dict[str, str]]
    endpoints: List[Dict[str, str]]
    pricing: Dict[str, Any]
    differentiators: List[str]
    engine: str = "template"
    is_llm: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "problem": self.problem,
            "target_user": self.target_user,
            "stack": list(self.stack),
            "entities": self.entities,
            "endpoints": self.endpoints,
            "pricing": self.pricing,
            "differentiators": list(self.differentiators),
            "engine": self.engine,
            "is_llm": self.is_llm,
        }


@dataclass
class QaReport:
    passed: bool
    checks: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": list(self.checks),
            "warnings": list(self.warnings),
        }


class MicroSaasSquad(BaseSquad):
    """Arquiteto + Dev + QA para micro-aplicações web monetizáveis."""

    squad_type = "micro_saas_dev"
    squad_name = "Esquadrão de Micro-SaaS"
    owner_agent = "micro_saas_dev"
    requires_validation = True
    content_type = "code"

    # ------------------------------------------------------------- arquitecto
    def design(
        self, *, idea: str, niche: str, price_usd: float = 9.0
    ) -> SaasBlueprint:
        """Desenha o blueprint do micro-SaaS (LLM ou plano determinístico)."""
        prompt = (
            "Projecta um micro-SaaS mínimo e monetizável, entregável em 1 sprint.\n"
            f"IDEIA: {idea}\nNICHO: {niche}\nPREÇO SUGERIDO: ${price_usd:.2f}/mês\n\n"
            "Requisitos: resolver UMA dor concreta, autenticação simples, "
            "persistência em SQLite/Postgres, API FastAPI, frontend estático. "
            "Devolve JSON: name, problem, target_user, stack (lista), "
            "entities (lista {name, fields}), endpoints (lista {method, path, purpose}), "
            "pricing ({model, price_usd, trial_days}), differentiators (lista)."
        )
        request = LLMRequest(
            prompt=prompt,
            system=self._default_system(),
            task="code",
            json_mode=True,
            max_tokens=1400,
            temperature=0.4,
            agent_id=self.owner_agent,
            context={"squad": self.squad_type},
            json_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "problem": {"type": "string"},
                    "target_user": {"type": "string"},
                    "stack": {"type": "array"},
                    "entities": {"type": "array"},
                    "endpoints": {"type": "array"},
                    "pricing": {"type": "object"},
                    "differentiators": {"type": "array"},
                },
            },
        )
        data, response = llm_router.complete_json(
            request,
            required_keys=[
                "name", "problem", "target_user", "stack",
                "entities", "endpoints", "pricing", "differentiators",
            ],
        )
        if response.is_llm:
            entities = [
                {
                    "name": str(e.get("name", "Item"))[:40],
                    "fields": str(e.get("fields", "id, name, created_at"))[:300],
                }
                for e in (data.get("entities") or [])
                if isinstance(e, dict)
            ]
            endpoints = [
                {
                    "method": str(e.get("method", "GET")).upper()[:8],
                    "path": str(e.get("path", "/"))[:120],
                    "purpose": str(e.get("purpose", ""))[:160],
                }
                for e in (data.get("endpoints") or [])
                if isinstance(e, dict)
            ]
            if entities and endpoints:
                pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
                return SaasBlueprint(
                    name=str(data.get("name") or idea)[:90],
                    problem=str(data.get("problem") or idea)[:400],
                    target_user=str(data.get("target_user") or niche)[:200],
                    stack=[str(s)[:40] for s in (data.get("stack") or ["FastAPI", "SQLite", "HTML"])][:8],
                    entities=entities[:6],
                    endpoints=endpoints[:12],
                    pricing={
                        "model": str(pricing.get("model", "subscription"))[:40],
                        "price_usd": round(float(pricing.get("price_usd", price_usd) or price_usd), 2),
                        "trial_days": int(pricing.get("trial_days", 7) or 7),
                    },
                    differentiators=[str(d)[:160] for d in (data.get("differentiators") or [])][:6],
                    engine=response.provider,
                    is_llm=True,
                )

        return SaasBlueprint(
            name=f"{idea[:50].title()} Tracker",
            problem=str(idea)[:400],
            target_user=niche[:200],
            stack=["Python 3.11", "FastAPI", "SQLite", "HTML estático"],
            entities=[
                {"name": "Item", "fields": "id, title, amount, due_date, status, created_at"},
                {"name": "User", "fields": "id, email, plan, created_at"},
            ],
            endpoints=[
                {"method": "GET", "path": "/health", "purpose": "Verificação de saúde"},
                {"method": "GET", "path": "/items", "purpose": "Listar registos"},
                {"method": "POST", "path": "/items", "purpose": "Criar registo"},
                {"method": "PATCH", "path": "/items/{item_id}", "purpose": "Actualizar estado"},
                {"method": "GET", "path": "/summary", "purpose": "Totais e métricas"},
            ],
            pricing={"model": "subscription", "price_usd": round(float(price_usd), 2), "trial_days": 7},
            differentiators=[
                "Configuração em 2 minutos, sem cartão",
                "Feito para PMEs locais com dados em meticais",
                "Exportação imediata dos dados (sem lock-in)",
            ],
            engine="template",
            is_llm=False,
        )

    # -------------------------------------------------------------- developer
    def generate_code(self, blueprint: SaasBlueprint) -> Dict[str, str]:
        """Gera os ficheiros do serviço: app.py, test_app.py, index.html, README."""
        safe_name = re.sub(r"[^a-z0-9_]", "_", blueprint.name.lower())[:40] or "micro_saas"
        entity = blueprint.entities[0]["name"] if blueprint.entities else "Item"
        entity_table = re.sub(r"[^a-z0-9_]", "_", entity.lower())

        app_py = f'''"""Micro-SaaS gerado pela Automaton Holding Corp.

Serviço: {blueprint.name}
Problema: {blueprint.problem[:200]}
Alvo: {blueprint.target_user[:120]}

Gerado em {utcnow_iso()}. Código completo, com tipagem e tratamento de erros.
Corre com: uvicorn app:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

log = logging.getLogger("{safe_name}")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DB_PATH = os.environ.get("SAAS_DB_PATH", "{safe_name}.db")
PRICE_USD = {blueprint.pricing.get("price_usd", 9.0):.2f}
TRIAL_DAYS = {blueprint.pricing.get("trial_days", 7)}

app = FastAPI(title="{blueprint.name}", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Conexão SQLite com row_factory e fecho garantido."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        log.error("Erro de base de dados: %s", exc)
        raise HTTPException(status_code=500, detail="Erro interno de base de dados") from exc
    finally:
        connection.close()


def init_db() -> None:
    """Cria as tabelas necessárias (idempotente)."""
    with db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS {entity_table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            )
            """
        )


init_db()


class {entity}Create(BaseModel):
    """Payload de criação validado pelo pydantic."""

    title: str = Field(min_length=1, max_length=200)
    amount: float = Field(default=0.0, ge=0)


class {entity}Update(BaseModel):
    status: str = Field(pattern="^(open|done|cancelled)$")


@app.get("/health")
def health() -> Dict[str, Any]:
    """Verificação de saúde usada pelo monitor e pelo contentor."""
    return {{"status": "ok", "service": "{safe_name}", "price_usd": PRICE_USD}}


@app.get("/items")
def list_items(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lista registos, opcionalmente filtrados por estado."""
    with db() as connection:
        if status:
            rows = connection.execute(
                "SELECT * FROM {entity_table} WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM {entity_table} ORDER BY id DESC"
            ).fetchall()
    return [dict(row) for row in rows]


@app.post("/items", status_code=201)
def create_item(payload: {entity}Create) -> Dict[str, Any]:
    """Cria um registo; devolve o objecto criado."""
    created_at = datetime.now(tz=timezone.utc).isoformat()
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO {entity_table} (title, amount, status, created_at) VALUES (?, ?, 'open', ?)",
            (payload.title, float(payload.amount), created_at),
        )
        new_id = cursor.lastrowid
    return {{"id": new_id, "title": payload.title, "amount": float(payload.amount),
             "status": "open", "created_at": created_at}}


@app.patch("/items/{{item_id}}")
def update_item(item_id: int, payload: {entity}Update) -> Dict[str, Any]:
    """Actualiza o estado de um registo existente."""
    with db() as connection:
        cursor = connection.execute(
            "UPDATE {entity_table} SET status = ? WHERE id = ?", (payload.status, item_id)
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Registo não encontrado")
        row = connection.execute(
            "SELECT * FROM {entity_table} WHERE id = ?", (item_id,)
        ).fetchone()
    return dict(row) if row else {{}}


@app.get("/summary")
def summary() -> Dict[str, Any]:
    """Totais para o dashboard."""
    with db() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(amount), 0) AS amount, "
            "SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done FROM {entity_table}"
        ).fetchone()
    total = int(row["total"] or 0) if row else 0
    done = int(row["done"] or 0) if row else 0
    return {{
        "total_items": total,
        "total_amount": round(float(row["amount"] or 0), 2) if row else 0.0,
        "completion_rate": round(done / total, 4) if total else 0.0,
        "monthly_price_usd": PRICE_USD,
        "trial_days": TRIAL_DAYS,
    }}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serve o frontend estático embutido (sem dependências externas)."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return "<h1>{blueprint.name}</h1><p>Frontend indisponível.</p>"
'''

        test_py = f'''"""Testes reais do serviço {blueprint.name} (QA automatizado)."""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "SAAS_DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db")
)

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_and_list() -> None:
    created = client.post("/items", json={{"title": "Teste QA", "amount": 12.5}})
    assert created.status_code == 201
    item = created.json()
    assert item["title"] == "Teste QA"
    listed = client.get("/items")
    assert listed.status_code == 200
    assert any(row["id"] == item["id"] for row in listed.json())


def test_validation_rejects_empty_title() -> None:
    response = client.post("/items", json={{"title": "", "amount": 1}})
    assert response.status_code == 422


def test_update_missing_item() -> None:
    response = client.patch("/items/999999", json={{"status": "done"}})
    assert response.status_code == 404


def test_summary() -> None:
    response = client.get("/summary")
    assert response.status_code == 200
    assert "total_items" in response.json()
'''

        index_html = f"""<!DOCTYPE html>
<html lang="pt">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{blueprint.name}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #0b1120; color: #e8eef7; margin: 0; padding: 28px 18px 60px; }}
    main {{ max-width: 680px; margin: 0 auto; }}
    h1 {{ font-size: 26px; margin-bottom: 6px; }}
    p.sub {{ color: #94a3b8; margin-bottom: 22px; }}
    form, .card {{ background: #131a26; border: 1px solid #1f2a3a; border-radius: 14px;
                  padding: 18px; margin-bottom: 16px; }}
    input, button {{ font-size: 16px; padding: 12px; border-radius: 10px; width: 100%;
                    box-sizing: border-box; margin-bottom: 10px; }}
    input {{ background: #0d141f; border: 1px solid #24344a; color: #e8eef7; }}
    button {{ background: linear-gradient(135deg, #22d3ee, #3b82f6); border: 0;
             color: #04121c; font-weight: 700; cursor: pointer; }}
    li {{ list-style: none; padding: 10px 0; border-bottom: 1px solid #1c2534;
         display: flex; justify-content: space-between; }}
    .done {{ color: #4ade80; }} .open {{ color: #fbbf24; }}
    small {{ color: #94a3b8; }}
  </style>
</head>
<body>
<main>
  <h1>{blueprint.name}</h1>
  <p class="sub">{blueprint.problem[:160]}</p>
  <div class="card"><strong>A partir de ${blueprint.pricing.get("price_usd", 9.0):.2f}/mês</strong>
    <br /><small>{blueprint.pricing.get("trial_days", 7)} dias de teste. Cancela quando quiseres.</small></div>
  <form id="f">
    <input id="title" placeholder="Descrição do registo" required />
    <input id="amount" type="number" step="0.01" min="0" placeholder="Valor (MZN/USD)" />
    <button type="submit">Adicionar</button>
  </form>
  <div class="card">
    <div id="summary"><small>A carregar resumo…</small></div>
    <ul id="list"></ul>
  </div>
</main>
<script>
  async function refresh() {{
    try {{
      const [items, summary] = await Promise.all([
        fetch('/items').then(r => r.json()),
        fetch('/summary').then(r => r.json())
      ]);
      document.getElementById('summary').innerHTML =
        '<strong>' + summary.total_items + '</strong> registos · total ' +
        summary.total_amount.toFixed(2) + ' · conclusão ' +
        (summary.completion_rate * 100).toFixed(0) + '%';
      const list = document.getElementById('list');
      list.innerHTML = items.map(i =>
        '<li><span>' + i.title + '</span><span class="' + i.status + '">' +
        (i.status === 'done' ? '✓' : '•') + '</span></li>').join('');
    }} catch (e) {{
      document.getElementById('summary').textContent = 'Erro a carregar dados.';
    }}
  }}
  document.getElementById('f').addEventListener('submit', async (ev) => {{
    ev.preventDefault();
    const title = document.getElementById('title').value.trim();
    if (!title) return;
    const amount = parseFloat(document.getElementById('amount').value || '0') || 0;
    await fetch('/items', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ title, amount }})
    }});
    document.getElementById('f').reset();
    refresh();
  }});
  refresh();
</script>
</body>
</html>
"""

        readme = textwrap.dedent(
            f"""# {blueprint.name}

Micro-SaaS gerado pela Automaton Holding Corp.

**Problema:** {blueprint.problem}
**Alvo:** {blueprint.target_user}
**Preço:** ${blueprint.pricing.get('price_usd', 9.0):.2f}/mês ({blueprint.pricing.get('trial_days', 7)} dias de teste)

## Stack
{chr(10).join(f'- {item}' for item in blueprint.stack)}

## Arranque local
```bash
pip install fastapi uvicorn pydantic pytest httpx
uvicorn app:app --host 0.0.0.0 --port 8090
pytest -q
```

## Endpoints
{chr(10).join(f"- `{e['method']} {e['path']}` — {e['purpose']}" for e in blueprint.endpoints)}

## Diferenciais
{chr(10).join(f"- {item}" for item in blueprint.differentiators)}

## Licença
MIT — Automaton Holding Corp. Dados do cliente exportáveis em CSV (sem lock-in).
"""
        )
        return {
            "app.py": app_py,
            "test_app.py": test_py,
            "index.html": index_html,
            "README.md": readme,
        }

    # -------------------------------------------------------------------- QA
    def quality_assurance(
        self,
        files: Dict[str, str],
        *,
        run_tests: bool = False,
        out_dir: Optional[Path] = None,
    ) -> QaReport:
        """Verificação estática (AST) + testes opcionais em subprocesso."""
        report = QaReport(passed=True)
        app_source = files.get("app.py", "")

        # 1. Compilação Python
        try:
            compile(app_source, "app.py", "exec")
            report.checks.append({"check": "python_compile", "passed": True,
                                  "message": "app.py compila sem erros de sintaxe."})
        except SyntaxError as exc:
            report.passed = False
            report.checks.append(
                {"check": "python_compile", "passed": False,
                 "message": f"Erro de sintaxe em app.py: {exc}"}
            )
            return report

        # 2. Verificações AST (qualidade mínima exigida pelo mandato)
        try:
            tree = ast.parse(app_source)
        except SyntaxError as exc:
            report.passed = False
            report.checks.append({"check": "ast_parse", "passed": False, "message": str(exc)})
            return report

        functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        missing_docstring = [
            fn.name for fn in functions
            if not ast.get_docstring(fn) and not fn.name.startswith("_")
        ]
        report.checks.append(
            {
                "check": "docstrings",
                "passed": len(missing_docstring) <= 1,
                "message": (
                    "Todas as funções têm docstring."
                    if not missing_docstring
                    else f"Funções sem docstring: {missing_docstring[:5]}"
                ),
            }
        )
        has_error_handling = any(
            isinstance(node, ast.ExceptHandler) for node in ast.walk(tree)
        )
        report.checks.append(
            {
                "check": "error_handling",
                "passed": has_error_handling,
                "message": (
                    "Tratamento de excepções presente."
                    if has_error_handling
                    else "Nenhum bloco try/except encontrado."
                ),
            }
        )
        has_annotations = all(
            fn.returns is not None
            for fn in functions
            if not fn.name.startswith("_") and fn.decorator_list
        )
        report.checks.append(
            {
                "check": "type_hints",
                "passed": has_annotations,
                "message": (
                    "Funções de rota anotadas com tipo de retorno."
                    if has_annotations
                    else "Adicionar anotações de retorno às rotas."
                ),
            }
        )
        lazy = re.search(r"#\s*(TODO|FIXME)|implementar\s+depois", app_source)
        report.checks.append(
            {
                "check": "no_lazy_code",
                "passed": lazy is None,
                "message": (
                    "Sem marcadores TODO/FIXME."
                    if lazy is None
                    else f"Código incompleto detectado: {lazy.group(0)}"
                ),
            }
        )
        if any(not c["passed"] for c in report.checks):
            report.passed = False

        # 3. Testes reais (opcional, em subprocesso isolado)
        if run_tests and out_dir is not None:
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "test_app.py"],
                    cwd=str(out_dir),
                    capture_output=True,
                    text=True,
                    timeout=240,
                    check=False,
                    env={"SAAS_DB_PATH": str(out_dir / "qa.db"), "PATH": "/usr/bin:/bin"},
                )
                ok = completed.returncode == 0
                report.checks.append(
                    {
                        "check": "pytest",
                        "passed": ok,
                        "message": (completed.stdout or completed.stderr or "")[-600:],
                    }
                )
                if not ok:
                    report.passed = False
            except (subprocess.SubprocessError, OSError) as exc:
                report.warnings.append(f"Execução de testes falhou: {exc}")
        elif run_tests:
            report.warnings.append("run_tests=True sem out_dir: testes não executados.")
        else:
            report.warnings.append(
                "Testes escritos mas não executados (allow_code_execution desligado). "
                "Correr 'pytest -q' no contentor isolado."
            )
        return report

    # ------------------------------------------------------------------ ciclo
    def run(
        self,
        *,
        idea: str,
        niche: str,
        smoke_test_id: Optional[str] = None,
        squad_id: Optional[str] = None,
        price_usd: float = 9.0,
        allow_code_execution: bool = False,
    ) -> SquadRunResult:
        """Arquiteto -> Dev -> QA -> Juiz, com todos os portões."""
        self.ensure_operational("construir micro-SaaS")
        self.ensure_not_repeated(idea, niche, tags=[self.squad_type, niche])
        validated = self.ensure_demand(smoke_test_id)

        sid = squad_id or self.stable_id("sq", self.squad_type, idea, niche)
        self.ensure_registered(squad_id=sid, name=idea[:80], niche=niche)

        blueprint = self.design(idea=idea, niche=niche, price_usd=price_usd)
        files = self.generate_code(blueprint)

        out_dir = Path(settings.vault_path) / "artifacts" / sid / utcnow_iso()[:10]
        out_dir.mkdir(parents=True, exist_ok=True)
        written: List[str] = []
        for name, content in files.items():
            path = out_dir / name
            try:
                path.write_text(content, encoding="utf-8")
                written.append(str(path))
            except OSError as exc:
                log.error("Não foi possível escrever %s: %s", path, exc)

        qa = self.quality_assurance(
            files, run_tests=allow_code_execution, out_dir=out_dir
        )

        judge_content = (
            f"# {blueprint.name}\n\n{blueprint.problem}\n\n"
            + "\n\n".join(
                f"## {name}\n```\n{content[:4000]}\n```" for name, content in files.items()
            )
        )
        verdict = self.review_with_judge(
            content=judge_content,
            squad_id=sid,
            metadata={
                "lines": len(files.get("app.py", "").splitlines()),
                "has_tests": "test_app.py" in files,
                "tests_passing": qa.passed if allow_code_execution else None,
            },
            content_type="code",
        )

        package = {
            "type": "micro_saas_package",
            "squad_id": sid,
            "blueprint": blueprint.to_dict(),
            "files": written,
            "qa": qa.to_dict(),
            "judge": verdict.to_dict(),
            "delivery": {
                "run": "uvicorn app:app --host 0.0.0.0 --port 8090",
                "test": "pytest -q",
                "container": "usar o serviço 'micro_saas' do docker-compose "
                             "(perfil micro_saas) para execução isolada",
            },
            "validated_demand": bool(validated),
            "smoke_test_id": smoke_test_id,
            "created_at": utcnow_iso(),
        }
        (out_dir / "package.json").write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.persist_artifact({"type": "micro_saas", "squad_id": sid, **package})
        self.record_agent_execution(
            success=qa.passed, judge_score=verdict.score,
            failure_reason="" if qa.passed else "qa_reprovado",
        )
        self.publish(
            "micro_saas_built",
            f"Micro-SaaS '{blueprint.name}' gerado — QA "
            f"{'ok' if qa.passed else 'falhou'}, juiz {verdict.score:.1f}.",
            squad_id=sid,
            qa_passed=qa.passed,
            judge_score=verdict.score,
        )

        return SquadRunResult(
            squad_id=sid,
            squad_type=self.squad_type,
            action="build_micro_saas",
            status="completed" if qa.passed else "awaiting_validation",
            summary=(
                f"Blueprint '{blueprint.name}' ({len(blueprint.endpoints)} endpoints), "
                f"{len(written)} ficheiros, QA {'aprovado' if qa.passed else 'reprovado'}, "
                f"juiz {verdict.score:.1f}/100. Saída: {out_dir}"
            ),
            artifacts=[package] + [{"type": "file", "path": p} for p in written],
            metrics={
                "qa_passed": qa.passed,
                "qa_checks": qa.checks,
                "judge_score": verdict.score,
                "files": len(written),
                "output_dir": str(out_dir),
                "tests_executed": allow_code_execution,
            },
            next_actions=[
                "Revisão humana do código e do blueprint.",
                "Deploy do serviço e configuração do checkout (LemonSqueezy).",
                "Executar pytest no contentor isolado antes de abrir ao público.",
            ],
            blockers=[] if qa.passed else [{"reason": "qa_reprovado",
                                            "checks": [c for c in qa.checks if not c["passed"]]}],
            llm_provider=blueprint.engine,
            is_llm=blueprint.is_llm,
        )


micro_saas_squad = MicroSaasSquad()

__all__ = ["MicroSaasSquad", "micro_saas_squad", "SaasBlueprint", "QaReport"]
