# Conformidade com a especificação técnica (entrega a entrega)

Mapa honesto entre os artigos 5.x do mandato e o que existe neste repositório.
Só há três estados possíveis: **cumpre**, **cumpre com nome diferente**
(alias documentado) e **lacuna** (com o que falta, sem promessas).

---

## ENTREGA 1 — Infraestrutura e núcleo blindado

### 5.1 `backend/core/llm_router.py` — roteador com rate limit

| Requisito | Estado | Onde |
|---|---|---|
| Contador RPM 30 / RPD 1 000 / TPD 200 000 | cumpre | `RateLimiter.can_call` + `ProviderSpec` |
| Contador em memória **e** disco | cumpre | `RateLimiter._persist` → `vault_data/runtime/llm_usage.json` |
| Modelos `gpt-oss-120b` / `qwen3.8-27b` | cumpre | `settings.groq_model_fast` / `groq_model_reasoning` |
| Mudar de rota aos **85%** do limite da Groq | **cumpre (novo)** | `RateLimiter.headroom()` + `settings.llm_failover_threshold=0.85` |
| Nunca parar por 429 (Gemini → OpenRouter → Ollama → local) | cumpre | cadeia em `LLMRouter.complete` |
| Nunca gastar um cêntimo sem permissão | cumpre | `settings.daily_spend_cap_usd=0.0` + `SpendGuard` |

O failover preventivo é a peça que faltava: antes só se mudava de provedor
*depois* do limite rebentar. Agora, a 85% do RPM/RPD/TPD mais apertado, o
provedor é abandonado com `provider:skip(quota_86%)` na cadeia de fallback —
a quota restante fica reservada para tarefas críticas.

```python
from backend.core.llm_router import llm_router
r = llm_router.complete(pedido)
r.fallback_chain   # ['groq:skip(quota_86%)', 'gemini:try']
```

### 5.2 `backend/core/security_guard.py` — escudo e travas

| Requisito | Estado | Onde |
|---|---|---|
| `sanitize_input(raw_text: str)` com llm-guard | **cumpre (novo)** | fachada sobre `PromptShield.scan` |
| `verify_execution_permission(agent, custo)` | **cumpre (novo)** | fachada sobre `SpendGuard.authorize_spend` |
| 1) kill switch interrompe o processo | cumpre | `KillSwitchActive` |
| 2) `gasto_dia + estimado > max_daily_cap` | cumpre | `SpendCapExceeded` |
| 3) `> $5.00` → grava `pending_approvals.json` + push APK/Telegram + biometria | cumpre | `ApprovalRequired` com `approval_id` |
| Scanners PromptInjection / Secrets / Toxicity | cumpre | `llm_guard` quando instalado; heurística local quando não |

Detalhe importante: **custo `$0.00` é aprovado**, não é erro. Nesta holding o
normal é free-tier; `verify_execution_permission(agent, 0.0)` valida o kill
switch e devolve autorização sem consumir orçamento. Um custo positivo
esbarra primeiro no tecto diário ($0.00 por omissão) e só depois no limiar
biométrico — exactamente a ordem do mandato.

### 5.3 `cfo_treasury.py` e `payout_bridge.py`

| Requisito | Estado | Onde |
|---|---|---|
| `process_incoming_revenue(gross_usd, squad_id)` | **cumpre (novo alias)** | delega em `record_revenue` |
| `tax_usd = round(gross * 0.05, 2)` somado a `tax_reserve_usd` | cumpre | `record_revenue` |
| `net_usd` somado a `monthly_revenue_usd` | cumpre | `monthly_revenue` |
| Fases 10/20/70 → 10/40/50 → 50/30/20 | cumpre | `PHASES`, `split_for_phase` |
| Fronteiras 1 500,00 e 15 000,00 | **corrigido** | `phase_for_monthly_revenue` (limite superior inclusivo) |
| Ledger atómico e thread-safe | cumpre | `vault.transaction` + `RLock` |
| `MozambiquePayoutBridge` / `withdraw_to_mpesa(...)` | **lacuna** | ver abaixo |
| SDK `paymentsds-mpesa` para `api.vm.co.mz` | cumpre | `MPesaConnector._payout_with_sdk` |

**Duas notas de honestidade:**

1. O mandato diz `from mpesa import Client`. O import correcto do pacote
   `paymentsds-mpesa` é `from paymentsds.mpesa import Client` — `mpesa` é um
   pacote diferente (vazio no PyPI). O código suporta os dois caminhos.
