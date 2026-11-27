# Instalar no telemóvel (real)

**Ficheiro:** `app-debug.apk` · 17,8 MB
**sha256:** `ca90923640abfa3f99fec41c6e31277a6e49d9837e487f939fc60321b730d979`
**Verificar:** `sha256sum -c SHA256SUMS.txt` → tem de dizer `OK`

---

## O problema deste APK num telemóvel

Este binário foi compilado **sem `AUTOMATON_BASE_URL`**, por isso a `BASE_URL`
embutida é `http://10.0.2.2:8000` — o alias do **emulador** Android para a
máquina anfitriã. Num telemóvel real esse endereço não existe: a app arranca e
mostra «não consigo chegar a http://10.0.2.2:8000».

Não é um bug do APK: é o build a dizer a verdade sobre o que tem dentro. Para
o usares **no telemóvel** precisas de uma versão compilada com a URL do teu
backend.

---

## Caminho A — backend no computador, telemóvel na mesma Wi-Fi (mais rápido)

Não precisa de servidor público nem de HTTPS: o APK de **debug** já permite
HTTP em claro para a rede local.

1. Descobre o IP local do computador:
   - Linux/macOS: `hostname -I` ou `ipconfig getifaddr en0`
   - Windows: `ipconfig` → "Endereço IPv4" (ex.: `192.168.1.42`)
2. Sobe o backend:
   ```bash
   API_ACCESS_TOKEN=o-teu-token python -m uvicorn backend.main:app \
       --host 0.0.0.0 --port 8000
   ```
3. Compila o APK a apontar para esse IP:
   ```bash
   gh workflow run 02_build_android_apk.yml \
       -f base_url=http://192.168.1.42:8000
   ```
   (ou localmente: `./gradlew :app:assembleDebug -Pautomaton.baseUrl=http://192.168.1.42:8000`)
4. Descarrega o novo APK do ramo `apk-artifact` (ou do artefacto do run).
5. Instala — ver a secção *Instalar* abaixo.

O telemóvel tem de estar na **mesma rede** do computador. Se o router isolar
clientes (AP isolation), não vai funcionar; nesse caso usa o caminho B.

## Caminho B — backend público com HTTPS (para usar fora de casa)

O APK de debug aceita HTTP em claro **apenas** para `10.0.2.2`, `localhost` e
`127.0.0.1`. Para um endereço público é obrigatório HTTPS.

```bash
# exemplo com um túnel (a máquina precisa de rede para o serviço de túnel)
cloudflared tunnel --url http://localhost:8000     # dá um https://....trycloudflare.com
gh workflow run 02_build_android_apk.yml -f base_url=https://o-teu-endereco
```

Sem HTTPS o Android recusa a ligação com «Cleartext HTTP traffic not
permitted» — política de rede do próprio APK, de propósito.

---

## Instalar

### Opção 1 — ADB por USB (recomendado)

O `adb` oficial (37.0.1) está neste repositório, descarregado por
`scripts/fetch_adb.sh` — o `dl.google.com` está bloqueado no ambiente onde o
projeto foi construído, mas o npm serve o mesmo binário, verificado por
sha256.

```bash
bash scripts/fetch_adb.sh          # idempotente, verifica o checksum
tools/adb devices                   # autoriza no telemóvel: "Permitir depuração USB"
tools/adb install -r app-debug.apk
```

No telemóvel: Definições → Sobre → tocar 7× em "Número de compilação" →
Opções de programador → **Depuração USB**.

### Opção 2 — transferir o ficheiro

Copia `app-debug.apk` para o telemóvel (cabo, Drive, Telegram para ti mesmo),
abre-o e autoriza «instalar de fontes desconhecidas» para essa app.

### Depois de instalar

```bash
tools/adb logcat -s ApiService:* HoldingViewModel:*   # ver o que a app faz
tools/adb shell am start -n com.automaton.holding.debug/.MainActivity
```

---

## Assinatura e confiança

APK de **debug**, assinado com `CN=Android Debug`. Não serve para
distribuição na Play Store, e a chave é a pública de debug — qualquer pessoa
pode assinar um APK igual. Para uso a sério, gera um keystore próprio e usa
o passo de release do workflow.

## Se aparecer «não consigo chegar a …»

É a mensagem certa: o endereço vem do build. Confirma qual está embutido:

```bash
unzip -p app-debug.apk classes.dex | strings | grep -oE 'https?://[0-9a-zA-Z.:_-]+' | sort -u | head
```
