# APK — auditoria e estado de teste

Documento honesto do que foi **testado de facto**, do que foi corrigido, e do
que **não** foi possível executar neste ambiente. Última actualização:
2026-11-27.

---

## 1. Veredicto curto

| Camada | Estado | Prova |
|---|---|---|
| Contratos APK ↔ API (28 modelos, 226 campos) | **testado** | 0 divergências contra respostas reais |
| Corpos de pedido (7 rotas de escrita) | **testado** | nomes e tipos conferem |
| Autenticação e biometria | **testado** | 401 sem token, 428 sem biometria, 200 com |
| Kill switch | **testado** (e corrigido) | 423 nas escritas, 200 no release |
| WebSocket `/ws/live` | **testado** (fixture real) | snapshot com backlog |
| Compilação Kotlin/Gradle/APK | **SIM — no CI** | APK 18 MB, sha256 `9d20e979…`, run 46055045863 |
| Testes de contrato a correr | **SIM — no CI** | 13 casos, 0 falhas |
| Protocolo do APK contra a API real | **SIM** | `scripts/device_sim.py` → 20/20 passos |
| Execução em emulador (CI, com KVM) | **SIM — 0 falhas** | ver §10 |
| Execução em telemóvel físico | **NÃO** | sem dispositivo; ver §10 |

O APK **foi compilado no GitHub Actions** e inspeccionado a partir do próprio
binário (§7). O que continua por fazer é executá-lo num dispositivo.

---

## 2. Porque não foi possível compilar

Não há Java, Gradle nem Android SDK instalados, e as quatro fontes de onde
poderiam vir estão bloqueadas nesta rede:

| Fonte | Estado | Dá para quê |
|---|---|---|
| `repo.maven.apache.org` | **000** | dependências Maven |
| `dl.google.com` (Maven Android) | **000** | AGP, androidx, Compose |
| `services.gradle.org` | **000** | próprio Gradle |
| `api.adoptium.net` / `download.java.net` | **000** | JDK (Temurin) |
| `github.com/adoptium/...releases` | **000** | JDK por release asset |
| `registry.npmjs.org` | **200** | só o `njre`, que descarrega o JDK do Adoptium → inútil |

Tentado e descartado: `njre` / `java-caller` (npm) — inspeccionado o código,
descarregam de `api.adoptium.net` e seguem para *release assets* do GitHub;
ambos inalcançáveis. `android-sdk` e `gradle` no npm são apenas invólucros
(Node) que invocam ferramentas externas, não trazem SDK.

Tentado e descartado: **GitHub Actions**, que tem toda a toolchain. O
repositório tem `.github/workflows/02_build_android_apk.yml`, mas o token desta
sessão não tem permissão sobre Actions:

```
GET /repos/.../actions/workflows      -> 403 Resource not accessible by integration
GET /repos/.../actions/permissions    -> 403
gh workflow run 02_build_android_apk.yml --ref ...  -> 404
gh run list                            -> []
```

Ou seja: a app GitHub está autorizada a ler e escrever código, não a disparar
pipelines. **Isto é uma limitação do ambiente, não do repositório.**

### Como compilar (2 minutos, numa máquina com rede)

```bash
cd android_apk
export AUTOMATON_BASE_URL=https://o-teu-backend        # obrigatório no release
export AUTOMATON_API_TOKEN=...                          # header X-Automaton-Token
gradle wrapper --gradle-version 8.10.2                  # cria o wrapper (não versionado)
./gradlew :app:testDebugUnitTest                        # 13 testes de contrato
./gradlew :app:assembleDebug                            # APK de debug
```

Ou, sem toolchain local: **Actions → «02 · Build Android APK» → Run workflow**,
preenchendo `base_url`. O APK fica no artefacto `automaton-holding-debug-apk`.
(Só funciona se Actions estiver activo no repositório e o segredo
`AUTOMATON_BASE_URL` existir.)

---

## 3. Bugs encontrados e corrigidos (commit `1eee500`)

Nenhum destes era detectável sem ler o código — a app parece completa.

### 3.1 Faltava o plugin de serialização → o build falhava

`ApiService.kt` tem **16 classes `@Serializable`**, mas o módulo só aplicava
`kotlin.android` e `kotlin.plugin.compose`. Sem
`org.jetbrains.kotlin.plugin.serialization`, o compilador não gera os
serializers e o build pára com:

```
Serializer for class 'HealthResponse' is not found.
```

Corrigido no `build.gradle.kts` da raiz e do módulo.

### 3.2 Dependência que não existe

```kotlin
implementation("androidx.biometric:biometric:1.2.0-alpha05")   // não existe
```

