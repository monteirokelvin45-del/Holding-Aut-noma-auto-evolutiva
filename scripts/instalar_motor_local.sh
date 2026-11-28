#!/usr/bin/env bash
# Instala um LLM local que funcione neste ambiente — sem Ollama, sem chaves.
#
# Porquê não Ollama
# -----------------
# `curl -fsSL https://ollama.com/install.sh | sh` falha aqui: `ollama.com` e
# `registry.ollama.ai` devolvem 000 (bloqueados), e o binário oficial do GitHub
# (`ollama-linux-amd64.tar.zst`, 1,65 GB) redirecciona para
# `release-assets.githubusercontent.com`, também bloqueado. Ver docs/LLM_LOCAL.md.
#
# O que este script faz
# ---------------------
# 1. `pip install llama-cpp-python` — compila a partir do sdist do PyPI
#    (~13 min num CPU de 2 núcleos; não há wheels manylinux no PyPI).
# 2. Descarrega o GGUF do Qwen3-0.6B Q4_K_M em 5 fragmentos publicados no
#    registry npm (que está aberto), remonta-os e **verifica o SHA-256**
#    contra o valor declarado na proveniência do pacote.
#
# O modelo é Qwen3-0.6B Q4_K_M (Apache-2.0), 397 MB, ~28 tokens/s num CPU de
# 2 núcleos. Serve para os agentes raciocinarem sem chave; não substitui um
# modelo de 70B.
#
# Uso:
#   scripts/instalar_motor_local.sh            # instala e verifica
#   LOCAL_GGUF_PATH=/caminho/modelo.gguf       # usa um GGUF já existente
set -uo pipefail

DESTINO="${MODELOS_DIR:-$HOME/modelos}"
GGUF="$DESTINO/Qwen3-0.6B-Q4_K_M.gguf"
SHA_ESPERADO="ac2d97712095a558e31573f62f466a3f9d93990898b0ec79d7c974c1780d524a"
REGISTRO="https://registry.npmjs.org"
PARTES="tripilot-model-qwen-part-1 tripilot-model-qwen-part-2 tripilot-model-qwen-part-3 tripilot-model-qwen-part-4 tripilot-model-qwen-part-5"

# O Python do sistema é "externally managed" (PEP 668) e não deixa instalar
# pacotes. Respeita um venv já activo; senão usa $VENV (ou .venv).
if [ -n "${VIRTUAL_ENV:-}" ]; then
  PY="$VIRTUAL_ENV/bin/python"
elif [ -n "${VENV:-}" ] && [ -x "$VENV/bin/python" ]; then
  PY="$VENV/bin/python"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  echo "ERRO: nenhum venv. Cria um: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt" >&2
  echo "      (o Python do sistema é externally-managed e recusa pip install)" >&2
  exit 1
fi
echo "   python: $PY"

echo "== 1/3 motor de inferência"
if "$PY" -c "import llama_cpp" 2>/dev/null; then
  echo "   llama-cpp-python já instalado ($("$PY" -c 'import llama_cpp; print(llama_cpp.__version__)'))"
else
  echo "   a compilar llama-cpp-python (sem wheels no PyPI; ~13 min em 2 núcleos)"
  "$PY" -m pip install --quiet llama-cpp-python || {
    echo "ERRO: compilação falhou. Ver docs/LLM_LOCAL.md." >&2
    exit 1
  }
  echo "   instalado: $("$PY" -c 'import llama_cpp; print(llama_cpp.__version__)')"
fi

echo "== 2/3 pesos (Qwen3-0.6B Q4_K_M, 397 MB em 5 fragmentos)"
mkdir -p "$DESTINO"
if [ -f "$GGUF" ] && [ "$(sha256sum "$GGUF" | cut -d' ' -f1)" = "$SHA_ESPERADO" ]; then
  echo "   já presente e verificado: $GGUF"
else
  TMP="$(mktemp -d)"
  : > "$TMP/weights.bin"
  for parte in $PARTES; do
    ficheiro="$TMP/$parte.tgz"
    echo "   $parte"
    curl -sL --retry 3 --max-time 300 -o "$ficheiro" \
      "$REGISTRO/$(echo "$parte" | sed 's|/|%2F|')/-/$parte-0.1.0-alpha.1.tgz" || {
        echo "ERRO: falha a descarregar $parte" >&2; exit 1; }
    tar xzf "$ficheiro" -C "$TMP" --strip-components=2 --wildcards '*/artifacts/weights.part' \
      -O >> "$TMP/weights.bin" || {
        echo "ERRO: $parte não contém artifacts/weights.part" >&2; exit 1; }
  done
  mv "$TMP/weights.bin" "$GGUF"
  rm -rf "$TMP"
fi

echo "== 3/3 integridade"
OBTIDO="$(sha256sum "$GGUF" | cut -d' ' -f1)"
if [ "$OBTIDO" != "$SHA_ESPERADO" ]; then
  echo "ERRO: SHA-256 não confere." >&2
  echo "  esperado: $SHA_ESPERADO" >&2
  echo "  obtido:   $OBTIDO" >&2
  exit 1
fi
echo "   SHA-256 confere: $OBTIDO"
echo "   $GGUF ($(du -h "$GGUF" | cut -f1))"

echo
echo "Pronto. Para usar:"
echo "  export LOCAL_GGUF_PATH=$GGUF"
echo "  PYTHONPATH=. $PY -c \"from backend.core.llm_router import llm_router, LLMRequest; \\"
echo "    r = llm_router.complete(LLMRequest(prompt='Diz OK', max_tokens=32)); print(r.provider, r.is_llm)\""
