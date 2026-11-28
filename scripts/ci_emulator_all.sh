#!/usr/bin/env bash
# =============================================================================
# As duas passagens do emulador, num sítio só.
#
# Porque existe este ficheiro: a lógica estava dentro do `script:` do
# android-emulator-runner, e o runner falhava com exit 2 antes de correr
# uma linha — sem deixar rasto, porque os logs do job não são acessíveis
# por API. Um script no repositório testa-se com `bash -n`, corre-se
# localmente, e o YAML fica com uma linha.
#
# Uso: bash scripts/ci_emulator_all.sh <apk> <dir_smoke> <dir_botoes> [fase]
#
# Corre as duas passagens sempre, mesmo que a primeira falhe: interessa o
# retrato completo, não parar no primeiro sintoma. No fim, sai 1 se alguma
# falhou.
# =============================================================================
set -uo pipefail

APK="${1:?falta o caminho do APK}"
DIR_SMOKE="${2:?falta o directório do smoke}"
DIR_BOTOES="${3:?falta o directório dos botões}"
FASE="${4:-descartavel}"

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

falhou=0
rc_smoke=0
rc_botoes=0

echo "=== emulador: smoke ==="
bash "$AQUI/ci_emulator_smoke.sh" "$APK" "$DIR_SMOKE" || rc_smoke=$?
echo "smoke: exit=$rc_smoke"

echo "=== emulador: botões ==="
bash "$AQUI/ci_emulator_buttons.sh" "$DIR_BOTOES" "$FASE" || rc_botoes=$?
echo "botões: exit=$rc_botoes"

if [ "$rc_smoke" -ne 0 ] || [ "$rc_botoes" -ne 0 ]; then
  falhou=1
  echo "::error::emulador falhou (smoke=$rc_smoke botoes=$rc_botoes)"
else
  echo "emulador: 0 falhas nas duas passagens"
fi

exit "$falhou"
