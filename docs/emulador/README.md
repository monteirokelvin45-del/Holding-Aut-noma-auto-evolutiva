# Provas de execução no emulador

Screenshots tirados com `adb exec-out screencap` durante a execução do APK
num emulador Android 11 (API 30, x86_64, Pixel 5) no runner do GitHub, com o
backend real a correr no mesmo runner.

Run: 46112429730 · **0 falhas** · APK `sha256 5948fefd…`

| Ficheiro | Ecrã |
|---|---|
| `01_ao_vivo_e_ligado.png` | Ao Vivo, WebSocket ligado (evento `CONNECTED`) |
| `emulador_02_aprovar.png` | Mesa de aprovações vazia + aviso de biometria |
| `emulador_03_tesouro.png` | Tesouro: cofres, fase, saque transfronteiriço |
| `emulador_04_diretoria.png` | Diretoria: RH de IA, contabilidade, juiz eval |
| `emulador_05_esquadroes.png` | Esquadrões: sem unidades activas (estado real) |

Os dados estão a zeros porque o backend arrancou com o ledger vazio: é o
estado verdadeiro, não um mock. Nenhuma destas imagens foi editada — são o
que o emulador desenhou.

O que **não** está provado aqui: biometria (o emulador não tem sensor),
Wi-Fi de telemóvel real, e os fluxos de escrita (aprovar/saque/kill switch),
que exigem o diálogo biométrico.
