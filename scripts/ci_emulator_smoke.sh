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

# Título esperado no corpo de cada aba. Serve para CONFIRMAR que o toque mudou
# de ecrã: no primeiro ensaio saíram 4 screenshots iguais e só o md5 o revelou.
titulo_da_aba() {
  case "$1" in
    "Ao Vivo")    echo "TERMINAL DE OPERAÇÕES" ;;
    # Textos lidos do código dos ecrãs (não inventados): cada aba tem um
    # título fixo que existe mesmo que os dados estejam vazios.
    "Aprovar")    echo "MESA DE APROVAÇÕES" ;;
    "Tesouro")    echo "COFRES" ;;
    "Diretoria")  echo "DIRETORIA" ;;
    "Esquadrões") echo "ABATIDOS" ;;
  esac
}

textos_do_ecra() {  # todos os textos visíveis, um por linha
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  adb pull /sdcard/ui.xml /tmp/ui.xml >/dev/null 2>&1
  python3 -c '
import re
try:
    xml = open("/tmp/ui.xml", encoding="utf-8", errors="ignore").read()
except FileNotFoundError:
    raise SystemExit(0)
for m in re.finditer(r"text=\"([^\"]+)\"", xml):
    print(m.group(1))
'
}

tocar_texto() {  # $1 = etiqueta da aba, $2 = nome do screenshot
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  adb pull /sdcard/ui.xml /tmp/ui.xml >/dev/null 2>&1
  local coords
  # A etiqueta da aba NÃO é a área clicável: num NavigationBarItem, tocar no
  # centro da Row do ícone/texto não acende a aba (medido: as abas 02-05
  # ficaram iguais). Usa-se o menor nó *clicável* que contém esse texto — o
  # próprio NavigationBarItem.
  coords=$(python3 -c '
import re, sys
from xml.etree import ElementTree as ET

alvo = sys.argv[1]
try:
    raiz = ET.parse("/tmp/ui.xml").getroot()
except Exception:
    sys.exit(0)

def caixa(no):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", no.get("bounds", ""))
    return tuple(map(int, m.groups())) if m else None

def centro(b):
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2

clicaveis = []
for no in raiz.iter():
    if no.get("clickable") == "true" and caixa(no):
        clicaveis.append(no)

def tem_texto(no):
    if (no.get("text") or "").strip() == alvo:
        return True
    return any((d.get("text") or "").strip() == alvo for d in no.iter())

candidatos = [n for n in clicaveis if tem_texto(n)]
if candidatos:
    menor = min(candidatos, key=lambda n: (caixa(n)[2]-caixa(n)[0]) * (caixa(n)[3]-caixa(n)[1]))
    x, y = centro(caixa(menor))
    print(x, y)
    sys.exit(0)

# Sem nó clicável: cair para o centro do texto.
for no in raiz.iter():
    if (no.get("text") or "").strip() == alvo and caixa(no):
        x, y = centro(caixa(no))
        print(x, y)
        break
' "$1")
  if [ -z "$coords" ]; then
    nok "não encontrei a aba '$1' no ecrã"
    return
  fi
  # shellcheck disable=SC2086
  adb shell input tap $coords
  sleep 3

  local esperado ecra
  esperado="$(titulo_da_aba "$1")"
  ecra="$(textos_do_ecra)"
  if echo "$ecra" | grep -qi "$esperado"; then
    ok "aba '$1' aberta (título \"$esperado\" visível)"
  else
    nok "aba '$1' NÃO abriu — \"$esperado\" não está no ecrã"
    { echo "--- textos visíveis ao tocar em '$1' ---"; echo "$ecra" | head -20; } >> "$SAIDA/adb.log"
  fi
  tirar_ecra "$2"
}

tocar_texto "Aprovar"     "02_aprovar"
tocar_texto "Tesouro"     "03_tesouro"
tocar_texto "Diretoria"   "04_diretoria"
tocar_texto "Esquadrões"  "05_esquadroes"
tocar_texto "Ao Vivo"     "06_ao_vivo_de_volta"

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
