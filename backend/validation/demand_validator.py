"""Agente Validador: produzir com IA é barato, a atenção é o gargalo.

Antes de qualquer esquadrão gastar processamento, o Validador:

  1. Confirma que a ideia **não está no cemitério** (``shared_memory``).
  2. Gera um **Smoke Test** gratuito: uma landing page estática 9:16-friendly
     com botão de lista VIP/pré-venda, pronta para GitHub Pages, mais 3 posts
     curtos de teste para TikTok/X/Reddit (texto, sem produção de vídeo).
  3. Regista a campanha no cofre (``squad_metrics.json`` -> ``smoke_tests``).
  4. Acumula cliques/inscrições via API (``/validation/{id}/event``).
  5. Decide: **aprovado** se (cliques >= meta e conversão >= meta) dentro da
     janela; **reprovado** se o prazo expirar sem as metas.

Sem GitHub token, a página é gravada em ``vault_data/smoke_tests/<id>/index.html``
e servida pelo backend em ``/validation/{id}/page`` — continua 100% gratuita.

Nenhuma destas operações custa dinheiro: usa templates determinísticos, com
LLM opcional apenas para o copy (e fallback local honesto).
"""

from __future__ import annotations

import hashlib
import html
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.clock import days_between, parse_iso, utcnow, utcnow_iso
from ..core.errors import NotFound, ValidationBlocked
from ..core.llm_router import LLMRequest, llm_router
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.shared_memory import shared_memory
from ..core.vault_store import SQUAD_METRICS, vault

log = get_logger("validation.demand")

SMOKE_DIR = Path(settings.vault_path) / "smoke_tests"


@dataclass
class SmokeTest:
    id: str
    idea: str
    niche: str
    squad_type: str
    hypothesis: str
    cta: str
    price_anchor_usd: float
    status: str  # running | passed | failed | expired | cancelled
    created_at: str
    deadline_at: str
    min_clicks: int
    min_conversion_rate: float
    clicks: int = 0
    unique_clicks: int = 0
    signups: int = 0
    preorders: int = 0
    revenue_usd: float = 0.0
    sources: Dict[str, int] = field(default_factory=dict)
    posts: List[str] = field(default_factory=list)
    page_path: str = ""
    public_url: str = ""
    cemetery_check: Dict[str, Any] = field(default_factory=dict)
    decided_at: Optional[str] = None
    decision_reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["conversion_rate"] = round(
            self.signups / self.clicks, 4) if self.clicks else 0.0
        payload["days_remaining"] = round(
            max(0.0, days_between(utcnow_iso(), self.deadline_at)), 2
        )
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SmokeTest":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})  # type: ignore[arg-type]


