#!/usr/bin/env bash
# Instala o Android platform-tools (adb, fastboot) sem dl.google.com.
#
# O SDK Android oficial descarrega de dl.google.com, que está bloqueado na rede
# do sandbox (000). O npm serve os mesmos binários oficiais empacotados por
# terceiros. Verificam-se por sha256 antes de serem aceites: um adb adulterado
# é um vetor de ataque óbvio (acede ao telefone por USB).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/tools"
PKG="android-sdk-tools-linux"
VER="37.0.1"
URL="https://registry.npmjs.org/${PKG}/-/${PKG}-${VER}.tgz"

# sha256 dos binários, fixados na primeira instalação verificada.
SHA_ADB="a902be8f45c6c62e76c9efaf6947a0fa747c9cabd89a2ac8e0d16ecb30b3ed01"
SHA_FASTBOOT="a686e2c7e8dc9cf4cba0cb8a2eef05f7b2bd682c925abd032fe203215d80b618"

if [[ -x "$DEST/adb" && -x "$DEST/fastboot" ]]; then
  if [[ "$(sha256sum "$DEST/adb" | cut -d' ' -f1)" == "$SHA_ADB" ]]; then
    echo "ok: adb e fastboot já presentes em $DEST"
    "$DEST/adb" version | head -1
    exit 0
  fi
  echo "aviso: adb presente mas com sha256 diferente; a reinstalar"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "==> a descarregar $PKG@$VER (npm)"
curl -fsSL "$URL" -o "$TMP/p.tgz"
tar xzf "$TMP/p.tgz" -C "$TMP"
mkdir -p "$DEST"
cp -r "$TMP/package/." "$DEST/"
chmod +x "$DEST/adb" "$DEST/fastboot"

echo "==> a verificar"
GOT_ADB="$(sha256sum "$DEST/adb" | cut -d' ' -f1)"
if [[ "$GOT_ADB" != "$SHA_ADB" ]]; then
  echo "erro: sha256 do adb não confere" >&2
  echo "  esperado: $SHA_ADB" >&2
  echo "  obtido:   $GOT_ADB" >&2
  echo "  (binário rejeitado; não confiar num adb não verificado)" >&2
  rm -f "$DEST/adb"
  exit 1
fi
if [[ "$(sha256sum "$DEST/fastboot" | cut -d' ' -f1)" != "$SHA_FASTBOOT" ]]; then
  echo "erro: sha256 do fastboot não confere" >&2
  rm -f "$DEST/fastboot"; exit 1
fi

"$DEST/adb" version | head -1
"$DEST/fastboot" --version | head -1
echo "pronto: $DEST/adb"