2. `api.vm.co.mz` é o **produção**; o sandbox é `api.sandbox.vm.co.mz`.
   `MPESA_BASE_URL` controla qual se usa; o código não força produção.
3. Os nomes `MozambiquePayoutBridge`/`withdraw_to_mpesa` não existem: o
   equivalente é `MPesaConnector.payout(PayoutRequest(...))`, com conversão
   USD→MZN por `cfo.convert_usd_to_mzn` (taxa `USD_MZN_REFERENCE=63.50`) e
   `amount_usd` no pedido. Em vez de criar uma segunda porta de saída de
   dinheiro (dois caminhos = duas auditorias), mantém-se um só, já coberto
   por testes e pelo gate biométrico. Se quiseres o nome do mandato como
   alias fino, é uma alteração de 15 linhas — diz e faço.

### 5.4 `demand_validator.py` e `eval_judge.py`

| Requisito | Estado | Onde |
|---|---|---|
| Consulta o cemitério antes de validar | cumpre | `shared_memory.check_against_cemetery` |
| Gera página HTML de pré-venda e mede clicks/signups/conversão | cumpre | `create_smoke_test` + `record_event` + `evaluate` |
| Falha → grava o nicho no `post_mortem_memory.json` | cumpre | `record_failure` no ciclo |
| Juiz com 4 critérios de 0 a 25 | **cumpre (novo)** | `AXIS_MAX_POINTS`, `AXIS_LABELS` |
| Soma 0–100; `< 85` bloqueia e devolve feedback | cumpre | `eval_judge.enforce` |
| Aceita caminho `.mp4` / ebook / código | **cumpre (novo)** | `EvalJudge._read_artifact` + `_probe_video` |

Mapeamento dos eixos (nomes internos preservados para não quebrar APK/testes):

| Mandato | Chave interna |
|---|---|
| Gancho / Retenção Inicial | `hook` |
| Qualidade Técnica / Áudio / Código | `technical` |
| Potencial de Monetização / SEO | `clarity` |
| Segurança de Direitos de Autor | `compliance` |

O `to_dict()` do veredicto passou a expor `points` (0–25 por eixo) e
`axis_labels`, para o APK e os relatórios mostrarem a pontuação do mandato.

### 5.5 `video_factory_squad.py`

| Requisito | Estado |
|---|---|
| Roteiro viral (Hook + Corpo + CTA) via `llm_router` | cumpre |
| Kokoro → Chatterbox → edge-tts | cumpre (`TTSRegistry.synthesize`) |
| Vozes `pt-BR-Antonio`, `pt-PT-Duarte`, `en-US-Christopher` | cumpre (`TTS_VOICE_PT`/`EN`, configurável) |
| Pexels/Pixabay verticais HD | cumpre |
| Wan 2.2 / LongCat / LTX por API | cumpre (`ai_video_backends`) |
| Render 1080×1920 com legendas sincronizadas | cumpre |
| `youtube-copyright-scanner` + `eval_judge` antes da aprovação | cumpre (`audit_media` + juiz) |
| Integração com a **API do MoneyPrinterTurbo** | **lacuna** |

O `MONEYPRINTER_BASE_URL` existe na configuração e o contentor está no
`docker-compose`, mas o pipeline **não** chama a API do MoneyPrinterTurbo:
o caminho nativo (motion graphics + Pexels + ffmpeg) é o que produz. Chamar
uma API que não está a correr daria a ilusão de integração; está por fazer e
assim declarado.

---

## O que esta entrega mudou de facto

1. `llm_router.py`: `RateLimiter.headroom()` + failover preventivo a 85%
   (`LLM_FAILOVER_THRESHOLD`), com a cadeia a registar o motivo do salto.
2. `security_guard.py`: `sanitize_input()` e `verify_execution_permission()`
   como contrato público (com `spend_guard` singleton e custo zero aceite).
3. `cfo_treasury.py`: `process_incoming_revenue()` e a fronteira dos 15 000,00
   a pertencer à Fase 2, como o mandato escreve.
4. `eval_judge.py`: eixos de 0–25 expostos, rótulos do mandato e medição real
   de ficheiros `.mp4` (duração, vertical, presença de áudio).
5. `test_ai_integration.py`: corrigido o `SettingsShim` que entrava em
   recursão infinita quando o router passou a ler um campo novo.

**Testes: 57 verdes** (52 antes desta entrega + 5 do contrato 5.2).
