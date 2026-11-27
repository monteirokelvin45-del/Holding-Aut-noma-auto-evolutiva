# 🏛 Automaton Holding Corp.

**Holding e venture builder autónoma de IA.** Capital inicial **$0.00**. Caça
nichos, valida demanda com *smoke tests*, produz com esquadrões de IA,
auto-audita-se (qualidade, RH, contabilidade, jurídico) e reinveste os lucros
com divisão dinâmica em três fases — tudo supervisionado por um humano através
de um **APK Android nativo** (Kotlin + Jetpack Compose) com **Telegram Bot**
como canal de recurso.

```
┌───────────────────────────────────────────────────────────────────────────┐
│  CAMADA 5 · SUPERVISÃO HUMANA                                             │
│  APK Android (5 abas) · Bot Telegram · botão vermelho KILL SWITCH < 1s    │
├───────────────────────────────────────────────────────────────────────────┤
│  CAMADA 4 · GOVERNANÇA INTERNA                                            │
│  Juiz Eval (0–100, corte 85) · RH de IA · Contabilidade/abate · Jurídico  │
├───────────────────────────────────────────────────────────────────────────┤
│  CAMADA 3 · TESOURARIA E FASES                                            │
│  5% imposto · 10/20/70 → 10/40/50 → 50/30/20 · M-Pesa/e-Mola/PayPal/Wise   │
├───────────────────────────────────────────────────────────────────────────┤
│  CAMADA 2 · PRODUÇÃO (ESQUADRÕES)                                         │
│  vídeo curto · infoprodutos+SEO+afiliados · micro-SaaS · dados B2B / POD   │
├───────────────────────────────────────────────────────────────────────────┤
│  CAMADA 1 · DESCOBERTA E VALIDAÇÃO                                         │
│  Scout (GitHub, Reddit, marketplaces) · Cemitério · Smoke Test obrigatório │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## ⚡ Arranque em 60 segundos

### 1. Backend (Docker)

```bash
cp .env.example .env          # opcional: todas as chaves podem ficar vazias
docker compose up -d --build
curl http://localhost:8000/health
open http://localhost:8000/docs
```

### 2. Backend (Python local)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

### 3. APK Android

```bash
cd android_apk
./gradlew :app:assembleDebug \
  -Pautomaton.baseUrl="https://o-teu-backend.exemplo" \
  -Pautomaton.token="o-teu-API_ACCESS_TOKEN"
# saída: app/build/outputs/apk/debug/app-debug.apk
```

Ou descarrega o APK compilado nos *artifacts* do workflow
[`02 · Build Android APK`](.github/workflows/02_build_android_apk.yml).

### 4. Ciclo autónomo

```bash
# manual
curl -X POST http://localhost:8000/cycle/run \
  -H "X-Automaton-Token: $API_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"force_scan": true, "produce": true}'
```

Em produção, o workflow `01 · Autonomous Holding Cycle` corre a cada 6 horas.

---

## 🧱 Estrutura

```
backend/
  config/        settings.py (Pydantic), agents.yaml, tasks.yaml
  core/          vault_store, security_guard, llm_router, approvals, runtime_bus,
                 shared_memory, observability_langfuse, clock, logger, errors
  finance/       cfo_treasury (fases + 5% imposto), payout_bridge (M-Pesa/e-Mola/Wise/PayPal)
  governance/    ceo_orchestrator, accountant_auditor, hr_department, legal_compliance
  validation/    demand_validator (smoke tests), eval_judge (0–100)
  squads/        scout_hunter, video_factory, infoproducts_affiliate, micro_saas_dev
  tests/         suíte de integração (pytest + TestClient)
  main.py        FastAPI 1.0.0 — 53 rotas
  Dockerfile     imagem multi-stage, non-root, com ffmpeg