class DemandValidator:
    """Cria, mede e decide Smoke Tests de demanda real."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        SMOKE_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- cofre
    def _read(self) -> Dict[str, Any]:
        data = vault.read(SQUAD_METRICS)
        tests = data.get("smoke_tests")
        if not isinstance(tests, list):
            data["smoke_tests"] = []
        return data

    def list_tests(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        tests = [SmokeTest.from_dict(raw) for raw in self._read()["smoke_tests"]]
        if status:
            tests = [t for t in tests if t.status == status]
        tests.sort(key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in tests]

    def get(self, test_id: str) -> SmokeTest:
        for raw in self._read()["smoke_tests"]:
            if raw.get("id") == test_id:
                return SmokeTest.from_dict(raw)
        raise NotFound(f"Smoke test {test_id} não encontrado.")

    # ------------------------------------------------------------- criação
    def create_smoke_test(
        self,
        *,
        idea: str,
        niche: str,
        squad_type: str = "generic",
        hypothesis: str = "",
        cta: str = "Quero entrar na lista VIP",
        price_anchor_usd: float = 0.0,
        min_clicks: Optional[int] = None,
        min_conversion_rate: Optional[float] = None,
        window_days: Optional[int] = None,
        campaign_source: str = "organic",
    ) -> Dict[str, Any]:
        """Executa o portão de validação e cria a campanha de demanda."""
        idea = (idea or "").strip()
        niche = (niche or "").strip()
        if len(idea) < 8:
            raise ValidationBlocked(
                "Descrição da ideia demasiado curta para validar.",
                details={"idea": idea},
            )

        # ---- 1. cemitério (obrigatório) ------------------------------------
        cemetery = shared_memory.check_against_cemetery(idea=idea, niche=niche)
        log.info("Cemitério OK para '%s' (corpus=%s).", idea[:60], cemetery["corpus_size"])

        # ---- 2. copy do teste (LLM opcional) -------------------------------
        copy = self._generate_copy(idea, niche, cta, price_anchor_usd)

        clicks_goal = int(min_clicks or settings.smoke_min_clicks)
        conversion_goal = float(
            min_conversion_rate or settings.smoke_min_conversion_rate
        )
        days = int(
            max(
                settings.smoke_min_days,
                min(window_days or settings.smoke_min_days, settings.smoke_max_days),
            )
        )
        test_id = "smk_" + hashlib.sha1(
            f"{idea}|{niche}|{utcnow_iso()}".encode("utf-8")
        ).hexdigest()[:12]
        created = utcnow()
        deadline = (created + timedelta(days=days)).replace(microsecond=0).isoformat()

        page_dir = SMOKE_DIR / test_id
        page_dir.mkdir(parents=True, exist_ok=True)
        page_path = page_dir / "index.html"
        page_path.write_text(
            self._render_landing_page(
                test_id=test_id,
                headline=copy["headline"],
                subheadline=copy["subheadline"],
                bullets=copy["bullets"],
                cta=cta,
                price_anchor_usd=price_anchor_usd,
                deadline=deadline,
            ),
            encoding="utf-8",
        )

        posts = self._generate_test_posts(idea, niche, cta)

        test = SmokeTest(
            id=test_id,
            idea=idea[:600],
            niche=niche[:200],
            squad_type=squad_type[:60],
            hypothesis=(hypothesis or copy["hypothesis"])[:500],
            cta=cta[:160],
            price_anchor_usd=round(float(price_anchor_usd), 2),
            status="running",
            created_at=created.replace(microsecond=0).isoformat(),
            deadline_at=deadline,
            min_clicks=clicks_goal,
            min_conversion_rate=conversion_goal,
            sources={campaign_source: 0},
            posts=posts,
            page_path=str(page_path),
            public_url=f"{settings.github_pages_base_url.rstrip('/')}/smoke/{test_id}/",
            cemetery_check=cemetery,
            metadata={
                "copy_engine": copy["engine"],
                "campaign_source": campaign_source,
                "is_llm_copy": copy["is_llm"],
            },
        )

        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            data.setdefault("smoke_tests", [])
            data["smoke_tests"].append(test.to_dict())
            if len(data["smoke_tests"]) > 200:
                data["smoke_tests"] = data["smoke_tests"][-200:]

        event_bus.publish(
            "smoke_test_created",
            agent="demand_validator",
            message=(
                f"Smoke test {test_id} para '{idea[:80]}' — meta: "
                f"{clicks_goal} cliques e {conversion_goal:.0%} conversão em {days} dias."
            ),
            payload={
                "test_id": test_id,
                "deadline_at": deadline,
                "min_clicks": clicks_goal,
                "min_conversion_rate": conversion_goal,
                "page_path": str(page_path),
            },
        )
        log.warning(
            "Smoke test criado %s (%s): meta %d cliques / %.1f%% em %d dias",
            test_id, squad_type, clicks_goal, conversion_goal * 100, days,
        )
        return {
            "test": test.to_dict(),
            "page_path": str(page_path),
            "posts": posts,
            "next_step": (
                "Publicar a página (GitHub Pages ou /validation/{id}/page) e os "
                "3 posts de teste. A produção real só desbloqueia após aprovação."
            ),
        }

    # ---------------------------------------------------------------- medição
    def record_event(
        self,
        test_id: str,
        *,
        event: str = "click",
        source: str = "direct",
        value_usd: float = 0.0,
        count: int = 1,
    ) -> Dict[str, Any]:
        """Regista cliques/inscrições/pré-vendas e reavalia a decisão."""
        event_kind = (event or "click").lower()
        if event_kind not in {"click", "signup", "preorder", "view", "sale"}:
            raise ValidationBlocked(f"Evento de validação inválido: {event}")
        increment = max(1, int(count))

        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            tests = data.setdefault("smoke_tests", [])
            target: Optional[Dict[str, Any]] = None
            for raw in tests:
                if raw.get("id") == test_id:
                    target = raw
                    break
            if target is None:
                raise NotFound(f"Smoke test {test_id} não encontrado.")
            if target.get("status") != "running":
                return {
                    "test": target,
                    "note": f"Teste já encerrado ({target.get('status')}); evento ignorado.",
                }

            if event_kind == "click":
                target["clicks"] = int(target.get("clicks", 0)) + increment
                target["unique_clicks"] = int(target.get("unique_clicks", 0)) + increment
            elif event_kind == "view":
                target.setdefault("views", 0)
                target["views"] = int(target.get("views", 0)) + increment
            elif event_kind == "signup":
                target["signups"] = int(target.get("signups", 0)) + increment
                target["clicks"] = int(target.get("clicks", 1)) + 0
            elif event_kind == "preorder":
                target["preorders"] = int(target.get("preorders", 0)) + increment
                target["signups"] = int(target.get("signups", 0)) + increment
            elif event_kind == "sale":
                target["revenue_usd"] = round(
                    float(target.get("revenue_usd", 0.0)) + max(0.0, float(value_usd)),
                    6,
                )
                target["preorders"] = int(target.get("preorders", 0)) + increment

            sources = target.setdefault("sources", {})
            key = (source or "direct")[:60]
            sources[key] = int(sources.get(key, 0)) + increment
            target["updated_at"] = utcnow_iso()
            snapshot = dict(target)

        decision = self.evaluate(test_id, snapshot=snapshot)
        return {"test": decision.get("test", snapshot), "decision": decision.get("decision"),
                "metrics": decision.get("metrics", {})}

    @staticmethod
    def _passou_metade_da_janela(test: Any, deadline: Optional[datetime]) -> bool:
        """True se já decorreu metade do prazo do teste.

        A reprovação antecipada só é justa a meio da janela. Sem esta guarda
        disparava mal os cliques chegavam ao dobro da meta — antes de qualquer
        inscrição poder ser registada, porque num funil os cliques precedem as
        conversões.
        """
        if deadline is None:
            return True
        try:
            inicio = parse_iso(test.created_at)
        except Exception:
            return True
        if inicio is None:
            return True
        meio = inicio + (deadline - inicio) / 2
        return utcnow() >= meio

    def evaluate(
        self, test_id: str, *, snapshot: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Decide se o teste passou, falhou por prazo ou continua a correr."""
        test = SmokeTest.from_dict(snapshot) if snapshot else self.get(test_id)
        conversion_now = (int(test.signups) / int(test.clicks)) if test.clicks else 0.0
        metrics = {
            "clicks": int(test.clicks),
            "signups": int(test.signups),
            "conversion_rate": round(conversion_now, 4),
            "min_clicks": test.min_clicks,
            "min_conversion_rate": test.min_conversion_rate,
        }
        if test.status != "running":
            return {
                "test": test.to_dict(),
                "decision": test.status,
                "changed": False,
                "reason": test.decision_reason,
                "metrics": metrics,
            }

        clicks = int(test.clicks)
        signups = int(test.signups)
        conversion = (signups / clicks) if clicks else 0.0
        deadline = parse_iso(test.deadline_at)
        expired = bool(deadline and utcnow() > deadline)

        new_status = "running"
        reason = ""
        if clicks >= test.min_clicks and conversion >= test.min_conversion_rate:
            new_status = "passed"
            reason = (
                f"Meta atingida: {clicks} cliques (meta {test.min_clicks}) e "
                f"conversão {conversion:.1%} (meta {test.min_conversion_rate:.1%})."
            )
        elif expired:
            new_status = "expired"
            reason = (
                f"Prazo expirado sem meta: {clicks}/{test.min_clicks} cliques e "
                f"conversão {conversion:.1%} (meta {test.min_conversion_rate:.1%})."
            )
        elif (
            clicks >= test.min_clicks * 2
            and conversion < test.min_conversion_rate
            and signups <= 1
            # O prazo tem de estar a meio. Sem isto, a reprovação antecipada
            # disparava no instante em que os cliques chegavam — antes de
            # qualquer inscrição poder ser registada. Medido: 80 cliques e
            # depois 6 inscrições, e o teste já estava `failed`, por isso a
            # conversão de 7,5% nunca foi avaliada. Num funil real os cliques
            # vêm primeiro; o julgamento tem de esperar metade da janela.
            and self._passou_metade_da_janela(test, deadline)
        ):
            # Volume claramente suficiente, metade da janela passada e ainda
            # quase sem inscrições: reprovação antecipada (evita gastar a
            # janela toda num teste morto).
            new_status = "failed"
            reason = (
                f"{clicks} cliques com apenas {signups} inscrição(ões) "
                f"({conversion:.1%} < {test.min_conversion_rate:.1%}): "
                "a oferta não ressoa — reformular antes de produzir."
            )

        changed = new_status != "running"
        if changed:
            with self._lock, vault.transaction(SQUAD_METRICS) as data:
                for raw in data.setdefault("smoke_tests", []):
                    if raw.get("id") == test_id:
                        raw["status"] = new_status
                        raw["decided_at"] = utcnow_iso()
                        raw["decision_reason"] = reason
                        test = SmokeTest.from_dict(raw)
                        break

            level = "warning" if new_status == "passed" else "error"
            event_bus.publish(
                "smoke_test_decided",
                agent="demand_validator",
                message=f"Smoke test {test_id}: {new_status.upper()} — {reason}",
                level=level,
                payload={
                    "test_id": test_id,
                    "status": new_status,
                    "clicks": clicks,
                    "conversion_rate": round(conversion, 4),
                    "production_unlocked": new_status == "passed",
                },
            )
            log.warning("Smoke test %s decidido: %s", test_id, new_status)

        return {
            "test": test.to_dict(),
            "decision": new_status,
            "changed": changed,
            "reason": reason,
            "metrics": {
                "clicks": clicks,
                "signups": signups,
                "conversion_rate": round(conversion, 4),
                "min_clicks": test.min_clicks,
                "min_conversion_rate": test.min_conversion_rate,
            },
        }

    def require_validated(self, test_id: str) -> Dict[str, Any]:
        """Portão usado pelos esquadrões antes de produzir. Lança se não passar."""
        result = self.evaluate(test_id)
        if result["test"]["status"] != "passed":
            raise ValidationBlocked(
                "Produção bloqueada: o Smoke Test de demanda ainda não foi aprovado.",
                details={
                    "test_id": test_id,
                    "status": result["test"]["status"],
                    "metrics": result.get("metrics", {}),
                    "reason": result.get("reason", ""),
                },
            )
        return result["test"]

    def cancel(self, test_id: str, reason: str = "cancelado manualmente") -> Dict[str, Any]:
        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            for raw in data.setdefault("smoke_tests", []):
                if raw.get("id") == test_id:
                    raw["status"] = "cancelled"
                    raw["decided_at"] = utcnow_iso()
                    raw["decision_reason"] = reason[:400]
                    return dict(raw)
        raise NotFound(f"Smoke test {test_id} não encontrado.")

    def sweep_expired(self) -> int:
        """Avalia todos os testes em curso (chamado a cada ciclo do CEO)."""
        changed = 0
        for raw in self._read()["smoke_tests"]:
            if raw.get("status") != "running":
                continue
            try:
                result = self.evaluate(str(raw.get("id")))
                if result.get("changed"):
                    changed += 1
            except Exception as exc:
                log.warning("Falha ao avaliar smoke test %s: %s", raw.get("id"), exc)
        return changed

    # ------------------------------------------------------------------ copy
    def _generate_copy(
        self, idea: str, niche: str, cta: str, price_anchor_usd: float
    ) -> Dict[str, Any]:
        """Gera headline/subheadline/bullets; LLM primeiro, template sempre."""
        fallback = {
            "headline": self._titlecase(idea[:90]),
            "subheadline": (
                f"Estamos a validar interesse em {niche or 'este nicho'}. "
                "Entra na lista VIP e recebe acesso antecipado."
            ),
            "bullets": [
                "Acesso antecipado antes do lançamento público",
                "Preço de fundador congelado para sempre",
                "Conteúdo prático, sem enrolação",
            ],
            "hypothesis": (
                f"Se {self._lower_first(idea[:120])} resolver uma dor real, "
                "converterá pelo menos a meta mínima de inscrições."
            ),
            "engine": "template",
            "is_llm": False,
        }
        try:
            request = LLMRequest(
                prompt=(
                    "Escreve copy de uma landing page de validação de demanda.\n"
                    f"IDEIA: {idea}\nNICHO: {niche or 'geral'}\nCTA: {cta}\n"
                    f"ÂNCORA DE PREÇO: ${price_anchor_usd:.2f}\n\n"
                    "Devolve JSON com as chaves: headline (máx 70 chars), "
                    "subheadline (máx 160), bullets (3 strings curtas), "
                    "hypothesis (1 frase). Português europeu/moçambicano, "
                    "sem promessas de rendimento garantido."
                ),
                task="fast",
                json_mode=True,
                max_tokens=500,
                temperature=0.6,
                agent_id="demand_validator",
                context={"squad": "validation"},
                json_schema={
                    "type": "object",
                    "properties": {
                        "headline": {"type": "string"},
                        "subheadline": {"type": "string"},
                        "bullets": {"type": "array"},
                        "hypothesis": {"type": "string"},
                    },
                },
            )
            data, response = llm_router.complete_json(
                request, required_keys=["headline", "subheadline", "bullets", "hypothesis"]
            )
            if not response.is_llm:
                return fallback
            bullets = data.get("bullets")
            if isinstance(bullets, str):
                bullets = [line.strip("-• ") for line in bullets.splitlines() if line.strip()]
            if not isinstance(bullets, list) or not bullets:
                bullets = fallback["bullets"]
            return {
                "headline": str(data.get("headline") or fallback["headline"])[:90],
                "subheadline": str(data.get("subheadline") or fallback["subheadline"])[:200],
                "bullets": [str(b)[:120] for b in bullets][:4],
                "hypothesis": str(data.get("hypothesis") or fallback["hypothesis"])[:400],
                "engine": response.provider,
                "is_llm": True,
            }
        except Exception as exc:
            log.warning("Copy via LLM falhou (%s); a usar template.", exc)
            return fallback

    def _generate_test_posts(self, idea: str, niche: str, cta: str) -> List[str]:
        """3 posts curtos de teste (sem custo de produção de vídeo)."""
        base = self._lower_first(idea[:140])
        return [
            f"P0 · Gancho: Ninguém fala disto em {niche or 'Portugal/Moçambique'}: {base}. "
            f"{cta} (link na bio).",
            f"P1 · Prova: O que já testámos sobre {base}. 3 lições em 30 segundos. "
            "Comenta 'QUERO' se quiseres o acesso antecipado.",
            f"P2 · Oferta: Estamos a construir {base}. Lista VIP com preço de fundador. "
            f"{cta}.",
        ]

    @staticmethod
    def _titlecase(text: str) -> str:
        cleaned = (text or "").strip().rstrip(".")
        return cleaned[:1].upper() + cleaned[1:] if cleaned else "Nova oportunidade"

    @staticmethod
    def _lower_first(text: str) -> str:
        cleaned = (text or "").strip()
        return cleaned[:1].lower() + cleaned[1:] if cleaned else cleaned

    # ------------------------------------------------------------------ HTML
    def _render_landing_page(
        self,
        *,
        test_id: str,
        headline: str,
        subheadline: str,
        bullets: List[str],
        cta: str,
        price_anchor_usd: float,
        deadline: str,
    ) -> str:
        """Landing page estática, mobile-first, sem dependências externas."""
        safe = html.escape
        bullet_html = "\n".join(f"        <li>{safe(str(b))}</li>" for b in bullets)
        price_block = (
            f'<p class="price">Pré-venda de fundador: <strong>${price_anchor_usd:.2f}</strong></p>'
            if price_anchor_usd > 0
            else '<p class="price">Acesso antecipado <strong>gratuito</strong> para a lista VIP</p>'
        )
        return f"""<!DOCTYPE html>
<html lang="pt">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>{safe(headline)}</title>
  <meta name="description" content="{safe(subheadline)}" />
  <meta property="og:title" content="{safe(headline)}" />
  <meta property="og:description" content="{safe(subheadline)}" />
  <meta property="og:type" content="website" />
  <style>
    :root {{ color-scheme: dark; --bg:#0b0f17; --card:#131a26; --accent:#22d3ee; --text:#e8eef7; --muted:#94a3b8; }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: radial-gradient(circle at 20% 10%, #16324a 0%, var(--bg) 55%);
            color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            min-height: 100dvh; display: flex; align-items: center; justify-content: center; padding: 24px; }}
    main {{ background: var(--card); border: 1px solid #1f2a3a; border-radius: 20px; padding: 28px 22px;
            max-width: 520px; width: 100%; box-shadow: 0 24px 60px rgba(0,0,0,.45); }}
    .badge {{ display:inline-block; font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
              color: var(--accent); border: 1px solid rgba(34,211,238,.35); border-radius: 999px;
              padding: 6px 12px; margin-bottom: 18px; }}
    h1 {{ font-size: clamp(22px, 6vw, 30px); line-height: 1.2; margin-bottom: 12px; }}
    p.sub {{ color: var(--muted); font-size: 15px; line-height: 1.55; margin-bottom: 18px; }}
    ul {{ list-style: none; margin: 0 0 20px; }}
    li {{ padding: 10px 0 10px 28px; position: relative; font-size: 15px; border-bottom: 1px solid #1c2534; }}
    li::before {{ content: "✓"; position: absolute; left: 0; color: var(--accent); font-weight: 700; }}
    .price {{ color: var(--muted); font-size: 14px; margin-bottom: 18px; }}
    .price strong {{ color: var(--text); }}
    form {{ display: flex; flex-direction: column; gap: 10px; }}
    input[type=email] {{ background: #0d141f; border: 1px solid #24344a; border-radius: 12px;
      color: var(--text); padding: 14px 16px; font-size: 16px; }}
    input[type=email]:focus {{ outline: 2px solid var(--accent); border-color: transparent; }}
    button {{ background: linear-gradient(135deg, #22d3ee, #3b82f6); border: 0; border-radius: 12px;
      color: #04121c; font-weight: 700; font-size: 16px; padding: 15px 18px; cursor: pointer; }}
    button:disabled {{ opacity: .6; cursor: default; }}
    .foot {{ margin-top: 18px; font-size: 12px; color: var(--muted); line-height: 1.5; }}
    .ok {{ display:none; margin-top: 14px; padding: 14px; border-radius: 12px;
           background: rgba(34,197,94,.12); border: 1px solid rgba(34,197,94,.4); font-size: 14px; }}
  </style>
</head>
<body>
  <main data-smoke-test="{safe(test_id)}">
    <span class="badge">Validação de demanda · {safe(deadline[:10])}</span>
    <h1>{safe(headline)}</h1>
    <p class="sub">{safe(subheadline)}</p>
    <ul>
{bullet_html}
    </ul>
    {price_block}
    <form id="vip" novalidate>
      <input type="email" id="email" name="email" placeholder="o.teu@email.com" required
             autocomplete="email" inputmode="email" />
      <button type="submit" id="btn">{safe(cta)}</button>
    </form>
    <div class="ok" id="ok">Estás na lista VIP. Vais receber o acesso antecipado.</div>
    <p class="foot">
      Página de teste de interesse. Sem promessas de rendimento. Os teus dados
      só são usados para este lançamento e podem ser removidos a pedido.
    </p>
  </main>
  <script>
    // Endpoint relativo ao backend que serve esta página.
    var API_BASE = window.location.pathname.replace(/\\/validation\\/.*$/, '') || '';
    var TEST_ID = document.querySelector('main').dataset.smokeTest;
    function track(event, extra) {{
      var payload = Object.assign({{ event: event, source: 'landing' }}, extra || {{}});
      try {{
        fetch(API_BASE + '/validation/' + TEST_ID + '/event', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
          keepalive: true
        }});
      }} catch (e) {{ /* nunca bloquear o utilizador por telemetria */ }}
    }}
    track('click');
    document.getElementById('vip').addEventListener('submit', function (ev) {{
      ev.preventDefault();
      var email = document.getElementById('email').value.trim();
      if (!email || email.indexOf('@') < 1) {{ document.getElementById('email').focus(); return; }}
      track('signup', {{ email_hash: email }});
      document.getElementById('btn').disabled = true;
      document.getElementById('vip').style.display = 'none';
      document.getElementById('ok').style.display = 'block';
    }});
  </script>
</body>
</html>
"""

    def render_page(self, test_id: str) -> str:
        """Devolve o HTML do teste (para servir via API)."""
        test = self.get(test_id)
        path = Path(test.page_path)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("Não foi possível ler a página de %s: %s", test_id, exc)
        return self._render_landing_page(
            test_id=test.id,
            headline=test.idea,
            subheadline=test.hypothesis,
            bullets=["Acesso antecipado", "Preço de fundador", "Sem enrolação"],
            cta=test.cta,
            price_anchor_usd=test.price_anchor_usd,
            deadline=test.deadline_at,
        )

    # -------------------------------------------------------------- relatório
    def report(self) -> Dict[str, Any]:
        tests = [SmokeTest.from_dict(raw) for raw in self._read()["smoke_tests"]]
        running = [t for t in tests if t.status == "running"]
        passed = [t for t in tests if t.status == "passed"]
        failed = [t for t in tests if t.status in {"failed", "expired"}]
        total_clicks = sum(t.clicks for t in tests)
        total_signups = sum(t.signups for t in tests)
        return {
            "summary": {
                "total": len(tests),
                "running": len(running),
                "passed": len(passed),
                "failed": len(failed),
                "total_clicks": total_clicks,
                "total_signups": total_signups,
                "overall_conversion": round(
                    total_signups / total_clicks, 4) if total_clicks else 0.0,
                "production_unlocked": len(passed),
            },
            "thresholds": {
                "min_clicks": settings.smoke_min_clicks,
                "min_conversion_rate": settings.smoke_min_conversion_rate,
                "window_days": [settings.smoke_min_days, settings.smoke_max_days],
            },
            "active": [t.to_dict() for t in running[:20]],
            "generated_at": utcnow_iso(),
        }


demand_validator = DemandValidator()

__all__ = ["DemandValidator", "demand_validator", "SmokeTest"]
