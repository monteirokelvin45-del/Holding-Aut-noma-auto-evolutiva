# Estado real do projeto — 28 de novembro de 2026

Documento de situação. Não é marketing nem roteiro: é o que foi **medido neste
repositório**, com o comando ao lado de cada número. Onde não há medição, está
escrito «não testado».

> **Actualização de 28/11 (tarde) — o motor deixou de ser `grep`.**
> O ciclo corre agora com um LLM real (Qwen3-0.6B Q4_K_M), voz Kokoro-82M em
> português e montagem FFmpeg desenhada frame a frame. Tudo local, **sem
> chaves e sem rede na inferência**. Os números e o texto real estão na
> secção 9. O resto deste documento continua verdadeiro no que respeita a
> infraestrutura; o que mudou foi a linha de produção.

---

## 1. Em uma frase

A infraestrutura está construída e provada — 65 rotas, APK em emulador, 89
testes de backend — e **o ciclo produtivo já produz**: um e-book de 5 capítulos
e um vídeo de 21 s com narração em português, gerados sem chave nenhuma. A
receita continua em **$0.00** porque nada foi aprovado nem vendido: o produto
está na loja e na fila, à espera de decisão humana.

---
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

---

## 9. Motor local: o que mudou em 28/11 (medido)

Tudo abaixo foi executado neste sandbox (3,8 GB RAM, 2 vCPU, sem GPU) e no
runner do GitHub Actions. Comando ao lado de cada número.

### 9.1 O cérebro — Qwen3-0.6B Q4_K_M

| Métrica | Valor | Como |
|---|---|---|
| Provider escolhido | `local_gguf` | `llm_router.complete()` |
| `is_llm` | `True` | idem |
| Custo | `$0.00` | idem |
| Latência (3 runs, mesmo prompt) | 15 615 / 8 400 / 14 120 ms | `latency_ms` |
| Tokens/s | 25,7 / 25,8 / 25,4 → **média 25,6** | tokens ÷ segundos |
| Tamanho do modelo | 396 705 472 bytes | `stat` |
| SHA-256 | `ac2d9771…0d524a` ✅ confere com `provenance.json` | `ensure_local_llm.py` |
| Reconstrução limpa | **41 s** das 5 partes ao hash verificado | idem |

**Texto real do modelo** (não `grep`, não template):

> `<think>` Okay, the user is asking why a freelancer in Maputo should save
> money in dollars… First, I need to consider the economic context of Maputo… `</think>`
> «Freelancing in Mozambique offers flexible opportunities for individuals to
> work remotely and gain skills without needing a permanent job.»

O raciocínio sai em inglês e a resposta em português. É o comportamento do
Qwen3-0.6B: o bloco `<think>` é do modelo, não do nosso código.

### 9.2 A voz — Kokoro-82M com vozes portuguesas

| Métrica | Valor |
|---|---|
| Modelo | `kokoro-q8.onnx`, 92 361 116 bytes, SHA `fbae9257…8a1478` ✅ |
| Vozes | **54**, incluindo `pf_dora`, `pm_alex`, `pm_santa` |
| Voz usada | `pf_dora` (português feminina) |
| Amostragem | 24 000 Hz mono |
| Limite do grafo | **510 fonemas** por chamada (514 falha com «invalid expand shape») |

O limite foi medido por bisseção. É em **fonemas**, não em caracteres: 442
letras de português dão 510 fonemas. Texto maior é dividido em blocos com
pausa de 180 ms.

### 9.3 O vídeo — montagem offline

| Métrica | Valor |
|---|---|
| Ficheiro | `short.mp4`, **2 015 058 bytes** |
| Duração | **21,59 s** |
| Resolução / fps | 1080×1920 (9:16), 30 fps |
| Codecs | h264 High + aac 24 kHz mono |
| Frames desenhados | **646** |
| Tempo de render | 104 372 ms (frames) + 16 205 ms (voz) |
| Custo por frame | 65 ms (era 136 ms antes de desfocar em baixa resolução) |
| Legendas | `legendas.srt` com tempos **medidos** por fala, não estimados |

### 9.4 O ciclo completo

| Passo | Resultado |
|---|---|
| Scout | 8 sinais, 8 viáveis |
| Validação | 3 smoke tests criados |
| Produção | **1 e-book**, 5 capítulos, juiz **85,5/100** |
| Vídeo | juiz **91,3/100**, `publishable: true`, zero blockers |
| Tesouraria | fase 1, `$0.00`, ledger equilibrado |
| Duração do ciclo | **214 s** |
| Erros | **nenhum** |