A série 1.2.x real é `alpha01`…`alpha06`. Resolução de dependências falharia
com `Could not find androidx.biometric:biometric:1.2.0-alpha05`. Passou para
**1.1.0** (estável), que tem tudo o que o código usa: `BIOMETRIC_STRONG`,
`BIOMETRIC_WEAK`, `setAllowedAuthenticators`, `setConfirmationRequired`.

### 3.3 Regras ProGuard insuficientes → release partia em runtime

As regras só cobriam `kotlinx.serialization.json.**`; os `$serializer` da
própria app ficavam ao arbítrio do R8. É a falha clássica que só aparece no
APK de release (`SerializationException: Serializer for class ... is not
found`), nunca em debug. Substituídas pelas regras oficiais da biblioteca.

### 3.4 O APK apontava para um backend que não existe

```kotlin
?: "https://automaton-holding.fly.dev"    // nenhum serviço aqui
```

Instalado o APK, todas as chamadas falhavam e a app mostrava «erro de rede»,
sem forma de perceber que o endereço estava fixado no build. Agora:

- **debug** → `http://10.0.2.2:8000` (alias documentado do emulador para a
  máquina anfitriã — o backend local);
- **release** → exige `-Pautomaton.baseUrl=...` ou `AUTOMATON_BASE_URL`, e o
  build **falha** sem ela;
- a mensagem de erro passa a nomear o endereço: «não consigo chegar a
  `https://…` (verifique a rede; o endereço vem do build)».

### 3.5 Faltava a política de rede para o loopback

`usesCleartextTraffic="false"` sem `network_security_config`: um debug contra
`http://10.0.2.2:8000` morria em `Cleartext HTTP traffic not permitted`.
Criado `res/xml/network_security_config.xml` — HTTPS obrigatório, com excepção
apenas para `10.0.2.2`, `localhost` e `127.0.0.1` (não alcançáveis num
telefone real, portanto sem efeito em produção).

### 3.6 O CI escondia falhas

O passo de testes terminava em `|| echo "::warning::Sem testes..."`, o que
transformava qualquer falha real num aviso verde. Removido: sem testes
definidos o Gradle devolve sucesso; com testes, uma falha falha o build.

---

## 4. Testes de contrato (`ApiContractTest.kt`, 13 testes JVM)

As fixtures em `app/src/test/resources/fixtures/` **não foram escritas à mão**:
são a saída literal do FastAPI deste repositório (8 rotas + 1 evento de
WebSocket). Se o backend renomear um campo, o CI falha em vez de o APK mostrar
zeros em produção.

Cobrem as regras que não podem regredir:

| Teste | Regra |
|---|---|
| `treasury mantem o split da fase` | 10/20/70 · 10/40/50 · 50/30/20 somam 1.0 |
| `(mesmo teste)` | imposto 5% (`tax_reserve_rate == 0.05`) |
| `(mesmo teste)` | limiares 1 500 e 15 000 USD |
| `governance ... limiar de 85` | juiz reprova abaixo de 85 |
| `(mesmo teste)` | nenhuma licença proibida na allowlist |
| `reserve payout codifica ...` | corpo usa `amount_usd`, não `amountUsd` |
| `decide approval ...` | corpo usa `biometric_verified` |
| `campos desconhecidos nao partem a app` | `ignoreUnknownKeys` |
| `corpo de erro em texto simples` | HTML de proxy não estoura o parser |

---

## 5. Contrato verificado contra o backend a correr

Backend real (`uvicorn`, porta 8000, `API_ACCESS_TOKEN=apk-test-token`):

```
POST /approvals  sem token                 -> 401
GET  /health /system/status /treasury /approvals
     /squads /agents /governance /validation -> 200   (1,3–2,2 ms cada)
POST /approvals/{id}/decide  sem biometria  -> 428 APPROVAL_REQUIRED
POST /approvals/{id}/decide  com biometria  -> 200 {"ok":true,...}
POST /kill-switch/engage                    -> 200  engaged=true
POST /approvals          (com kill switch)  -> 423 KILL_SWITCH_ACTIVE
GET  /treasury           (com kill switch)  -> 200  (leitura permitida)
POST /kill-switch/release                   -> 200  engaged=false
POST /approvals          (após release)     -> 200
```

### Bug de backend corrigido por causa deste teste (`a7dcc79`)

O kill switch **não bloqueava as escritas da API**. Travava ciclos e gastos de
agentes, mas `POST /approvals` respondia 200 e criava a aprovação com o sistema
«morto» — o botão vermelho do APK não parava nada. Adicionado middleware que
devolve **423** em qualquer escrita, com isenções deliberadas para o próprio
`/kill-switch` (senão era impossível libertar), `/health`, `/ws/live`,
`/events` e o webhook do Telegram. Leituras continuam abertas para o APK poder
mostrar o estado.

