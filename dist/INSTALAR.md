# Instalar no telemóvel (real)

**Ficheiro:** `app-debug.apk` · 17,8 MB
**sha256:** `ca90923640abfa3f99fec41c6e31277a6e49d9837e487f939fc60321b730d979`
**Verificar:** `sha256sum -c SHA256SUMS.txt` → tem de dizer `OK`

---

## Já funciona no telemóvel — configura-se na app

Este APK **não traz o endereço fixo**. Ao abrir, tens um ecrã **«Ligar ao
backend»** com um campo de endereço. Escreves, carregas em **«Testar e
ligar»**, e só se o backend responder é que fica guardado.

O ecrã abre sozinho se a app não conseguir falar com o backend, e podes
abri-lo sempre pelo **ícone de Wi-Fi** no topo:
- verde = ligado
- vermelho = falhou
- cinza = por testar

### 1. No computador

```bash
# o IP da rede Wi-Fi (Linux/macOS; no Windows é `ipconfig`)
hostname -I            # ex.: 192.168.1.42

# o backend tem de aceitar ligações de fora do próprio PC:
API_ACCESS_TOKEN=o-teu-token python -m uvicorn backend.main:app \
    --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` é obrigatório. Com o valor por omissão (`127.0.0.1`) o
backend só ouve o próprio computador e o telemóvel recebe «ligação recusada».

### 2. No telemóvel

1. Instala o APK (ver *Instalar* abaixo).
2. Mesma rede Wi-Fi do computador.
3. No ecrã de ligação escreve `192.168.1.42:8000` (o teu IP, sem `http://`).
4. Token é opcional: **as leituras são públicas**; sem token a app vê tudo e
   não consegue aprovar nem accionar nada.
5. «Testar e ligar».

### 3. Se falhar, a mensagem diz porquê

| Mensagem | Causa | Solução |
|---|---|---|
| «não encontro "192.168…"» | IP errado ou outra rede | confirma `hostname -I`; mesma Wi-Fi |
| «recusou a ligação» | backend só em 127.0.0.1, ou desligado | `--host 0.0.0.0` |
| «não respondeu a tempo» | firewall do PC | abrir a porta 8000 |
| «o token não serve (401)» | token diferente do do backend | copiar o `API_ACCESS_TOKEN` |
| «HTTP em claro só para a rede local» | escreveste um domínio público | usar `https://` |

### Fora de casa

Um endereço público **tem** de ser `https://` — o APK recusa HTTP em claro
para a internet, para o token não viajar em texto aberto. Exemplo com túnel:

```bash
cloudflared tunnel --url http://localhost:8000
# escreve no telemóvel o https://....trycloudflare.com que ele imprimir
```

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
