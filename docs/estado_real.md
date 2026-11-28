# Estado real do projeto — 28 de novembro de 2026

Documento de situação. Não é marketing nem roteiro: é o que foi **medido neste
repositório**, com o comando ao lado de cada número. Onde não há medição, está
escrito «não testado».

---

## 1. Em uma frase

A infraestrutura está construída e provada — 18 678 linhas de backend, 4 485 de
app Android, 65 rotas, APK que corre num emulador — e o negócio tem **$0.00 de
receita, 0 eventos de receita, 0 esquadrões e 0 aprovações**. Construiu-se a
fábrica inteira; a linha de produção nunca arrancou porque não há matéria-prima
nem motor.

---

## 2. O que existe e funciona (medido)

| Componente | Números | Como foi verificado |
|---|---|---|
| API backend | **65 rotas**, 18 678 linhas Python | `app.routes` carregado |
| App Android | 4 485 linhas Kotlin, 5 ecrãs, 1 APK | executado em emulador |
| Agentes declarados | **11 agentes, 15 tarefas** | `backend/config/agents.yaml` |
| Testes backend | **62 passed, 3 skipped** | `pytest backend/tests` |
| Testes Kotlin | **20 casos, 0 falhas** | `:app:testDebugUnitTest` no CI |
| TTS offline | espeak-ng **28 ms** (54 580 amostras), Kokoro **2 629 ms** (54 272 amostras) | síntese real executada |
| Protocolo do APK | **20/20 passos** | `scripts/device_sim.py` contra API real |
| Execução no emulador | **0 falhas** | run 46112429730, Android 11 x86_64 |

### Governação (esta parte é sólida)

- **Kill switch** com bloqueio real de escritas (423) e libertação auditada.
- **Gate biométrico**: decidir aprovações devolve **428** sem biometria; **200**
  com ela. Verificado no simulador de dispositivo.
- **Aprovações** com idempotência, sequência e auditoria.
- **Tesouraria** com fases 10/20/70 → 10/40/50 → 50/30/20, 5 % de imposto
  retido, ledger atómico.
- **Saques** com reserva, aprovação e reembolso em falha. Sem credenciais,
  ficam em `dry_run` — nunca fingem sucesso.

---

## 3. O que existe mas **não produz nada** (a parte que importa)

### 3.1 O motor de IA é uma heurística

```
MODELO: heuristic-v1
TEXTO: [MOTOR LOCAL — SEM LLM] Tarefa: general.
Resumo: Qual a melhor estratégia para vender infoprodutos em Moçambique?
Termos-chave: estratégia, infoprodutos, melhor, moçambique, qual, vender
Recomendação: registar como pendente de revisão humana...
```

Isto é a resposta real do `llm_router.complete()` **sem chave de API**. Não
pensa: extrai palavras, devolve um esqueleto e recomenda revisão humana. É
honesto (diz «SEM LLM» na primeira linha), mas significa que os 11 agentes não
raciocinam. `crewai` não está instalado: o `crew_bridge` corre no caminho
«nativo», que é este mesmo motor.

**Consequência:** nenhum agente pode, hoje, descobrir um nicho, escrever um
infoproduto ou avaliar demanda. Não é um bug — é a ausência do cérebro.

### 3.2 Zero receita, zero operação

| Métrica | Valor |
|---|---|
| Receita bruta | **$0.00** |
| Receita líquida | **$0.00** |
| Bolso do Presidente | **$0.00** |
| Imposto retido | **$0.00** |
| Saques | **0** |
| Eventos de receita | **0** |
| Esquadrões com métricas | **0** |
| Aprovações pendentes | **0** |
| Fase | **1** (capital zero, tudo reinvestido) |

O ciclo das 6 horas (`cron: 0 */6 * * *`) está escrito e **nunca correu**: não
há um único run do workflow 01 no histórico do Actions. Mesmo que corresse,
esbarraria no ponto 3.1 e no 3.3.

### 3.3 Todas as saídas de dinheiro estão em dry-run

Nenhuma credencial está configurada — 0 variáveis de ambiente com chaves, 17
campos de credencial vazios em `settings.py`. M-Pesa, eMola, PayPal e Wise
existem como conectores completos e todos devolvem `dry_run` com reembolso.

