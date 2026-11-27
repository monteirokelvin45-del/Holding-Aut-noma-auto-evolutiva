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

## 🤖 Integração com IA — o que está ligado, exactamente

Três camadas, todas auditáveis:

**1. Roteador LLM (`backend/core/llm_router.py`).** Cadeia com *fallback* e
*rate limiting* por provedor, todos em tier gratuito:

| Provedor | Limite grátis | Papel na cadeia |
|---|---|---|
| Groq | 30 RPM · 1 000 req/dia · 200k tokens/dia | rápido (Qwen/LLaMA) e raciocínio (GPT-OSS) |
| Gemini AI Studio | 10 RPM · 250 req/dia | contexto longo |
| OpenRouter | 20 RPM · 50 req/dia (1 000 após upgrade $10) | reserva |
| Ollama | ilimitado (local) | zero-latência, offline |
| Heurístico local | ilimitado | **último recurso determinístico** |

**2. Agentes declarativos (`backend/config/agents.yaml` + `tasks.yaml`).**
11 agentes (Scout, Validador, Vídeo, Infoprodutos, Micro-SaaS, Juiz, CFO,
Contabilista, RH, Jurídico, CEO) e 15 tarefas com gates
(`kill_switch_clear`, `cemetery_check`, `smoke_test_passed`, `eval_min_85`,
`fail_closed`, `approval_approved_and_biometric`). A ponte
`backend/core/crew_bridge.py` lê-os em runtime, valida referências, tiers e
ciclos, e executa as tarefas.

**3. CrewAI (MIT), opcional mas suportado a sério.** Se `crewai` estiver
instalado, é construído um `BaseLLM` que **delega no nosso roteador** — o
CrewAI nunca instancia LiteLLM nem gasta dinheiro. Sem CrewAI, as mesmas
tarefas correm no caminho nativo com o mesmo prompt de sistema.

### Verificar em 5 segundos

```bash
curl -s localhost:8000/agents/probe | jq '.conclusion, .results'
# ["IA ligada via groq (tier gratuito, custo $0.00)."]
curl -s localhost:8000/agents | jq '.validation'
curl -X POST localhost:8000/agents/run -H "X-Automaton-Token: $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"task_id":"scan_market","inputs":{"niche":"retalho"}}' | jq '.is_llm'
```

O campo `is_llm` (e `engine` nas entregas das squads) diz sempre a verdade:
`false` = motor heurístico local determinístico; `true` + `provider` = modelo
real respondeu. Sem chaves o sistema **não falha** — degrada e avisa.

### 4. Média gerado por IA aberta + pesquisa real

Só modelos com licença utilizável comercialmente, todos a **$0.00** (pesos
abertos em hardware próprio). A auditoria completa está em
[`docs/stack_audit.md`](docs/stack_audit.md).

| Função | Motor | Licença | Onde vive |
|---|---|---|---|
| Vídeo (texto→vídeo) | **Wan 2.2** (5B, 8 GB VRAM) | Apache-2.0 | `squads/genai_server.py` |
| Vídeo rápido/longo | **LTX-Video**, **LongCat-Video** | Apache-2.0 / MIT | idem |
| Imagem | **Kandinsky 6.0**, SDXL / SD 3.5 | MIT / Stability | idem |
| Voz | edge-tts, **Kokoro-82M**, Chatterbox, Piper | LGPL / Apache-2.0 / MIT / MIT | `squads/video_factory_squad.py` |
| Pesquisa | **Crawl4AI**, **GPT-Researcher**, extractor nativo | Apache-2.0 | `squads/research_backends.py` |
| Pagamentos | **`paymentsds-mpesa`** (SDK oficial MZ) | Apache-2.0 | `finance/payout_bridge.py` |
| B-roll de reserva | Pexels, Pixabay, fundo procedimental | Pexels/Pixabay ToS | `squads/video_factory_squad.py` |

**Ordem de decisão no footage** (`FootageProvider.fetch`): IA local → Pexels →
Pixabay → fundo procedimental. Cada geração leva proveniência
(`backend`, `license`, `prompt`, `seed`, `latency_ms`) e é auditada pelo
`legal_compliance.audit_media` antes de entrar no render. Quando cai para banco
de imagens, fica um `WARNING` explícito no log — nunca se faz passar stock por
IA.

```bash
# Que motores estão prontos, agora (nada é inventado)
curl -s localhost:8000/platform/stack | jq '.video.generative_backends.ready,
                                            .research.active, .payments.mpesa.sdk'
# → []  "native"  "paymentsds-mpesa"     ($0.00: ainda sem endpoints/GPU)

# Pesquisa profunda real (escudo anti-injection aplicado ao texto)
curl -s -X POST localhost:8000/platform/research \
     -H 'Content-Type: application/json' \
     -d '{"query":"gestão de inventário retalho Moçambique","max_results":3}' | jq '.engine,.results|length'

# Ligar a geração por GPU (ComfyUI/SDXL/…) e provar com 1 frame
docker compose --profile gpu_video up -d genai_video
export GENAI_VIDEO_ENABLED=true WAN22_ENDPOINT=http://localhost:8188/generate
curl -s -X POST localhost:8000/platform/genai/probe -H "X-Automaton-Token: $TOKEN" | jq
```

Sem GPU o endpoint `/generate` responde **503 com o motivo**
(`diffusers` em falta / sem ComfyUI) e o sistema usa stock — é degradação
declarada, não simulação.

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
# stack de IA aberta
GET  /platform/stack · /platform/genai
POST /platform/research          (público, só leitura)
POST /platform/genai/probe       (token; gera 1 frame de teste)
# tempo real
WS   /ws/live        (eventos + snapshot + heartbeat)
```

Documentação interactiva: **`/docs`**.

---

## 🧪 Testes

```bash
.venv/bin/python -m pytest backend/tests -q     # 38 testes, ~1.5 s
```

Cobrem: token de escrita, as três fases, retenção fiscal de 5%, teto de gasto
zero, Juiz Eval (reprova/aprova), kill switch a abortar o ciclo, smoke test
com demanda real, landing page pública, biometria obrigatória (428), ciclo
completo do CEO, bloqueio pelo cemitério, metadados de licença dos backends de
vídeo, `503` honesto do servidor de geração sem GPU, escudo anti-injection no
resumo de pesquisa e o caminho SDK do M-Pesa (B2C via `paymentsds`, com
cliente falso, mais o *dry-run* quando faltam credenciais).

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