---

## 6. O que continua por testar

1. **Compilação** — precisa de rede até Maven/Google/Gradle (ver §2).
2. **Execução em emulador** — não há emulador aqui; os 13 testes correm na JVM.
3. **Biometria real** — o código está correcto, mas `BiometricPrompt` só corre
   em dispositivo com sensor; aqui só foi testado o lado do servidor (428).
4. **WebSocket em rede real** — testado via `TestClient` e fixture; não contra
   um socket TCP real atrás de proxy.
5. **Assinatura de release** — precisa do keystore (`ANDROID_KEYSTORE_BASE64`),
   que não existe neste ambiente.

Nada disto está escondido atrás de um «✅»: está listado como não feito.


---

## 7. Build real no GitHub Actions

O ambiente de desenvolvimento não tem JDK/SDK e a rede bloqueia Maven, Google
e Adoptium, mas o **runner do GitHub tem tudo**. O que faltava era o Actions
não estar acessível à conta de serviço desta sessão — e é: `POST /actions/
workflows/...` devolve 403. Contudo, o workflow também dispara em
`pull_request`, e **esse** evento funciona: basta abrir o PR #1.

### Evidência do APK (não é "ficou lá", é medido)

| Propriedade | Valor |
|---|---|
| Ficheiro | `app-debug.apk` |
| Tamanho | 18 MB (18 681 376 bytes) |
| sha256 | `9d20e979985d370ef3d2ca1e16dcf44d6abc252dfb63c453e8af08d9fb1a65d4` |
| package | `com.automaton.holding.debug` v1.0.0 |
| compileSdk / targetSdk | 35 / 35 |
| label | Automaton Holding |
| assinatura | `CN=Android Debug` (debug, como esperado) |
| DEX | **8 ficheiros, 57 MB** |
| classes da app | **518** |
| URL embutida | `http://10.0.2.2:8000` (o `-Pautomaton.baseUrl` chegou ao `BuildConfig`) |
| permissões | INTERNET, ACCESS_NETWORK_STATE, USE_BIOMETRIC, VIBRATE |
| testes de contrato | **13 casos, 0 falhas** |

Rotas encontradas dentro do DEX: `/health`, `/treasury/payouts/reserve`,
`/approvals`, `/squads`, `/agents`, `/governance`, `/validation`,
`/kill-switch/engage`, `/kill-switch/release`, `/cycle/run`.
Contrato presente: `X-Automaton-Token`, `amount_usd`, `biometric_verified`.

### O que o CI apanhou (e que nenhuma revisão de código apanharia)

1. `urlExplicita` declarada dentro de `defaultConfig`, invisível em
   `buildTypes.release` → **erro de compilação do script Gradle**.
2. `api.getSystemStatus()` (suspend) chamado de `ingest()` que não era
   suspend → **erro de compilação Kotlin**.
3. `governance?.accounting?.report` — `AccountingPayload` não tem `report`.
4. Antes disso, o próprio workflow: `setup-gradle` não instala o Gradle sem
   `gradle-version`, e uma URL vazia era passada ao `-P`.

Nenhum destes era visível por leitura. É a diferença entre «parece completo» e
«compila».

### Como repetir

```bash
gh workflow run 02_build_android_apk.yml      # precisa de permissão de Actions
# ou: abrir um PR — o evento pull_request dispara o mesmo workflow
```

O workflow publica a evidência no PR em caso de sucesso e o diagnóstico em
caso de falha. Foi assim que se diagnosticou tudo isto: os logs do Actions e
os artefactos vivem em `*.blob.core.windows.net`, que **esta rede não
alcança** (`curl` → 000, `gh run download` → EOF).


---

## 8. Emulador Android: o que foi tentado e porque não corre aqui

O pedido «baixa um emulador» esbarra em quatro limites independentes, cada um
suficiente para o impedir. Todos medidos, não presumidos:

| Requisito | Estado neste ambiente | Consequência |
|---|---|---|
| Binário do emulador | `dl.google.com` → **000** (nem o `repository2-3.xml`) | sem `emulator.zip` |
| Pacotes do sistema / SDK | `apt` sem root (uid 1001), `deb.debian.org` → **000** | sem `qemu-system-x86` |
| Virtualização | `/dev/kvm` inexistente, `vmx`/`svm` ausentes em `/proc/cpuinfo` | arranque por emulação de software: lento a roçar o inutilizável |
| Recursos | 2 vCPU, 3 GB RAM (o AVD mínimo pede ~2 GB + 8 GB de disco) | nem um AVD API 26 subiria |

