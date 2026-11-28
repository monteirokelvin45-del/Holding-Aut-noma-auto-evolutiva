# Arquitectura 100% local — zero chaves, zero custo, zero limite

O ciclo da holding corre **sem chaves nenhumas** e produz raciocínio real.
Este documento regista o que foi medido, o que falhou e porquê — incluindo a
arquitectura que se tentou e **não** foi possível.

---

## Resultado, no GitHub Actions

Run `46138893970` em diante, ramo `estado-ciclo`, ficheiro `sonda_llm.json`:

```json
{
  "escolhido": "local_gguf",
  "is_llm": true,
  "cadeia": ["local_gguf:try"],
  "json_escolhido": "local_gguf",
  "json_is_llm": true,
  "json_dados": {
    "idea": "Freelancing in Mozambique offers flexible opportunities for
             individuals to work remotely and gain skills without needing a
             permanent job."
  },
  "erro": null
}
```

`local_gguf` é o motor local: nenhuma chave, nenhuma chamada de rede na
inferência, nenhum limite de pedidos.

| O que | Valor medido |
|---|---|
| Modelo | Qwen3-0.6B, quantizado Q4_K_M, Apache-2.0 |
| Tamanho | 396 705 472 bytes |
| Velocidade (2 vCPU) | 28 tokens/s |
| Latência de um JSON dos esquadrões | 10–14 s |
| Custo | $0.00 |
| Voz | Kokoro-82M, 54 vozes, `pf_dora` / `pm_alex` / `pm_santa` |
| Vídeo gerado | 1080×1920, 18,87 s, `audio_engine: kokoro:pf_dora` |

---

## O Ollama não foi instalável. Porquê.

O pedido era `curl -fsSL https://ollama.com/install.sh | sh`. Não é possível
neste ambiente, e não por falta de tentativa:

| Tentativa | Resultado |
|---|---|
| `ollama.com/install.sh` | `000` — TLS encerrado a meio |
| `registry.ollama.ai` (manifesto) | `000` — bloqueado |
| Binário oficial do GitHub (`ollama-linux-amd64.tar.zst`, 1,65 GB) | redirecciona para `release-assets.githubusercontent.com` → bloqueado |
| `huggingface.co` / `cdn-lfs.huggingface.co` (pesos) | `000` — bloqueado |
| `git clone https://huggingface.co/...` | `gnutls_handshake() failed` |

O que está aberto: `github.com` (páginas e API), `pypi.org`,
`registry.npmjs.org`.

**Não há GPU** no sandbox (sem `nvidia-smi`, sem `/dev/dri`), 3,8 GB de RAM,
2 vCPU. Um modelo de 9 GB faria OOM, como o pedido antecipava correctamente.

## O que se usou em vez disso

Duas peças, ambas com integridade verificada:

### Motor de inferência — `llama-cpp-python`

`pip install llama-cpp-python` compila do sdist do PyPI (não há wheels
manylinux). ~13 min num CPU de 2 núcleos. O binário do Ollama não é
necessário: a biblioteca abre o `.gguf` directamente.

### Pesos — npm como transporte

O registry npm está aberto, e há modelos publicados lá em fragmentos:

| Pacote | Conteúdo | SHA-256 verificado |
|---|---|---|
| `tripilot-model-qwen` (+ `part-1`…`part-5`) | Qwen3-0.6B Q4_K_M, 5 fragmentos | `ac2d9771…0d524a` |
| `kokoro-q8-shards` | Kokoro-82M q8 ONNX, 6 fragmentos | `fbae9257…8a1478` |
| `kokoro-js` | 54 vozes `*.bin` (510×256 f32) | — |

O `provenance.json` do pacote Qwen declara a origem (HF `unsloth/Qwen3-0.6B-GGUF`),
o tamanho exacto e o SHA-256 de cada fragmento. Depois de remontar, o hash
confere — não é um binário de origem desconhecida.

Instalação e verificação num comando:

```bash
scripts/instalar_motor_local.sh
```

---

## Como o `llm_router` decidiu

```
groq → gemini → local_gguf → ollama → github_models → openrouter → local
```

`local_gguf` vem antes das nuvens «talvez» porque é grátis e ilimitado; Groq e
Gemini ficam à frente para quem tiver chave. `github_models` ficou **desligado
por omissão** (`GITHUB_MODELS_ENABLED=false`): mediu-se que o host devolve
`200 text/plain "OK"` a tudo, inclusive a um modelo inexistente e a um caminho
inválido — é um proxy de egress, e cada tentativa custava 25 s antes de se
chegar ao motor local.

## Duas armadilhas que custaram corridas

### 1. O bloco de raciocínio do Qwen3

O modelo escreve `<think>…</think>` antes de responder. Com `max_tokens=160`,
o orçamento acabava **dentro** do raciocínio e o JSON nunca aparecia — parecia
que o motor não funcionava. Pior: o extractor de JSON apanhava o exemplo
escrito *dentro* do raciocínio.

Correcção: mínimo de 512 tokens em modo JSON, e `extrair_json()` remove o
bloco antes de procurar o JSON.

### 2. O venv do runner

O diagnóstico mostrou `python 3.12.16 /opt/hostedtoolcache/...` a instalar os
pacotes enquanto o ciclo corria em `/tmp/venv`. Os pacotes iam para outro
intérprete e o provider nunca era tentado. Agora tudo usa `VENV=/tmp/venv`.

Estes dois defeitos só apareceram porque o diagnóstico passou a ser escrito em
`motor_local.txt` e publicado no ramo — os logs do Actions não são legíveis de
fora (o blob de resultados devolve EOF).

---

## Kokoro: a justificação antiga estava errada

O código recusava português com:

```
kokoro_onnx:sem_voz_pt (o pacote voices-v1.0 só tem vozes en/zh;
usar edge-tts para português)
```

É falso. O Kokoro-82M v1.0 tem 54 vozes, três portuguesas: `pf_dora`,
`pm_alex`, `pm_santa`. O que faltava era ter os ficheiros.

`backend/squads/kokoro_tts.py` faz o caminho completo:

```
texto → espeak-ng pt-br (via misaki) → fonemas IPA
      → vocabulário do Kokoro → input_ids
      → style = linha len(ids) da voz
      → onnxruntime → waveform 24 kHz
```

Verificado por análise do áudio gerado: 70% das janelas com energia de fala,
formantes em 80–300 Hz / 300–1 kHz / 1–3 kHz, DC ≈ 0. É voz, não ruído.

### Limitação declarada

O espeak só traz **pt-br**. O sotaque é brasileiro, não moçambicano — há
diferenças no «-s» final e na nasalidade. O Kokoro treina português, mas o
fonemizador é que decide a pronúncia, e não existe variante pt-MZ.

---

## Como reproduzir

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

VENV=.venv scripts/instalar_motor_local.sh     # compila + descarrega + verifica

export LOCAL_GGUF_PATH=$HOME/modelos/Qwen3-0.6B-Q4_K_M.gguf
export TTS_ENGINE=kokoro

PYTHONPATH=. .venv/bin/python scripts/diagnostico_motor.py
```

O diagnóstico deve mostrar `local_gguf configurado=True`, `Kokoro: pronto` e
`kokoro True` nos TTS disponíveis.

## Modelo maior, onde houver RAM

Numa máquina com 8–16 GB, o mesmo código aceita outro GGUF:

```bash
LOCAL_GGUF_PATH=~/modelos/qwen2.5-3b-instruct-q4_k_m.gguf scripts/instalar_motor_local.sh
```

Só o caminho muda — o router, o cache do modelo e a limpeza do `<think>`
continuam a valer.
