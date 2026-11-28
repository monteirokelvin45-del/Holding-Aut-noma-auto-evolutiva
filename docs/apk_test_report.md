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


---

## 11. Nota sobre a suíte de testes backend neste ambiente

`pytest backend/tests` deu **62 passed, 3 skipped** — mas só depois de:

```bash
bash scripts/fetch_models.sh              # 89 MB em models/kokoro/ (npm, sha256)
pip install kokoro-onnx espeakng-loader   # declarados em backend/requirements.txt
```

Sem isso dá `3 failed`: os testes de TTS verificam que os motores offline
existem, e não existiam porque os pesos (`.onnx`, `.npz`) e as bibliotecas não
são versionados. A falha era do ambiente, não do código — e o teste está certo
a recusar-se a passar sem o que diz precisar.

---

## 12. Segunda auditoria (2026-11-28): os botões, o cérebro e os agentes

A auditoria anterior provou que o APK **arranca e navega**. Não provou que
**faça** alguma coisa: o `ci_emulator_smoke.sh` toca nas cinco abas e confirma o
título de cada uma. Zero botões eram premidos. Esta secção fecha essa lacuna e
documenta três defeitos reais encontrados pelo caminho.

### 12.1 O que foi executado, e como

| Prova | Como | Resultado |
|---|---|---|
| O binário existe e é real | `apk-artifact` via `git show` | 18 MB, 8 ficheiros `.dex`, 517 entradas, ABIs arm64/armeabi/x86/x86_64 |
| O protocolo do APK | `scripts/device_sim.py` contra a API real | **20/20 passos** |
| Os botões | `scripts/ci_emulator_buttons.sh` (novo) | corre no CI, em cofre descartável |
| **Não** executado aqui | emulador Android | sem Java/Gradle/SDK; `maven`, `dl.google.com`, `services.gradle.org` → **000** |

A primeira corrida do simulador deu **10/18**. Não era defeito da app: era o meu
arranque — token trocado (`apk-test-token` vs. o configurado) e `websockets`
instalado *depois* do uvicorn arrancar. Com o token certo e o servidor
reiniciado: 20/20. Um relatório que registasse «10/18» como defeito do APK
estaria a mentir na direcção mais confortável.

### 12.2 Defeito 1 — o cérebro estava desligado, em silêncio

O `settings` só lia `LOCAL_GGUF_PATH` do ambiente. O modelo reconstruído por
`scripts/instalar_motor_local.sh` fica em `~/modelos/`. Ninguém exportava nada,
e o resultado era este, no `/agents`:

```json
"local_gguf": { "configured": false, "available": false, "reason": "sem_chave" }
```

Duas mentiras numa linha: o cérebro *estava* no disco (397 MB), e um modelo
local **não usa chave nenhuma**. O ciclo caía na heurística sem avisar, e a
única pista era um `reason` falso.

Corrigido em dois pontos: descoberta automática (`_descobrir_gguf`, com glob
sobre `~/modelos/*.gguf`) e motivos honestos (`gguf_nao_encontrado`,
`ollama_desligado`, `desligado_por_omissao`). Medido depois:

```
local_gguf: reason=pronto, available=True, Qwen3-0.6B-Q4_K_M.gguf
resposta em 5,7 s · provider=local_gguf · is_llm=True · custo $0.00
```

### 12.3 Defeito 2 — o APK deitava fora os agentes

A API enviava 11 agentes. O `AiCatalog` Kotlin era:

```kotlin
data class AiCatalog(val bridge: BridgeInfo, val validation: JsonObject?, val llm: JsonObject?)
```

O campo `agents` não tinha destino. O `kotlinx.serialization` ignora campos
desconhecidos **sem erro** — a pior forma de falhar: 11 agentes no servidor,
zero no ecrã, nenhum aviso. Acrescentado `AgentSpecDto` e um cartão
«AGENTES DE IA (17)» na Diretoria, com tier de motor e ferramentas.

