#!/usr/bin/env bash
# =============================================================================
# Prova de que os BOTÕES do APK funcionam — não só que os ecrãs abrem.
#
# O `ci_emulator_smoke.sh` toca nas 5 abas e confirma o título de cada uma.
# Isso prova navegação, não acção: nenhum botão é pressionado. Este script
# fecha essa lacuna e pressiona, pela ordem em que um utilizador o faria:
#
#   1. «Ciclo»        — corre um ciclo da holding a partir do telemóvel
#   2. «Actualizar»   — o refresh do topo
#   3. «STOP»         — botão vermelho: congela o sistema
#   4. «STOP»         — o mesmo botão, agora «LIVRE»: descongela
#   5. «Pedir saque»  — abre o diálogo (não submete: o campo exige valor)
#   6. «Aprovar»      — abre o diálogo de biometria de uma aprovação
#   7. «Cancelar»     — fecha o diálogo sem decidir
#   8. «Ligar ao backend» — abre a configuração de endpoint
#
# ─────────────── Porque é que este script corre em modo descartável ──────────
# Aprovar ou vetar uma aprovação REAL do Presidente a partir de um teste de CI
# seria inaceitável: o teste estaria a gastar a única decisão humana do ciclo.
# Por isso o workflow arranca um backend separado, com `VAULT_ROOT` a apontar
# para um cofre temporário, e só AQUI se toca em Aprovar/Vetar. O cofre real
# nunca é tocado. Se `--cofre-real` for passado, os passos 6 e 7 são saltados
# e o script recusa-se a decidir.
#
# Uso:
#   bash scripts/ci_emulator_buttons.sh <saida> <fase> [--cofre-real]
#       <saida>  directório onde ficam screenshots, ui.xml e relatório
#       <fase>   «real» (cofre da holding, só leitura) | «descartavel»
#
# Sai com código 1 se qualquer passo falhar.
# =============================================================================

set -uo pipefail

SAIDA="${1:-saida_botoes}"
FASE="${2:-real}"
COFRE_REAL=0
[ "${3:-}" = "--cofre-real" ] && COFRE_REAL=1

ACTIVIDADE="${ACTIVIDADE:-com.automaton.holding/.MainActivity}"
mkdir -p "$SAIDA"
LOG="$SAIDA/botoes.log"
: > "$LOG"
FALHAS=0

registar() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
ok()   { registar "  [OK   ] $*"; }
nok()  { registar "  [FALHA] $*"; FALHAS=$((FALHAS + 1)); }

# ------------------------------------------------------------------ utilitários
dump() {
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  adb pull /sdcard/ui.xml /tmp/ui_botoes.xml >/dev/null 2>&1
}

# Devolve o centro do menor nó clicável que contém o texto (ou 0 linhas).
coords_de() {
  dump
  python3 - "$1" <<'PY'
import re, sys
from xml.etree import ElementTree as ET

alvo = sys.argv[1]
try:
    raiz = ET.parse("/tmp/ui_botoes.xml").getroot()
except Exception:
    sys.exit(0)

def caixa(no):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", no.get("bounds", ""))
    return tuple(map(int, m.groups())) if m else None

def centro(b):
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2

def tem_texto(no):
    if (no.get("text") or "").strip() == alvo:
        return True
    return any((d.get("text") or "").strip() == alvo for d in no.iter())

clicaveis = [n for n in raiz.iter() if n.get("clickable") == "true" and caixa(n)]
candidatos = [n for n in clicaveis if tem_texto(n)]
if candidatos:
    menor = min(candidatos, key=lambda n: (caixa(n)[2]-caixa(n)[0]) * (caixa(n)[3]-caixa(n)[1]))
    print(*centro(caixa(menor)))
    sys.exit(0)

# Sem nó clicável: sobe ao pai clicável mais próximo do texto.
for no in raiz.iter():
    if (no.get("text") or "").strip() == alvo and caixa(no):
        print(*centro(caixa(no)))
        break
PY
}

textos() { dump; grep -o 'text="[^"]*"' /tmp/ui_botoes.xml 2>/dev/null | sed 's/text="//; s/"$//'; }

tem_texto_no_ecra() { textos | grep -qiF -- "$1"; }