### 9.5 Loja e fila

- `docs/store/index.html` com **5 produtos**; o novo é
  `guia-pr-tico-de-freelancing-para-jovens-de-maputo-sq_59453be2138a.html`,
  com o vídeo em `docs/store/media/guia-freelancing-video.mp4`.
- Fila `pending_approvals.json`: **3 pendentes**, incluindo
  `apr_d1b1b28d5990` «Publicar: Guia de freelancing (e-book) + vídeo Kokoro 21s».

### 9.6 A ancoragem de factos: o 0,6B deixou de inventar Moçambique

Um modelo de 0,6 mil milhões de parâmetros não é burro — é **ignorante**. Não
sabe qual é a moeda de Moçambique, e em vez de admitir, adivinha. Medido neste
sandbox, antes de qualquer correcção:

| Pergunta | Resposta do modelo 0.6B | Realidade |
|---|---|---|
| Qual é a moeda oficial de Moçambique? | «a **moeda nacional**» | Metical (MZN) |
| Que pagamentos móveis se usam em Maputo? | «**Paytm**, **Mobis**, **Mobis Pay**» | M-Pesa e e-Mola |
| Plataformas para freelancers em Maputo | «**Mercado Livre**» | não opera em Moçambique |

Paytm é indiano. «Mobis» não existe. O erro não se corrige pedindo «sê
factual» — corrige-se **dando os factos**.

`backend/core/grounding.py` guarda o bloco uma só vez e injecta-o em dois
sítios, porque um só não chegava:

- `llm_router.complete()` — todas as chamadas, incluindo as que o CrewAI faz
  por fora do `SquadBase`;
- `SquadBase._default_system()` — quem chamar o LLM por outro caminho.

`ancorar()` é idempotente (dois blocos gastariam contexto sem ensinar nada) e
põe os factos **à frente** da instrução específica: num modelo pequeno, o
início do prompt pesa mais.

O mesmo prompt, depois da ancoragem:

- «A moeda oficial de Moçambique é o **Metical (código: MZN)**.»
- «Em Moçambique os pagamentos móveis mais usados são **M-Pesa e e-Mola**.»

`grounding.problemas()` → `[]` nos dois casos.

Há ainda um **portão no esquadrão de vídeo**: o guião passa por
`_verificar_factos()` antes de ser narrado. O que é inequívoco é corrigido por
substituição directa (`Mozambique` → `Moçambique`, `Paytm` → `M-Pesa`,
`mapa-voz` → `Maputo`); o resto fica registado. A fronteira de palavra é
respeitada, para que «mobilidade» não seja lida como «Mobis».

13 testes novos em `backend/tests/test_grounding.py` fixam o comportamento —
incluindo um que verifica que o *router* ancora todas as chamadas. Se a
ancoragem ficasse a cargo de cada esquadrão, bastava um esquecer-se para o
modelo voltar a inventar.

### 9.7 Ciclo de confirmação, depois da ancoragem

| | Antes | Depois |
|---|---|---|
| Factos errados na narração | Mercado Livre, «moeda nacional», Paytm | **nenhum** (`problemas` → `[]`) |
| Juiz | 91,3/100 | 88,0/100 (hook 90, clareza 90, compliance 90) |
| Vídeo | 21,59 s, 646 frames | **20,17 s** (ffprobe) / 20,12 s (pacote), 1 898 226 B |
| Duração do ciclo | 203 s | **145 s** |
| Custo | $0.00 | $0.00 |

O guião ficou mais honesto e mais genérico — um 0,6B ancorado não fica
eloquente, fica apenas **correcto**. O que continua por resolver é o ponto 1 de
9.6: para escrever com substância sobre freelancing em Maputo, é preciso um
modelo maior, não mais instruções.

### 9.8 O que continua por fazer (honesto)

1. **A qualidade do texto é de um modelo de 0,6B.** Depois da ancoragem já não
   inventa factos (9.6), mas também não tem substância: escreve português
   correcto e vazio. Um modelo de 3B–7B melhoraria isto — precisa de mais RAM
   do que os 3,8 GB medidos. Isto é um limite de hardware, não de código.
2. **O sotaque é brasileiro.** O espeak só tem `pt-br`; não existe `pt-MZ`.
3. **O Scout continua a ler repositórios do GitHub trending** — a fonte mudou
   de "cópia literal" para "proposta gerada sobre um sinal fraco", mas o sinal
   em si continua fraco. Faltam fontes de demanda reais de Moçambique.
4. **Nada foi vendido.** Receita $0.00, 0 vendas. As 3 aprovações esperam
   decisão humana.
