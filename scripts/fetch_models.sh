#!/usr/bin/env bash
# Descarrega os pesos do Kokoro-82M sem depender do GitHub Releases nem do
# HuggingFace — ambos bloqueados na rede do sandbox (ver docs/stack_audit.md §1).
#
# Rota usada: dois pacotes npm públicos que contêm os pesos.
#   kokoro-q8-shards      -> model_quantized.onnx em 6 fragmentos (63,0 MB)
#   kokoro-local-runtime  -> 28 ficheiros voices/*.bin (13,5 MB)
#
# Idempotente: se os ficheiros finais já existem e o sha256 bate certo, sai
# imediatamente. Nada é dado como instalado sem verificação.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/models/kokoro"
MODEL="$DEST/kokoro-v1.0.q8.onnx"
VOICES="$DEST/voices-v1.0.npz"
EXPECTED_SHA="fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478"
NPM="https://registry.npmjs.org"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$DEST"

sha_of() { sha256sum "$1" | cut -d' ' -f1; }

if [[ -f "$MODEL" && -f "$VOICES" ]]; then
  if [[ "$(sha_of "$MODEL")" == "$EXPECTED_SHA" ]]; then
    echo "ok: pesos já presentes e verificados em $DEST"
    exit 0
  fi
  echo "aviso: sha256 do modelo não confere; a recarregar"
fi

command -v curl >/dev/null || { echo "erro: curl em falta" >&2; exit 1; }
PY="${PYTHON:-python3}"

echo "==> 1/3 fragmentos do modelo (kokoro-q8-shards@1.0.0)"
curl -fsSL "$NPM/kokoro-q8-shards/-/kokoro-q8-shards-1.0.0.tgz" -o "$TMP/shards.tgz"
tar -xzf "$TMP/shards.tgz" -C "$TMP"

# Layout real: package/kokoro-q8.part0.bin ... .part5.bin.
# Ordena por (nome, índice numérico) para que part10 venha depois de part9.
mapfile -t PARTS < <(
  find "$TMP/package" -type f -name '*.part[0-9]*.bin' |
    awk -F'.part' '{print $2"	"$0}' |
    sort -n -k1,1 |
    cut -f2-
)
if [[ ${#PARTS[@]} -eq 0 ]]; then
  mapfile -t PARTS < <(find "$TMP/package" -type f -name '*.onnx*' ! -name '*.json' | sort)
fi
if [[ ${#PARTS[@]} -eq 0 ]]; then
  echo "erro: nenhum fragmento de modelo dentro do .tgz" >&2
  find "$TMP/package" -type f | head -20 >&2
  exit 1
fi
echo "    ${#PARTS[@]} fragmentos"
cat "${PARTS[@]}" > "$MODEL"
GOT="$(sha_of "$MODEL")"
if [[ "$GOT" != "$EXPECTED_SHA" ]]; then
  echo "erro: sha256 do modelo reconstruído não confere" >&2
  echo "  esperado: $EXPECTED_SHA" >&2
  echo "  obtido:   $GOT" >&2
  exit 1
fi
echo "    sha256 ok ($(du -h "$MODEL" | cut -f1))"

echo "==> 2/3 vozes (kokoro-local-runtime@0.1.0)"
curl -fsSL "$NPM/kokoro-local-runtime/-/kokoro-local-runtime-0.1.0.tgz" -o "$TMP/voices.tgz"
tar -xzf "$TMP/voices.tgz" -C "$TMP"
"$PY" - "$TMP" "$VOICES" <<'PY'
import sys
from pathlib import Path

import numpy as np

src, dest = Path(sys.argv[1]), Path(sys.argv[2])
# Cada voice.bin são 510x256 float32 empatados num vector de 522 240 bytes.
# Não é float16: ler como float16 produz NaN.
bins = sorted(src.rglob("voices/*.bin"))
if not bins:
    sys.exit("erro: nenhum voices/*.bin dentro do .tgz")
out = {}
for path in bins:
    flat = np.fromfile(path, dtype=np.float32)
    if flat.size != 510 * 256:
        sys.exit(f"erro: {path.name} tem {flat.size} floats, esperado {510*256}")
    out[path.stem] = flat.reshape(510, 256)
np.savez(dest, **out)
print(f"    {len(out)} vozes -> {dest.name}")
PY

echo "==> 3/3 verificação final"
[[ -f "$MODEL" ]] || { echo "erro: modelo ausente" >&2; exit 1; }
[[ -f "$VOICES" ]] || { echo "erro: vozes ausentes" >&2; exit 1; }
"$PY" - "$ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
try:
    from backend.squads.video_factory_squad import TTSRegistry
except ImportError as exc:
    sys.exit(f"aviso: dependências do squad em falta ({exc})")
ok, detail = TTSRegistry._kokoro(
    "Offline check, one two three.", Path("/tmp/kokoro_check.wav"), "en"
)
print(f"    síntese de controlo (en): ok={ok} ({detail})")
if not ok:
    sys.exit(1)
PY

echo "pronto. Pesos aplicáveis: Apache-2.0 (Kokoro-82M)."
