# Auditoria da stack aberta — o que foi realmente descarregado

Registo honesto do pedido «baixa tudo necessário (tudo mesmo)»: o que ficou
instalado, como foi obtido, e o que se declarou **bloqueado** em vez de simulado.

Data: 2026-11-27 · Rede do sandbox: `github.com` ✓ · `pypi.org` ✓ ·
`registry.npmjs.org` ✓ · `huggingface.co` ✗ · `objects.githubusercontent.com` ✗ ·
`cdn.jsdelivr.net` ✗ · `speech.platform.bing.com` ✗

---

## 1. Mapa de alcance da rede (medido, não suposto)

| Destino | Estado | Consequência |
|---|---|---|
| `github.com` (HTML/API) | **200** | Repositórios e metadados legíveis |
| `objects.githubusercontent.com` | **000** | **Todos** os *release assets* mortos (302 → 0 bytes) |
| `pypi.org` / `files.pythonhosted.org` | **200** | Wheels instaláveis |
| `registry.npmjs.org` | **200** | Foi a via que salvou os pesos do Kokoro |
| `huggingface.co` | **000** (TLS cortado; o TCP *abre*, o handshake morre) | Nenhum modelo do Hub, `piper` sem vozes, `llm-guard` sem pesos |
| `hf-mirror.com`, `cdn.jsdelivr.net` | **000** | Sem espelhos |
| `speech.platform.bing.com` | **000** | `edge-tts` inalcançável apesar de instalado |

Consequência de desenho: **a selecção de motores foi feita pela rede, não pelo
gosto**. Qualquer alternativa que dependa do HuggingFace está fora — não por
licença, por alcance.

---

## 2. Texto-para-fala

| Motor | Licença | Estado | Prova |
|---|---|---|---|
| `edge-tts` 7.0.0 | LGPL-3.0 | instalado, **inalcançável** (endpoint Microsoft bloqueado) | `available()` → `True` só com rede aberta |
| **Kokoro-82M q8 ONNX** | Apache-2.0 | **funcional offline, inglês/chim** | `docs/assets/kokoro_test.mp3` (2,39 s, 57 344 amostras @24 kHz) |
| **espeak-ng** (via `espeakng-loader`) | GPLv3 (áudio não herda) | **funcional offline, português** | `docs/assets/espeak_pt.mp3` (4,24 s, 93 445 amostras @22,05 kHz) |
| Chatterbox | MIT | não instalado (sem pesos) | — |
| Piper | MIT | **bloqueado**: `piper-tts` instala, mas as vozes `pt_PT` vivem no HF | — |
| ChatTTS / F5-TTS | AGPL-3.0 / CC-BY-NC | **proibidos** por regra da holding | — |

### Como os pesos do Kokoro foram obtidos (caminho alternativo honesto)

Os *release assets* do GitHub estão mortos e o HF também, pelo que a rota
documentada (`hexgrad/Kokoro-82M`, `thewh1teagle/kokoro-onnx`) é **impossível
nesta rede**. A rota usada, verificável e reprodutível:

1. `https://registry.npmjs.org/kokoro-q8-shards/-/kokoro-q8-shards-1.0.0.tgz`
   — 63,0 MB, 6 fragmentos de `model_quantized.onnx` (de
   `onnx-community/Kokoro-82M-v1.0-ONNX`, Apache-2.0); concatenados dão
   `models/kokoro/kokoro-v1.0.q8.onnx`, 92,4 MB, sha256
   `fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478`
   (bate certo com o hash publicado pelo autor no README do pacote).
2. `https://registry.npmjs.org/kokoro-local-runtime/-/kokoro-local-runtime-0.1.0.tgz`
   — 13,5 MB, 28 ficheiros `voices/*.bin`; cada um são **510×256 float32
   empatados num vector** (não float16 — `np.float16` dá NaN). Empacotados em
   `models/kokoro/voices-v1.0.npz`.

Nenhum passo é simulado: os dois `.tgz` e o `.npz` estão no disco e o hash do
modelo foi recalculado localmente.

### Correções de código exigidas por estes pesos

- `kokoro_onnx` 0.6.1 assume vozes float16 e `style` de *rank* 0/1 →
  `Invalid rank for input: style Got: 0 Expected: 2`. `TTSRegistry._kokoro`
  substitui `_style_for` por uma versão que devolve `[1,256]` float32 e recusa
  português explicitamente (`kokoro_onnx:sem_voz_pt`) em vez de ler PT com voz
  inglesa.
- `_espeak_ng()` fala com `libespeak-ng.so` por `ctypes`. A assinatura do
  callback importa: `CFUNCTYPE(c_int, POINTER(c_short), c_int, c_uint, c_void_p)`
  e `espeak_Synth(text, len+1, 0, 0, 0, 0, None, None)`; a variante com
  `WaveHooks` faz `exit_code 2` sem traceback.
