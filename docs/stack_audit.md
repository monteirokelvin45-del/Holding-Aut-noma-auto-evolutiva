# Auditoria da stack de IA aberta

Verificação feita a **27 de novembro de 2026** contra `api.github.com` e
`pypi.org/pypi/<pacote>/json`. Regra da holding: **MIT / Apache-2.0 / BSD /
LGPL** apenas; **CC-BY-NC e AGPL ficam de fora**; nada pode custar dinheiro.

## 1. Vídeo e imagem

| Projecto | Estrelas | Licença | Decisão | Onde é usado |
|---|---:|---|---|---|
| [Wan-Video/Wan2.2](https://github.com/Wan-Video/Wan2.2) | 18 975 | Apache-2.0 | **Adoptado** (5B cabe em 8 GB VRAM) | `genai_server.py`, backend `wan22` |
| [meituan-longcat/LongCat-Video](https://github.com/meituan-longcat/LongCat-Video) | 9 779 | MIT | **Adoptado** | backend `longcat` |
| [Lightricks/LTX-Video](https://github.com/Lightricks/LTX-Video) | 11 514 | Apache-2.0 | **Adoptado** (grátis até US$10M de receita anual) | backend `ltx` |
| [Stability-AI/generative-models](https://github.com/Stability-AI/generative-models) | — | MIT (código); pesos com Stability Community License | **Adoptado com nota** (comercial até US$1M/ano) | backends `sdxl`, `sd35` |
| [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) | 145 971 | MIT | **Adoptado** | `docker-compose` serviço `moneyprinter` |
| [RayVentura/ShortGPT](https://github.com/RayVentura/ShortGPT) | — | MIT | **Adoptado (opcional)** | `SHORTGPT_ENABLED` |
| AI-Youtube-Shorts-Generator | — | MIT | **Adoptado (opcional)** | `YOUTUBE_SHORTS_GENERATOR_ENABLED` |
| HunyuanVideo | — | licença com limites territoriais | **Rejeitado** | — |

Kandinsky 6.0 (MIT) entra pela API de `diffusers`
(`kandinsky-community/kandinsky-3` → fallback `kandinsky-2-2-decoder`).

## 2. Voz

| Projecto | Estrelas | Licença | Decisão |
|---|---:|---|---|
| [hexgrad/kokoro](https://github.com/hexgrad/kokoro) | 10 408 | Apache-2.0 (código **e** pesos) | **Adoptado** |
| [resemble-ai/chatterbox](https://github.com/resemble-ai/chatterbox) | 28 160 | MIT (marca de água de voz) | **Adoptado** |
| [rhasspy/piper](https://github.com/rhasspy/piper) | — | MIT (o repo reporta MIT; binários históricos GPLv3) | **Adoptado só como CLI** |
| rany2/edge-tts | — | LGPL-3.0 | **Adoptado** (motor por omissão) |
| [2noise/ChatTTS](https://github.com/2noise/ChatTTS) | 40 043 | **AGPL-3.0** | **Proibido** |
| F5-TTS | — | **CC-BY-NC** | **Proibido** |

`backend/governance/legal_compliance.py` mantém `chattts`/`f5-tts` na lista de
pacotes proibidos, verificado por teste.

## 3. Pesquisa

| Projecto | Estrelas | Licença | Decisão |
|---|---:|---|---|
| [unclecode/crawl4ai](https://github.com/unclecode/crawl4ai) | 88 040 | Apache-2.0 | **Adoptado** (1.º na cadeia) |
| [assafelovic/gpt-researcher](https://github.com/assafelovic/gpt-researcher) | 30 959 | Apache-2.0 | **Adoptado** (2.º) |
| Extractor nativo (`urllib` + API JSON) | — | próprio | **Adoptado** (3.º, sem dependências) |

Versões PyPI verificadas: `crawl4ai 0.9.6` (o `requirements.txt` fixa
`0.4.247`, que existe e é estável), `gpt-researcher 0.17.0`.

## 4. Agentes e observabilidade

| Projecto | Estrelas | Licença | Decisão |
|---|---:|---|---|
| [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | 61 328 | MIT | **Adoptado (opcional)** — delega no nosso roteador |
| [FoundationAgents/MetaGPT](https://github.com/FoundationAgents/MetaGPT) | — | MIT | Não usado (sobreposição com CrewAI) |
| [protectai/llm-guard](https://github.com/protectai/llm-guard) | — | MIT | **Adoptado** (escudo anti-injection) |
| [langfuse/langfuse](https://github.com/langfuse/langfuse) | — | NOASSERTION (MIT no core) | **Adoptado, self-hosted**, sem SDK obrigatório |

## 5. Pagamentos

| Pacote / projecto | Versão | Licença | Decisão |
|---|---|---|---|
| [paymentsds/mpesa-python-sdk](https://github.com/paymentsds/mpesa-python-sdk) (`paymentsds-mpesa`) | 0.1a10 | Apache-2.0 | **Adoptado** — SDK oficial M-Pesa Moçambique |
| `mpesa-python-sdk` (Safaricom Ethiopia/Kénia) | 1.0.0 | MIT | Suportado como **2.º** caminho (`mpesa.B2C`) |
| `mpesa` (PyPI) | 0.0.1 | — | Ignorado (pacote vazio) |
| `mpesa-sdk` (Daraja Kénia) | 2.0.0 | não declarada | Rejeitado (licença ausente) |
| `pympesa` | 1.0.1 | MIT | Não necessário |

**Bug corrigido nesta auditoria:** o código importava `from mpesa.api.b2c import
B2C` — esse módulo **não existe** em nenhum dos pacotes reais, pelo que o
caminho "SDK" era silenciosamente morto e caía sempre no HTTP manual. A API
real do `paymentsds` é:

```python
from paymentsds.mpesa import Client
client = Client(api_key=..., public_key=..., service_provider_code=...,
                origin="+258840000000", host="https://api.sandbox.vm.co.mz:18352")
client.send({"to": "+258840000000", "amount": 635.0,
             "reference": "AUTO123", "third_party_reference": "AUTO123"})
```

Notas do SDK (verificadas no código instalado):
* `environment="sandbox"` **rebenta** (`IndexError`) — usar `host=` completo
  ou `Environment.SANDBOX` (`https://api.sandbox.vm.co.mz`).
* A operação é detectada pelo formato de `to`: telemóvel → `B2C_PAYMENT`,
  *service provider code* → `B2B_PAYMENT`.
* Resposta traz `output_ResponseCode`; sucesso é `INS-0`.

## 6. Ferramentas rejeitadas por falta de credibilidade

* **`licscan`** — não existe no PyPI; o repositório homónimo tem 1 estrela.
  A auditoria de licenças é feita internamente em
  `backend/governance/legal_compliance.py`.
* `scancode-toolkit` — mantido, mas com o pin real `32.5.0` (o `32.3.5`
  anterior não existia).

## 7. Como re-verificar

```bash
# licenças e estrelas
curl -s https://api.github.com/repos/Wan-Video/Wan2.2 | jq '.license.spdx_id, .stargazers_count'
# versões reais no PyPI
for p in paymentsds-mpesa crawl4ai gpt-researcher kokoro llm-guard; do
  printf '%s: ' "$p"; curl -s "https://pypi.org/pypi/$p/json" | jq -r '.info.version'
done
```

Um pin que não exista no PyPI impede a instalação de *toda* a imagem Docker;
por isso `backend/requirements.txt` é validado pin a pin nesta auditoria.

---

## 8. Correções de 27/nov/2026 (segunda passagem)

Três problemas reais encontrados a correr o sistema, e o que mudou.

### 8.1 Pesquisa: uma fonte morta e ruído de baixa precisão

| Sintoma | Causa | Correção |
|---|---|---|
| 0 oportunidades no ciclo | `hn.algolia.com` falha por TLS em redes restritas e era a 1.ª fonte | HN removido; ordem agora é GitHub Issues → GitHub Repos → npm |
| 1 página por consulta | só o GitHub Repos respondia | npm (`registry.npmjs.org/-/v1/search`) devolve resultados reais |
| 0 resultados com acentos | a API é literal: `inventário` não indexa | `_query_terms` normaliza (NFKD) e remove *stopwords* |
| 9 oportunidades irrelevantes | recurso com `sort=reactions` + termo único devolvia issues ultra-votadas de temas alheios | esse URL foi **removido** (0% de precisão) e a relevância passou a ser exigida |

Sinais passaram a ser legíveis por máquina: cada linha termina em
`sinal: stars=.. points=.. comments=.. downloads=..`. O Scout lê daí, em vez de
adivinhar números em prosa. Os downloads npm são convertidos para escala
comparável (~1 estrela ≈ 250 downloads/semana).

Resultado medido: `inventory stock management retail` → 15 sinais, 12
relevantes, feed 100% dentro do nicho (issues de inventário + pacotes npm).

### 8.2 Vídeo sem GPU: motion graphics próprios

O caminho anterior sem GPU era um gradiente abstracto — não é vídeo
publicável. Novo módulo `backend/squads/motion_renderer.py`:

* arte por plano com Pillow: gradiente diagonal, brilho em *screen*, vinheta,
  paleta diferente por plano;
* texto com quebra de linha **medida em píxeis** (máx. 4 linhas, fonte reduz
  até caber), sombra dupla e barra de progresso;
* `zoompan` lento + fades + `concat` sem recodificar;
* licença `own_work`; sem GPU, sem downloads, sem fontes externas.

Bugs apanhados no caminho (todos corrigidos e cobertos por teste):

1. `color`/`geq` são *source filters*: dentro de `-vf` o ffmpeg rejeita
   ("expected 1 input"). Passou a `-filter_complex` com rótulos.
2. `Image.blend` com camada preta **escurecia** em vez de iluminar; a vinheta
   estava invertida e apagava a arte. Agora `ImageChops.screen` + máscara
   correcta.
3. O texto era queimado pelo motion **e** legendado pelo SRT em simultâneo —
   duas camadas ilegíveis. `footage.has_text=True` desliga o SRT.
4. O `geq` desta build do ffmpeg produzia barras de cor em vez de gradiente;
   o fundo passou a ser desenhado em Pillow.

### 8.3 Áudio: honestidade em vez de silêncio disfarçado

O `edge-tts` precisa de rede até `speech.platform.bing.com`. Offline, o vídeo
saía com faixa silenciosa e o pacote dizia "pronto". Agora:

* `publishable` = render **e** narração **e** compliance;
* `publish_blockers` lista o que falta (`sem_narracao`, `render_indisponivel`,
  `compliance_falhou`);
* `next_step` do pacote passa a "BLOQUEADO: resolver … antes de publicar".

Para narração offline real: `kokoro` (Apache-2.0, pesos locais) ou `piper`.
Nenhum dos dois está instalado nesta imagem — está declarado, não simulado.

### 8.4 Estado verificado do pipeline completo

```
smoke test aprovado (40 cliques, 22.5% conversão)
  -> guião gerado -> footage motion_graphics (own_work)
  -> render 1080x1920 30fps + áudio AAC, 19.2 s
  -> juiz 88.0/100 (APROVADO)
  -> compliance ok -> publishable: True
```

Ficheiro de prova: `vault_data/media/videos/<squad>/<data>/short.mp4`.