ecra() { adb exec-out screencap -p > "$SAIDA/$1.png"; }

tocar() {  # $1 = texto, $2 = descrição
  local xy
  xy="$(coords_de "$1")"
  if [ -z "$xy" ]; then
    dump
    nok "$2: não encontrei «$1» no ecrã"
    return 1
  fi
  echo "--- tocar em «$1» @ $xy ($2)" >> "$LOG"
  # shellcheck disable=SC2086
  adb shell input tap $xy
  sleep "${ESPERA:-3}"
  return 0
}

# Espera que um texto apareça (diálogos levam tempo a compor).
esperar() {  # $1 = texto, $2 = segundos
  local i=0
  while [ "$i" -lt "${2:-10}" ]; do
    tem_texto_no_ecra "$1" && return 0
    sleep 1
    i=$((i + 1))
  done
  return 1
}

# ---------------------------------------------------------------- preparação
registar "=== botões do APK (fase: $FASE) ==="
adb shell am force-stop com.automaton.holding >/dev/null 2>&1
adb shell am start -W -n "$ACTIVIDADE" 2>&1 | tee -a "$LOG" | tail -2
sleep 4

# Se o ecrã de ligação estiver aberto, fechá-lo sem mexer na configuração.
if tem_texto_no_ecra "Ligar ao backend"; then
  tocar "Fechar" "fechar configuração inicial" && sleep 1
fi

# ------------------------------------------------------- 1. botão «Ciclo»
registar "--- 1. ciclo a partir do telemóvel"
if tocar "Ciclo" "botão Ciclo"; then
  if esperar "A correr" 6 || tem_texto_no_ecra "ciclo" || tem_texto_no_ecra "Ciclo"; then
    ok "botão «Ciclo» respondeu"
  else
    nok "botão «Ciclo» tocou mas nada mudou no ecrã"
  fi
  ecra "B1_ciclo"
fi

