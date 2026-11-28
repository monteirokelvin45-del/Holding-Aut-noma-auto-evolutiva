# Onde colocar as chaves dos LLM

Sem chaves, os 11 agentes correm no **motor local** (regras determinísticas):
produzem e-book e vídeo, mas as ideias saem genéricas — foi o que se viu no
ciclo de 28/11, com o Scout a copiar títulos de repositórios do GitHub
trending («Versão paga/prática de: erkamyaman/rust-anything»).

Com **uma** das duas chaves abaixo o raciocínio passa a ser real.

---

## Caminho A — GitHub Actions (é aqui que o ciclo corre)

1. Abrir o repositório no GitHub.
2. **Settings** (no topo do repositório, não o das definições da conta).
3. No menu lateral: **Secrets and variables** → **Actions**.
4. Botão verde **New repository secret**.
5. Preencher:

| Name (exacto, sensível a maiúsculas) | Value |
|---|---|
| `GROQ_API_KEY` | a chave `gsk_...` |
| `GEMINI_API_KEY` | a chave `AIza...` |

6. **Add secret**, uma vez por chave.

Não é preciso mexer em mais nada: `.github/workflows/02_build_android_apk.yml`
já passa as duas ao ciclo (`GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}` e
`GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}`). O código lê-as de
`backend/config/settings.py` (`groq_api_key`, `gemini_api_key`).

**Ordem de tentativa** (`LLM_PROVIDER_ORDER` no workflow):

```
groq → gemini → github_models → openrouter → local
```

### Como obter as chaves (ambas free-tier, sem cartão)

- **Groq** — <https://console.groq.com/keys> → *Create API Key*. Limite
  gratuito: ~30 pedidos/minuto.
- **Google Gemini** — <https://aistudio.google.com/apikey> → *Create API key*.
  Limite gratuito de `gemini-2.5-flash`: ~10 pedidos/minuto.

### Como confirmar que funcionou

Depois de guardar o segredo, correr o workflow:

```
Actions → 02 · Build Android APK → Run workflow → base_url = http://10.0.2.2:8000 → ciclo = true
```

No fim, o ramo `estado-ciclo` fica com `sonda_llm.json`. O que interessa:

```json
{ "escolhido": "groq", "is_llm": true, "cadeia": ["groq:used"] }
```

Se continuar `"escolhido": "local"`, o campo `erro` diz exactamente porquê
(HTTP 401 = chave errada; HTTP 400 = nome de modelo; rede = DNS).

---

## Caminho B — máquina local (`.env`)

Na raiz do repositório, criar/editar `.env`:

```bash
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=AIza...
LLM_PROVIDER_ORDER=groq,gemini,github_models,openrouter,local
```

O `.env` está no `.gitignore` — não vai para o repositório. Para experimentar
sem servir o backend:

```bash
PYTHONPATH=. python3 -c "
from backend.core.llm_router import llm_router, LLMRequest
r = llm_router.complete(LLMRequest(prompt='Diz OK', max_tokens=16))
print(r.provider, r.is_llm, r.text)
"
```

---

## GitHub Models: não conta

Foi tentado como terceira opção porque usa o `GITHUB_TOKEN` que já existe.
Medido no runner (`sonda_rede.txt`, run `46135545055`) — **o mesmo corpo `OK`
para tudo**:

| Pedido | Resposta |
|---|---|
| modelo `gpt-4o-mini` | `200 text/plain` → `OK` |
| modelo `gpt-4.1-mini` | `200 text/plain` → `OK` |
| modelo **inexistente** | `200 text/plain` → `OK` |
| caminho **inválido** | `200 text/plain` → `OK` |
| corpo **que não é JSON** | `200 text/plain` → `OK` |

Um LLM real responde de forma diferente a prompts diferentes; um servidor
real devolve 404 a um caminho inválido e 400 a um corpo inválido. Este host
devolve sempre o mesmo `OK`: é um proxy de egress a fingir de endpoint. O
provider continua na cadeia, mas rejeita essa resposta explicitamente
(`backend/core/llm_router.py`) em vez de a dar como texto de modelo.

O outro endpoint, `models.inference.ai.azure.com`, nem DNS tem no runner.

---

## Modelos usados

| Provider | Modelo (por omissão) | Nota |
|---|---|---|
| Groq | `openai/gpt-oss-120b` (raciocínio), `qwen/qwen3-27b` (rápido) | O nome curto `gpt-oss-120b` dá HTTP 400; o prefixo de autor é obrigatório |
| Gemini | `gemini-2.5-flash` | Free-tier |

Se a Groq retirar um modelo, o provider tenta automaticamente
`llama-3.3-70b-versatile` e `llama-3.1-8b-instant` antes de desistir — um 400
de nome de modelo já não parece uma chave inválida.