Isto é o comportamento **correto** e o que sempre se defendeu: um sistema que
finge pagar é pior do que um que não paga.

### 3.4 O que depende de terceiros e nunca foi exercitado

`YouTube`, `Telegram`, `edge_tts`, `chatterbox`, `piper`, publicação em lojas,
Shopify (0 referências). Estão implementados como código; nenhum foi chamado
contra o serviço real. Sem chaves e sem rede aberta, não há como testar.

---

## 4. O APK: o que está provado e o que não está

**Provado** (emulador Android 11, backend real no mesmo runner, 0 falhas):
instala, arranca em 3,0 s, os 5 ecrãs desenham, o WebSocket liga, chegam 15
GETs ao backend, sem uma única `FATAL EXCEPTION`.

**Não provado:**

- **Telemóvel físico.** Nunca. Wi-Fi real, DHCP, isolamento de clientes do
  router — nada disto foi exercitado.
- **Biometria.** O emulador não tem sensor, e o diálogo nunca chegou a abrir
  porque não havia aprovações pendentes.
- **Fluxos de escrita pela UI** (aprovar, sacar, accionar o kill switch com o
  dedo). Exigem biometria.

O endereço do backend já é configurável dentro da app (ecrã «Ligar ao
backend», testa antes de guardar), por isso instalar num telefone já não é um
beco sem saída.

---

## 5. Dívida técnica e fragilidades conhecidas

1. **O sandbox reinicia e leva o `.git` local.** Aconteceu duas vezes nesta
   sessão. O código e o histórico estão no remoto (`0b42225`); artefactos
   ignorados (`dist/`, binários em `tools/`, pesos em `models/`) desaparecem e
   têm de ser reconstruídos (`fetch_adb.sh`, `fetch_models.sh`).
2. **Workflows novos não são registados pelo Actions.** O job do emulador
   teve de ir para dentro do workflow 02, que já existia.
3. **Os logs do Actions são inalcançáveis** desta rede. Foi por isso que se
   criaram os ramos `apk-log` e `emulador-evidencias` — e sem eles cada
   falha custava um run cego.
4. **`main` tem apenas o README.** Todo o projecto vive num ramo
   (`arena/632fbadc-...`), com o PR #2 aberto. O CI já corre nesse ramo; o
   merge para `main` nunca foi feito.
5. **3 testes de TTS exigem ambiente preparado** (pesos de 103 MB + 3
   bibliotecas). Sem isso falham — correctamente, mas falham.

---

## 6. O que falta para o projecto fazer dinheiro

Por ordem de bloqueio, não de esforço:

| # | Bloqueio | O que desbloqueia |
|---|---|---|
| 1 | **Sem motor de IA** | Uma chave (OpenAI/Gemini) ou um LLM local. Sem isto, os agentes são um `grep`. |
| 2 | **Sem canal de venda** | Conta num marketplace, loja ou afiliação. Não há uma única referência a Shopify no código. |
| 3 | **Sem credencial de pagamento** | Conta M-Pesa/eMola de empresa para sair do dry-run. |
| 4 | **Sem primeira receita** | Tudo acima, mais alguém a comprar. |
| 5 | **Sem hardware de teste** | Um telemóvel Android para validar biometria e rede real. |

Nenhum destes é código. É por isso que o código já não é o problema.

---

## 7. Veredicto

**Como sistema de governação financeira, está pronto e é honesto.** O kill
switch funciona, a biometria barra, os saques não fingem, o ledger é atómico, o
APK fala com a API e desenha. Isto foi testado a sério, incluindo num emulador
Android — e os defeitos que apareceram (404 no WebSocket, «Esquadrões» a partir
em duas linhas, rótulos sobrepostos) foram corrigidos e reconfirmados.

**Como negócio autónomo, não arrancou.** Não tem cérebro (heurística), não tem
canal (nenhum), não tem dinheiro (0) e não tem primeira venda (0). A fábrica
está construída e ligada à corrente, mas a linha de montagem está parada à
espera de matéria-prima que o código não pode fabricar.

A frase que resume: **construiu-se a central de comando de uma holding que
ainda não tem nada para comandar.**