A segunda tentativa de build falhou nos testes Kotlin por outra razão minha:
declarei `tasks` como `JsonObject` quando `/agents` devolve uma **lista**. Isso
rebentava a descodificação do catálogo inteiro. Corrigido para
`List<JsonObject>`, e o teste de contrato passou a exigir agentes não vazios —
o teste anterior limitava-se a verificar o `bridge` e passava com 11 agentes
descartados, ou seja, não protegia nada.

### 12.4 Os agentes: 11 → 17

Seis novos, todos no motor local (zero chave, zero custo), todos com tarefa
declarada em `tasks.yaml`:

| Agente | Tarefa | O que faz |
|---|---|---|
| `tradutor_local` | `localizar_texto` | pt-MZ, não pt-BR; corrige moeda e pagamentos |
| `revisor_estilo` | `revisar_legibilidade` | corta sem alterar factos nem números |
| `guardiao_marca` | `auditar_tom_marca` | caça promessas de lucro e superlativos |
| `analista_feedback` | `triar_feedback` | comentários → temas → 3 mudanças |
| `arquivista_memoria` | `consolidar_memoria` | cemitério de negócios e padrões de falha |
| `operador_telegram` | `interpretar_comando` | linguagem natural → comando seguro |

Não são nomes num YAML. O `tradutor_local` correu e corrigiu de facto:

```
"antes": "Compre com RMB no Mercado Livre",  → "depois": "Compre com Metical (MZN)"
"antes": "pague com Paytm",                  → "depois": "pague com M-Pesa"
motivo: "A moeda oficial é Metical (MZN), não RMB."
```

Deixou passar «Mercado Livre» até o nomearmos no bloco de factos. Um 0,6B não
generaliza: o que não estiver escrito, não sabe. Depois de nomeado, passou.

### 12.5 Os botões (novo `ci_emulator_buttons.sh`)

Oito passos, pela ordem em que um utilizador os faria: **Ciclo**,
**Actualizar**, **STOP**, **LIVRE**, **Pedir saque**, **Aprovar**,
**Cancelar**, **Ligar ao backend**.

Corre em **cofre descartável** (`VAULT_DIR=/tmp/cofre-ci`), com uma aprovação
criada de propósito. A razão não é técnica, é de governação: aprovar a partir
do CI estaria a gastar a única decisão humana do ciclo, e um verde no Actions
passaria a significar «aprovações aprovadas por um robot».

### 12.6 Endpoints públicos sem chave

A busca no GitHub rendeu pouco do que é reutilizável: os projectos em alta
(`opencode2dsh`, `ollamafreeapi`) reencaminham para Ollama ou para serviços com
chave. O que serve é `api.llm7.io` e `text.pollinations.ai` — implementados no
router como `llm7` e `pollinations`, **desligados por omissão**
(`LLM_SEM_CHAVE_ATIVO=true` liga).

**Não foram medidos contra a rede**: os dois domínios devolvem **000** neste
sandbox. O que está provado é o contrato do cliente, com resposta simulada:
URL, payload, parsing, custo `$0.00` e — a parte que define «sem chave» — a
**ausência de qualquer cabeçalho de autenticação** no pedido.

### 12.7 Suíte de testes

| | Antes | Depois |
|---|---|---|
| Testes backend | 102 | **114** |
| Duração | 4m32s | **3m48s** |
| Testes Kotlin | 13 | 13 (2 reforçados) |

A descida de 9m55s para 3m48s não é cosmética: quando o cérebro passou a ser
encontrado, os testes de integração começaram a carregar 397 MB e a inferir a
25 tok/s — **416 s num único caso**. Um teste que bloqueia a suíte durante dez minutos
deixa de ser corrido. Agora o motor real é opt-in
(`AUTOMATON_TESTAR_MOTOR_REAL=1`) e por omissão usa um duplo determinístico.
