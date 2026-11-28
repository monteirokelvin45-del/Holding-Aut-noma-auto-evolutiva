#!/usr/bin/env bash
# Teste de fumo do APK num emulador Android real (corre no runner do GitHub,
# que tem /dev/kvm; no sandbox de desenvolvimento não há virtualização).
#
# Objectivo: não "verificar" o APK por fora, mas executá-lo e trazer provas:
#   - a app arranca sem crash (pid vivo, sem FATAL EXCEPTION no logcat);
#   - fala com o backend de verdade (o uvicorn regista os GETs);
#   - abre o WebSocket;
#   - desenha os 5 ecrãs (screenshots reais, não mockups).
#
# Uso: bash scripts/ci_emulator_smoke.sh <ficheiro.apk> <directoria_de_saida>
set -uo pipefail

APK="${1:?falta o caminho do APK}"
SAIDA="${2:-saida}"
PACOTE="com.automaton.holding.debug"
ACTIVIDADE="$PACOTE/com.automaton.holding.MainActivity"

mkdir -p "$SAIDA"
LOG="$SAIDA/adb.log"
: > "$LOG"

registar() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

falhas=0
ok()   { registar "OK   $*"; }
nok()  { registar "FALHA $*"; falhas=$((falhas+1)); }

# ------------------------------------------------------------------ 0. espera
registar "à espera do dispositivo"
adb wait-for-device
for _ in $(seq 1 60); do
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 2
done
registar "modelo: $(adb shell getprop ro.product.model | tr -d '\r') · Android $(adb shell getprop ro.build.version.release | tr -d '\r')"
adb shell settings put global window_animation_scale 0 || true
adb shell settings put global transition_animation_scale 0 || true
adb shell settings put global animator_duration_scale 0 || true

# --------------------------------------------------------------- 1. instalar
registar "a instalar $APK"
if adb install -r -t "$APK" 2>&1 | tee -a "$LOG" | grep -q "Success"; then
  ok "APK instalado"
else
  nok "instalação falhou"
fi

adb logcat -c || true

# --------------------------------------------------------------- 2. arrancar
registar "a arrancar $ACTIVIDADE"
adb shell am start -W -n "$ACTIVIDADE" 2>&1 | tee -a "$LOG"

# O arranque a frio de um Compose num emulador x86_64 leva alguns segundos.
sleep 12

PID="$(adb shell pidof "$PACOTE" | tr -d '\r')"
if [ -n "$PID" ]; then ok "processo vivo (pid $PID)"; else nok "app não está a correr"; fi

# -------------------------------------------------------- 3. ecrãs (prova)
tirar_ecra() {  # $1 = nome
  adb exec-out screencap -p > "$SAIDA/$1.png"
  local bytes
  bytes=$(wc -c < "$SAIDA/$1.png")
  if [ "$bytes" -gt 20000 ]; then ok "screenshot $1.png ($bytes bytes)"; else nok "screenshot $1.png vazio ($bytes bytes)"; fi
}

tirar_ecra "01_ao_vivo"

# Aba por aba, tocando no texto real da barra de navegação (não em coordenadas
# fixas): se o layout mudar, os toques continuam a acertar.
tocar_texto() {  # $1 = etiqueta da aba, $2 = nome do screenshot
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  adb pull /sdcard/ui.xml /tmp/ui.xml >/dev/null 2>&1
  local coords
  coords=$(python3 - "$1" <<'PY'
import re, sys
alvo = sys.argv[1]
try:
    xml = open("/tmp/ui.xml", encoding="utf-8", errors="ignore").read()
except FileNotFoundError:
    sys.exit(0)
for m in re.finditer(r'text="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
    if m.group(1).strip() == alvo:
        x1, y1, x2, y2 = map(int, m.groups()[1:])
        print(f"{(x1+x2)//2} {(y1+y2)//2}")
        break
PY
)
  if [ -z "$coords" ]; then
    nok "não encontrei a aba '$1' no ecrã"
    return
  fi
  # shellcheck disable=SC2086
  adb shell input tap $coords
  sleep 3
  tirar_ecra "$2"
}

tocar_texto "Aprovar"     "02_aprovar"
tocar_texto "Tesouro"     "03_tesouro"
tocar_texto "Diretoria"   "04_diretoria"
tocar_texto "Esquadrões"  "05_esquadroes"

# ------------------------------------------------ 4. o backend foi contactado
registar "a copiar o logcat"
adb logcat -d > "$SAIDA/logcat.txt" 2>&1 || true

if grep -qE "FATAL EXCEPTION|AndroidRuntime.*FATAL" "$SAIDA/logcat.txt"; then
  nok "FATAL EXCEPTION no logcat"
  grep -A12 "FATAL EXCEPTION" "$SAIDA/logcat.txt" | head -40 >> "$SAIDA/adb.log"
else
  ok "sem FATAL EXCEPTION"
fi

if grep -q "WebSocket ligado" "$SAIDA/logcat.txt"; then
  ok "WebSocket ligado ao backend"
else
  nok "WebSocket não ligou (ver logcat)"
fi

# O backend escreve o seu próprio log; se o ficheiro existir, confirma-se que
# recebeu pedidos da app.
if [ -f /tmp/backend.log ]; then
  cp /tmp/backend.log "$SAIDA/backend.log"
  n_get=$(grep -c "GET /" /tmp/backend.log || true)
  if [ "${n_get:-0}" -gt 0 ]; then
    ok "backend recebeu $n_get pedidos GET da app"
  else
    nok "o backend não registou nenhum pedido da app"
  fi
  if grep -q "WebSocket /ws/live" /tmp/backend.log; then
    ok "backend registou a ligação /ws/live"
  else
    nok "backend não registou /ws/live"
  fi
fi

# ------------------------------------------------------------------ 5. resumo
{
  echo "## Execução no emulador"
  echo
  echo "| Verificação | Resultado |"
  echo "|---|---|"
  echo "| Falhas | **$falhas** |"
  echo
  if [ -f "$SAIDA/backend.log" ]; then
    echo "Pedidos recebidos pelo backend:"
    echo '```'
    grep -E "GET /|WebSocket" "$SAIDA/backend.log" | tail -20
    echo '```'
  fi
} > "$SAIDA/RELATORIO.md"

registar "fim: $falhas falha(s)"
[ "$falhas" -eq 0 ]