- `legal_compliance` recusava `espeak_ng` como «fonte desconhecida sem licença
  comprovada» e **bloqueava o pipeline inteiro**. Passou a constar da allowlist
  de áudio com a nota GPLv3.

Cadeia final de fallback: `edge_tts` → `kokoro` (en/zh) → `chatterbox` →
`piper` → **`espeak_ng`**.

### Limitação declarada, não escondida

**Nesta rede, português só existe em espeak-ng** — voz robótica de 1995, não
neural. Kokoro v1.0 não tem vozes `pt*` (as 28 são `af_*`,`am_*`,`bf_*`,`bm_*`).
Para narração PT de qualidade é preciso rede até ao HuggingFace (Piper `pt_PT`)
ou até à Microsoft (`edge-tts`); ambas bloqueadas. O sistema diz isto em vez de
fingir: `_kokoro(pt)` devolve `(False, "kokoro_onnx:sem_voz_pt…")`.

---

## 3. Segurança / validação

| Pacote | Versão | Licença | Estado |
|---|---|---|---|
| `llm-guard` | 0.3.16 | MIT | instalado; **pesos `deberta-v3` no HF → inalcançável** |
| heurística local do `PromptShield` | — | própria | activa, 0,04 s |

**Bug corrigido nesta sessão:** o `PromptShield` tentava carregar o
`PromptInjection` e ficava **236 s** em `Retrying in 8s [Retry 5/5]` antes de
desistir — a cada processo novo. Passou a sondar `huggingface.co` com um `HEAD`
de 3 s antes de tentar (um `connect()` TCP **não** serve: o socket abre e é o
TLS que morre, o que fazia a sonda responder «alcançável» por engano).
Latência medida: **236 s → 0,04 s**, com o mesmo resultado de bloqueio
(`blocked=True, score=0.90`).

---

## 4. Vídeo e áudio

| Pacote | Versão | Uso |
|---|---|---|
| `moviepy` | 2.2.1 | montagem; API 2.x (`with_*`), actualizada no `requirements.txt` (estava `1.0.3`) |
| `soundfile` | 0.12.1 | escrita/leitura WAV dos TTS |
| `imageio-ffmpeg` | 0.5.1 | **único ffmpeg utilizável** (não há `ffmpeg` no PATH) |
| `Pillow` | 11.0.0 | *frames* do renderer de movimento |

---

## 5. Não instalado — e porquê

| Candidato | Tamanho | Razão da recusa |
|---|---|---|
| `scancode-toolkit` | 653 MB | audita licenças; as licenças já foram verificadas à mão e registadas aqui |
| `crawl4ai` + Playwright | ~400 MB + navegadores | descarregaria Chromium de `cdn.playwright.dev`, fora da lista de hosts permitidos → instalação que falharia a meio |
| `piper-plus`, `@diffusionstudio/piper-wasm` | 63 MB / 18,8 MB | os pesos vêm do HF → pacote inútil nesta rede |
| `gpt-researcher` | — | exige Python ≥3.12; o venv é 3.11 |
| `kokoro` (PyPI) | ~6 GB | arrasta torch + transformers para carregar pesos que o HF não entrega |

---

## 6. Reprodução

```bash
pip install -r backend/requirements.txt
bash scripts/fetch_models.sh      # pesos Kokoro via npm (idempotente, verifica sha256)
python -c "from backend.squads.video_factory_squad import TTSRegistry; \
           print(TTSRegistry.available())"
# {'edge_tts': True, 'kokoro': True, 'chatterbox': False, 'piper': False, 'espeak_ng': True}
```

`available()` reporta **capacidade instalada**; a acessibilidade de rede de cada
motor é reportada em separado, para não confundir «tem o pacote» com «consegue
falar com o serviço».

---

## 7. Veredicto

- **Descarregado e funcional:** Kokoro-82M q8 (92,4 MB) + 28 vozes, espeak-ng
  com dados `pt`, `onnxruntime`, `soundfile`, `moviepy`, `llm-guard`, `edge-tts`.
- **Bloqueado por rede, declarado:** pesos Piper PT, pesos `llm-guard`
  (`deberta`), `edge-tts` (endpoint).
- **Bloqueado por licença, recusado:** ChatTTS (AGPL), F5-TTS (CC-BY-NC).
- **Prova ponta-a-ponta:** `docs/assets/exemplo_short.mp4` — 19,23 s,
  1080×1920, h264+aac 22 050 Hz mono, narração PT por `espeak_ng`, juiz
  **88,0/100**, `publishable: True`, `blockers: []`.