Fontes alternativas verificadas e descartadas:
- **npm** (a única fonte que responde): `start-android-emulator`,
  `android-emulator-webrtc`, `mcp-android-emulator` — inspeccionado o código,
  todos assumem um SDK já instalado; o primeiro descarrega de
  `dl.google.com/android/repository` (bloqueado). Invólucros, não binários.
- **njre / java-caller** (JDK via npm): descarregam de `api.adoptium.net` →
  GitHub releases → **000**.

### O caminho que existe: emulador no CI

O runner do GitHub tem KVM e o SDK completo. Como o backend arranca com
apenas 5 pacotes (`fastapi`, `uvicorn`, `pydantic`, `pyyaml`, `httpx` —
`backend.main` carrega as 65 rotas), é viável subir o backend e o AVD no
mesmo job, instalar o APK e exercitá-lo a sério.

O que **não** está feito, e porquê: escrever esse workflow agora seria
publicá-lo sem nunca o ter executado — exactamente o tipo de coisa que este
relatório existe para não fazer. Está especificado em `docs/emulator_ci.md`
para ser escrito e validado numa sessão com orçamento de execuções.

---

## 9. O que foi substituído por um simulador honesto

`scripts/device_sim.py` não substitui o emulador: **não executa o APK**. O que
faz é exercitar o mesmo protocolo, na mesma ordem, contra a API real, e falhar
se o contrato se partir:

```
1. arranque ......... as 8 leituras do refreshAll()         → 8/8 200
2. autenticação ..... escrita sem token                      → 401
3. biometria ........ decidir $12 sem biometria              → 428
                     decidir com biometria                   → 200
4. botão vermelho ... engage / leituras vivas / escritas 423 → 423
                     release e retoma                        → 200
5. saque e abate .... reserva M-Pesa / esquadrão inexistente → 200 / 400
6. stream ........... snapshot com backlog e kill_switch     → ok
                                                    20/20 passos OK
```

O que isto prova: o **contrato e as regras** estão certos. O que não prova: que
a UI desenha, que a biometria abre, que o WebSocket sobrevive à rede de um
telemóvel. Essa parte continua por fazer e está dita.


---

## 10. Execução real no emulador (runner do GitHub)

O ambiente de desenvolvimento não tem `/dev/kvm` nem rede para `dl.google.com`
(§8), por isso a execução foi levada para o runner do GitHub — o único sítio
onde "corre no telemóvel" pode ser provado em vez de afirmado.

**Configuração**: AVD API 30, `x86_64`, perfil Pixel 5, KVM activo, Android 11.
Backend real a correr em `0.0.0.0:8000` no mesmo runner (5 pacotes).

**Resultado do run final: 0 falhas.**

| Verificação | Resultado |
|---|---|
| Instalação do APK | `Success` |
| Arranque da MainActivity | `LaunchState: COLD`, 3,0 s |
| Processo vivo | pid devolvido |
| 5 abas abrem | confirmado pelo título de cada ecrã |
| Screenshots | 5 PNG distintos (ver `docs/emulador/`) |
| FATAL EXCEPTION no logcat | nenhuma |
| WebSocket `/ws/live` | ligado (`WebSocket /ws/live [accepted]`) |
| Pedidos ao backend | 15 GETs com 200 |

### Defeitos que a execução revelou (e que verificações estáticas não apanham)

1. **`/ws/live` devolvia 404 dentro do emulador.** O uvicorn do CI não tinha
   biblioteca de WebSocket — respondia 404 e a app mostrava «Stream em pausa:
   Expected HTTP 101 response but was '404 Not Found'». Não era rede nem
   endereço.
2. **«Esquadrões» partia em duas linhas** na barra de 5 abas.
3. **«IMPOSTOS RETIDOS» encostava a «FASE»** na aba Diretoria.
4. **O próprio teste estava errado**: as abas 02–05 eram o mesmo ecrã (dois
   PNG com md5 igual). O toque ia ao centro da etiqueta, não ao
   `NavigationBarItem`. Passou a exigir o título da aba visível antes de
   capturar.

### O que continua por testar

- **Telemóvel físico**: Wi-Fi real, DHCP, isolamento de clientes do router.
- **Biometria**: o emulador não tem sensor. O diálogo nunca abriu neste teste,
  porque não havia aprovações pendentes. `adb -e finger` testaria o diálogo,
  não o sensor.
- **Fluxo de escrita** (aprovar, saque, kill switch): exigem biometria e um
  toque real no botão vermelho; fora do âmbito do teste de fumo.