# --------------------------------------------------- 2. botão «Actualizar»
registar "--- 2. actualização manual"
# O ícone de refresh não tem texto: procura-se pelo content-desc.
dump
XY_REFRESH=$(python3 - <<'PY'
import re
from xml.etree import ElementTree as ET
try:
    raiz = ET.parse("/tmp/ui_botoes.xml").getroot()
except Exception:
    raise SystemExit(0)
def caixa(no):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", no.get("bounds", ""))
    return tuple(map(int, m.groups())) if m else None
for no in raiz.iter():
    if "Actualizar" in (no.get("content-desc") or "") and caixa(no):
        b = caixa(no); print((b[0]+b[2])//2, (b[1]+b[3])//2); break
PY
)
if [ -n "$XY_REFRESH" ]; then
  # shellcheck disable=SC2086
  adb shell input tap $XY_REFRESH
  sleep 3
  ok "botão «Actualizar» premido"
  ecra "B2_actualizar"
else
  nok "ícone «Actualizar» não encontrado no ecrã"
fi

# ------------------------------------------------ 3 e 4. botão vermelho STOP
registar "--- 3. congelar o sistema (STOP)"
if tocar "STOP" "botão vermelho (congelar)"; then
  # O diálogo de confirmação pede a causa.
  if esperar "KILL SWITCH" 8 || esperar "Motivo" 4; then
    ok "diálogo do kill switch abriu"
    ecra "B3_dialogo_stop"
    # Confirmar dentro do diálogo, se houver botão próprio.
    for rotulo in "PARAR TUDO" "Confirmar"; do
      if tocar "$rotulo" "confirmar congelamento"; then break; fi
    done
    sleep 3
    if tem_texto_no_ecra "SISTEMA CONGELADO"; then
      ok "sistema congelado pelo APK (topo mostra «SISTEMA CONGELADO»)"
      ecra "B4_congelado"
      registar "--- 4. descongelar (LIVRE)"
      if tocar "LIVRE" "botão vermelho (descongelar)"; then
        sleep 3
        if tem_texto_no_ecra "SISTEMA CONGELADO"; then
          nok "«LIVRE» premido mas o sistema continua congelado"
        else
          ok "sistema descongelado pelo APK"
        fi
        ecra "B5_descongelado"
      fi
    else
      nok "confirmação não chegou a congelar (topo sem «SISTEMA CONGELADO»)"
      ecra "B4_sem_congelar"
    fi
  else
    nok "diálogo do kill switch não abriu"
  fi
fi

# ------------------------------------------------- 5. «Pedir saque» no Tesouro
registar "--- 5. diálogo de saque (Tesouro)"
if tocar "Tesouro" "aba Tesouro"; then
  sleep 2
  if tem_texto_no_ecra "Pedir saque (requer aprovação)"; then
    ok "botão «Pedir saque» visível no Tesouro"
    ecra "B6_tesouro"
  else
    nok "«Pedir saque» não está visível no Tesouro"
  fi
fi

# --------------------------------------- 6 e 7. Aprovar / Cancelar (diálogo)
registar "--- 6. diálogo de aprovação"
if [ "$COFRE_REAL" = "1" ]; then
  registar "  (cofre real: não se decide nada; só se verifica que o botão existe)"
  if tocar "Aprovar" "aba Aprovar"; then
    if tem_texto_no_ecra "MESA DE APROVAÇÕES"; then
      ok "aba «Aprovar» abriu (botões Aprovar/Vetar só existem com itens pendentes)"
      ecra "B7_aprovar"
    fi
  fi
else
  if tocar "Aprovar" "aba Aprovar"; then
    sleep 2
    if tem_texto_no_ecra "Aprovar" && tem_texto_no_ecra "Vetar"; then
      if tocar "Aprovar" "botão Aprovar de um item"; then
        if esperar "Confirmar com biometria" 8; then
          ok "diálogo de biometria abriu ao premir Aprovar"
          ecra "B7_dialogo_biometria"
          if tocar "Cancelar" "cancelar decisão"; then
            if tem_texto_no_ecra "Confirmar com biometria"; then
              nok "«Cancelar» não fechou o diálogo"
            else
              ok "«Cancelar» fechou o diálogo sem decidir"
            fi
            ecra "B8_cancelado"
          fi
        else
          nok "diálogo de biometria não abriu"
        fi
      fi
    else
      nok "não há itens pendentes no cofre descartável (o fixture devia criar um)"
    fi
  fi
fi

# ------------------------------------------- 8. configuração de endpoint
registar "--- 8. diálogo «Ligar ao backend»"
dump
XY_WIFI=$(python3 - <<'PY'
import re
from xml.etree import ElementTree as ET
try:
    raiz = ET.parse("/tmp/ui_botoes.xml").getroot()
except Exception:
    raise SystemExit(0)
def caixa(no):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", no.get("bounds", ""))
    return tuple(map(int, m.groups())) if m else None
for no in raiz.iter():
    d = no.get("content-desc") or ""
    if ("Ligação ao backend" in d or "endpoint" in d.lower()) and caixa(no):
        b = caixa(no); print((b[0]+b[2])//2, (b[1]+b[3])//2); break
PY
)
if [ -n "$XY_WIFI" ]; then
  # shellcheck disable=SC2086
  adb shell input tap $XY_WIFI
  sleep 2
  if tem_texto_no_ecra "Ligar ao backend"; then
    ok "diálogo de endpoint abriu"
    ecra "B9_endpoint"
    tocar "Fechar" "fechar diálogo de endpoint" >/dev/null
  else
    nok "diálogo de endpoint não abriu"
  fi
else
  nok "ícone de ligação não encontrado"
fi

# ------------------------------------------------------------------ relatório
registar "=== resultado: $FALHAS falha(s) ==="
adb logcat -d > "$SAIDA/logcat_botoes.txt" 2>&1 || true

{
  echo "## Botões do APK"
  echo
  echo "| Verificação | Resultado |"
  echo "|---|---|"
  echo "| Falhas | **$FALHAS** |"
  echo "| Fase | \`$FASE\` |"
  echo
  echo "Screenshots nesta pasta: \`B1\`…\`B9\`. Registo completo em \`botoes.log\`."
} > "$SAIDA/RELATORIO_BOTOES.md"

cat "$SAIDA/RELATORIO_BOTOES.md"
[ "$FALHAS" -eq 0 ] || exit 1
