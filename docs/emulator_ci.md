# Emulador Android no CI — especificação

O emulador **não corre no ambiente de desenvolvimento** (ver
`docs/apk_test_report.md` §8: sem `/dev/kvm`, sem rede para `dl.google.com`,
sem root). O runner do GitHub tem tudo isso. Este documento especifica o job
que falta escrever e executar — está aqui em vez de no workflow porque um
workflow nunca executado não é uma prova, é uma intenção.

## Porque é viável

- O runner `ubuntu-latest` expõe `/dev/kvm` (aceleração por hardware).
- `reactivecircus/android-emulator-runner` trata do AVD, do boot e do `adb`.
- O backend arranca com **5 pacotes** (`fastapi`, `uvicorn`, `pydantic`,
  `pyyaml`, `httpx`); medido: 65 rotas carregadas.

## Sequência do job

```yaml
job: emulador
  runs-on: ubuntu-latest
  steps:
    1. checkout
    2. setup-java 17 + setup-gradle (igual ao workflow 02)
    3. python: instalar os 5 pacotes do backend
    4. subir o backend em background:
         API_ACCESS_TOKEN=... uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
         aguardar /health responder 200 (máx. 30 s)
    5. instalar o AVD (reactivecircus/android-emulator-runner@v2):
         api-level: 26      # minSdk do projeto
         target: default
         arch: x86_64
         profile: pixel_5
         script: |
           adb wait-for-device
           adb install -r app/build/outputs/apk/debug/app-debug.apk

           # O APK de debug tem BASE_URL=http://10.0.2.2:8000 -> o backend
           # do passo 4 visto de dentro do emulador. Nenhum truque de rede
           # é preciso: 10.0.2.2 é o alias do anfitrião.

           # 1. a app arranca sem crash
           adb shell am start -W -n com.automaton.holding.debug/.MainActivity
           adb shell pidof com.automaton.holding.debug

           # 2. não rebentou: o log não tem FATAL EXCEPTION
           adb logcat -d -b crash | grep -c FATAL_EXCEPTION   # esperado 0

           # 3. o backend recebeu as leituras do refreshAll()
           grep -q 'GET /treasury' backend.log

           # 4. captura de ecrã para o artefacto (prova visual)
           adb exec-out screencap -p > ecra_inicial.png
```

## O que verificar no resultado

| Verificação | Como | Esperado |
|---|---|---|
| Arranca sem crash | `adb shell pidof <pkg>` | PID devolvido |
| Sem exceções fatais | `adb logcat -b crash` | vazio |
| Fala com o backend | log do uvicorn com `GET /treasury 200` | presente |
| UI desenhou | `screencap` não vazio, não branco | PNG com conteúdo |
| WebSocket ligou | `adb logcat \| grep "WebSocket ligado"` | presente |

## Limitações a declarar no relatório

- Biometria **não** é testável no emulador sem `adb -e finger` configurado;
  o emulador suporta impressão digital simulada (`finger touch`), mas isso
  testa o diálogo, não o sensor real.
- O APK de debug é assinado com a chave de debug: serve para este teste, não
  para distribuição.
- Um AVD x86_64 em CI leva ~3–5 min a arrancar; o job deve ter
  `timeout-minutes: 30`.

## Estado

Não escrito, não executado. Escrevê-lo sem o correr seria exactamente o tipo
de «parece que funciona» que este projeto recusa.