android_apk/     Kotlin 2.0 + Compose (Material 3), 5 ecrãs, biometria
vault_data/      estado auditável (ledger, aprovações, métricas, post-mortem)
.github/workflows/  01 ciclo autónomo · 02 build do APK
```

---

## 🛡 Regras inegociáveis

| Regra | Implementação |
|---|---|
| **Capital inicial $0.00** | `DAILY_SPEND_CAP_USD=0.00`; só free-tier (Groq 30 RPM/1 000 dia, Gemini, OpenRouter 50/dia) ou modelos locais |
| **Nada se produz sem validar** | `demand_validator` exige cliques + conversão mínimos **e** `cemetery_check` limpo |
| **Cemitério de negócios** | Similaridade de embeddings (`DEFAULT_SIMILARITY_BLOCK=0.40`) bloqueia ideias já mortas |
| **Juiz Eval** | 0–100; **< 85 reprova** e devolve ao produtor com motivos accionáveis |
| **Kill Switch** | `POST /kill-switch/engage` < 1s; botão vermelho no APK e `/kill` no Telegram |
| **5% de imposto** | `tax_reserve_usd` retido na hora, intocável, nunca usado em despesas |
| **Fases** | 10/20/70 → 10/40/50 → 50/30/20 (limiares $1 500 e $15 000/mês) |
| **Biometria obrigatória** | Gastos do fundo > $5.00 e todos os saques exigem `biometric_verified=true` (HTTP 428 sem ela) |
| **Regra de abate** | 7 dias sem tracção ou 21 dias sem o primeiro dólar ⇒ abate + post-mortem |
| **Anti prompt-injection** | Todo o conteúdo web passa pelo LLM Guard (heurística local se ausente); agentes de pesquisa nunca vêem chaves de pagamento |
| **Licenças** | Só MIT / Apache-2.0 / BSD / LGPL. **Proibido CC-BY-NC** (`2noise/ChatTTS`, `F5-TTS`) |

---

## 📱 APK — 5 abas

| Aba | O que faz |
|---|---|
| **Ao Vivo** | Terminal do pensamento dos agentes (WebSocket `/ws/live`), património, receita 30d, botão “Ciclo” |
| **Aprovar** | Mesa de decisões, críticos primeiro, aprovar/vetar **com biometria** |
| **Tesouro** | Cofres USD/MZN, fase actual, pedido de saque M-Pesa/e-Mola/PayPal/Wise (2 passos) |
| **Diretoria** | RH de IA, contabilidade e impostos, Juiz Eval, jurídico, validação |
| **Esquadrões** | Portefólio com TTFD/CAC/LTV e abate com biometria |

O **botão vermelho** no canto superior direito congela o sistema em menos de um
segundo; volta a tocar para libertar.

---

## 🔌 API (resumo)

```bash
# estado
GET  /health · /system/status · /system/config
# segurança
POST /kill-switch/engage · /kill-switch/release
# aprovações
GET  /approvals          POST /approvals/{id}/decide   (428 sem biometria)
# tesouraria
GET  /treasury · /treasury/ledger · /treasury/quote
POST /treasury/revenue · /treasury/spend · /treasury/payouts/reserve · /treasury/payouts
# governança
GET  /governance · /governance/hr · /governance/accounting · /governance/legal
POST /governance/hr/review · /governance/legal/audit · /governance/cemetery/check
# esquadrões
GET  /squads  POST /squads/{id}/kill · /squads/scout/scan
POST /squads/video/produce · /squads/infoproducts/produce · /squads/micro-saas/build
# validação e ciclo
POST /validation · /validation/{id}/event · /validation/judge · /cycle/run
# tempo real
WS   /ws/live        (eventos + snapshot + heartbeat)
```

Documentação interactiva: **`/docs`**.

---

## 🧪 Testes

```bash
.venv/bin/python -m pytest backend/tests -q     # 12 testes, ~1.3 s
```

Cobrem: token de escrita, as três fases, retenção fiscal de 5%, teto de gasto
zero, Juiz Eval (reprova/ aprova), kill switch a abortar o ciclo, smoke test
com demanda real, landing page pública, biometria obrigatória (428), ciclo
completo do CEO e bloqueio pelo cemitério.

---

## 🗝 Variáveis de ambiente essenciais

Ver [`.env.example`](.env.example) para a lista completa. Mínimo funcional:

```env
API_ACCESS_TOKEN=...        # header X-Automaton-Token do APK
SECRET_KEY=...              # assinatura de sessões
DAILY_SPEND_CAP_USD=0.00
APPROVAL_THRESHOLD_USD=5.00
TELEGRAM_BOT_TOKEN=         # opcional (fallback de supervisão)
TELEGRAM_ALLOWED_CHAT_IDS=
```

Sem chaves de LLM o sistema **não falha**: usa o motor heurístico local
determinístico e marca as entregas como `engine="template"`.

---

## 📜 Licença

Código sob **MIT**. Dependências apenas com licenças permissivas; qualquer
componente `CC-BY-NC` é bloqueado pelo departamento jurídico antes de entrar
em produção.
