# Auditoria — tinta por tinta

Data: 2026-11-28. Âmbito: tudo o que a integração PayPal tocou, mais o que
estava por baixo e apareceu durante o trabalho.

Método: não "correr os testes e ver verde". Cada secção diz **como** se
verificou e **o que falhou**, incluindo os erros que eram meus.

---

## Resumo

| Eixo | Antes | Depois |
|---|---|---|
| Testes do backend | 114 | **194** |
| Mutações apanhadas | — | **14/14** |
| Ferramentas declaradas vs. implementadas | 14 em falta | **1 em falta, 11 degradadas, 37 prontas** |
| Rotas PayPal | 0 | 6 (+ cartão no APK) |
| Bugs reais encontrados | — | **11** |
| Erros no próprio teste | — | **9** (corrigidos) |
| Avisos do pyflakes | 45 | **13** (só variáveis de diagnóstico) |

---

## 1. Sintaxe e compilação

```bash
python -m compileall -q backend      # exit 0
pyflakes backend                      # 45 avisos → 0 nos ficheiros novos
```

Os 45 avisos de `pyflakes` eram todos pré-existentes. Limparam-se os
imports mortos e, no meio deles, encontrou-se **um bug real** (§4).

---

## 2. Bugs reais encontrados

### 2.1 Seis na integração PayPal (escondidos pelo dry-run)

Nenhum destes era visível antes de haver credenciais: o código parava em
`if not self.configurado` e nunca chegava ao SDK.

| # | Defeito | Sintoma |
|---|---|---|
| 1 | `PaypalServersdkClient(client_id=..., client_secret=...)` | `TypeError: unexpected keyword argument` |
| 2 | `from_environment(client_id=...)` | `TypeError` em `clone_with` |
| 3 | `environment="sandbox"` (string) | `KeyError: 'sandbox'` ao montar URL |
| 4 | `orders.create_order(body=...)` | `TypeError` — a API é `options={"body": ...}` |
| 5 | `orders.get_order(order_id)` | 404 — e a chave é `id`, não `order_id` |
| 6 | `_apontar_para` copiava `config.environments` | Atributo de **classe**, não de instância: a cópia era inútil |

### 2.2 Dois de dinheiro (encontrados com o caminho a correr)

**a) Retry de captura nunca registava receita.** Um segundo
`capture_order` devolve `ORDER_ALREADY_CAPTURED`, não `COMPLETED`. Tratado
como erro, a receita ficava por registar **para sempre** — e o retry é
exactamente o que acontece quando a rede falha na primeira tentativa.
Correcção: perguntar o estado antes de desistir.

**b) A mesma `external_ref` entrava duas vezes no cofre.** O cofre
acreditava ter recebido o dobro. Provado antes/depois:

```
antes:  receita acumulada = 39.80   (duplicado)
depois: receita acumulada = 19.90   (idempotente)
```

A chave de deduplicação é o par **(origem, referência)**, não a
referência sozinha: o id `123` de um saque M-Pesa não tem nada a ver com
o `123` de uma cobrança PayPal.

### 2.3 Um bug adormecido no `demand_validator`

```python
def _passou_metade_da_janela(test: Any, deadline: Optional[datetime]) -> bool:
                                                        # ^^^^^^^^ não importado
```

Com `from __future__ import annotations` a anotação nunca é avaliada, por
isso importar o módulo funcionava. Qualquer introspecção de tipos rebentava:

```
>>> typing.get_type_hints(DemandValidator._passou_metade_da_janela)
NameError: name 'datetime' is not defined
```

FastAPI, Pydantic e CrewAI fazem esta introspecção. Não era um teste a
falhar: era um bug à espera do primeiro sítio que olhasse para a assinatura.

---

## 3. Cobertura das ferramentas dos agentes

`GET /agents/tools` compara o que os YAML prometem com o que existe.

**A descoberta que mudou o diagnóstico:** quase todas as "14 em falta" já
existiam, com outro nome.

| Declarado | Já era | 
|---|---|
| `ffmpeg` | `motion_renderer.ffmpeg` |
| `scancode` | `legal_department.scan_path_with_scancode` |
| `rubric_scorer` | `eval_judge.evaluate` |
| `landing_page_generator` | `demand_validator.render_page` |
| `metrics_engine` | `accountant.financial_report` |
| `github_api` | `scout_hunter._fetch_github_trending` |

Não faltava código — faltava a ligação. Resultado: 49 nomes mapeados,
37 prontas, 11 degradadas (falta chave/pacote), **1 em falta**
(`metagpt_pipeline`, com o motivo escrito).

Erros meus aqui: inventei `shared_memory.remember` (o método é
`list_entries`) e procurei `liscscan` quando o YAML escreve `licscan`.
Ambos corrigidos; o primeiro tinha um teste a passar por cima de um método
inexistente, porque nada o chamava.

---

## 4. Testes — os cinco tipos

| Tipo | Ficheiro | N |
|---|---|---|
| Contrato (dry-run) | `test_paypal.py` | 12 |
| Ponta a ponta (SDK real contra PayPal falso) | `test_paypal_e2e.py` | 10 |
| Caos, concorrência e atomicidade | `test_dinheiro_caos.py` | 16 |
| Contrato HTTP + segurança | `test_paypal_api.py` | 16 |
| Tools do CFO + registo | `test_cfo_tools.py` | 26 |

**Total: 194 testes, 0 falhas.**

### 4.1 Mutação — a prova de que os testes servem

Estrago deliberado no código, 14 vezes. Primeira passagem: **10/14**. As
quatro que sobreviveram eram buracos reais da suíte:

| Mutação | Porque sobreviveu | Teste acrescentado |
|---|---|---|
| dedupe ignora origem | só se testava a mesma origem | mesma ref em origens diferentes → 2 entradas |
| tudo "pronta" | nada testava `degradada` | requisito em falta **obriga** a degradada |
| falha silenciosa | nada chamava `invocar()` | ferramenta em falta levanta, não devolve `None` |
| aceita qualquer moeda | teste de moeda não existia | `MZN/ZAR/GBP/xyz/""` → recusado |

Segunda passagem: **14/14**.

### 4.2 Prova inversa

Remover o bloco de deduplicação faz o teste das 20 threads falhar, e
restaurá-lo fá-lo passar. Um teste que nunca falha não é um teste.

---

## 5. Auditoria de segurança

```bash
grep -rE "(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,})" backend/ android_apk/
```

**Nenhuma chave em código.**

**6 rotas de escrita sem `require_token`.** Verificadas uma a uma:

| Rota | Veredicto |
|---|---|
| `/telegram/webhook` | **Seguro.** `is_allowed(chat_id)` com lista explícita; sem chats configurados, tudo é recusado (default negado). |
| `/validation/{id}/event` | Público **por desenho** — chamado pela landing page. |
| `/governance/cemetery/check`, `/validation/judge`, `/platform/research`, `/security/scan` | Chamados **dentro do processo**, por código Python. Não são chamadas remotas da app (verificado: 0 referências em `ApiService.kt`). |

Nenhuma expõe dinheiro, segredos ou execução de código. Não se acrescentou
token para não partir chamadas internas — ficam documentadas como decisão.

**Rotas novas do PayPal:** as duas de escrita (`/paypal/cobrancas`,
`/paypal/capturar`) têm `Depends(require_token)`. Testado: sem token → 401.

**Fuga de credenciais:** um teste percorre as respostas JSON das rotas novas
à procura de chaves com valor. `paypal_client_secret: false` passa (é um
booleano); `paypal_client_secret: "A21AA..."` falharia.

---

## 5.1 Código morto e ligações em falta

Encontraram-se dois casos em que o código existia mas **nada a ele chegava**:

**a) O `CrewBridge` nunca entregava ferramentas ao CrewAI.** O `Agent(...)`
não tinha `tools=`. O defeito corrigido no registo reaparecia uma camada
mais fundo: o agente lia «podes usar registrar_saques» e não tinha função
nenhuma. Agora `_tools_crewai()` liga-o — CFO com 3, scout com 4.

**b) `vault_store.transaction` fazia um `deepcopy` que ninguém usava.** O
nome `snapshot` sugeria rollback; não havia rollback. Verificou-se que a
atomicidade era real (a escrita acontece depois do `yield`, e uma excepção
impede-a) e o trabalho a fingir saiu — substituído por três testes:

| Teste | O que prova |
|---|---|
| excepção a meio | nada gravado |
| contexto abandonado (GeneratorExit) | nada gravado |
| escrita normal | **grava mesmo** |

Sem o terceiro, os outros dois passariam com o cofre permanentemente
congelado e não provavam nada.

---

## 6. Auditoria APK

### 6.1 Um erro de compilação que o CI apanhou

```
e: MainActivity.kt:122:19 Property delegate must have a 'getValue(...)' method
```

A mensagem aponta para a delegação; a causa era outra: faltavam
`import PayPalStatus` e `import CobrancaResponse` no `HoldingViewModel`.
Uma mensagem de erro de Kotlin raramente diz onde está o erro.

### 6.2 O erro silencioso que quase passou

Escrevi `orderId`, `approvalUrl`, `dryRun` e `sacarParaMocambique` **sem
`@SerialName`**. O `Json` do projecto não converte snake_case: os campos
leriam vazios. O aviso do saque desapareceria da app — em silêncio, que é
exactamente o que ele não pode fazer.

O ficheiro tem 109 `@SerialName`. A convenção era explícita; eu não a
segui até a verificar. `PayPalContractTest` existe em parte para apanhar
isto.

### 6.3 Verificação de símbolos

Sem toolchain Android neste sandbox (nem `java`), escreveu-se um validador
que cruza símbolos Compose/Kotlin usados contra imports:

```
0 símbolo(s) em falta   (5 ficheiros)
```

Os dois primeiros "avisos" eram falsos positivos — uma palavra em comentário
e um parâmetro chamado `border`. O validador foi refinado até não gritar
sem motivo; um verificador que mente deixa de ser usado.

---

## 7. Erros que foram meus, no próprio teste

Documentados para não se repetirem:

1. `cp ficheiro ficheiro` (mesmo caminho) falhou e pintou o CI de vermelho.
2. Servidor falso não lia o corpo do token → `grant_type=client_credentialsPOST`.
3. Servidor falso usava `/v1/payments/checkout/orders`; a rota real é `/v2/checkout/orders`.
4. `monkeypatch.setattr` em `Settings` frozen → `FrozenInstanceError`.
5. IDs do servidor falso repetiam entre testes → colisão de deduplicação.
6. Teste e2e assumia cofre vazio → falhava só na suíte completa.
7. Header `Authorization: Bearer` quando o backend usa `X-Automaton-Token`.

O nº 6 é o mais instrutivo: um teste que passa sozinho e falha em conjunto
não é um teste, é uma coincidência.

---

## 8. O que continua por fazer

1. **Saque PayPal → Moçambique.** Não resolvido pela PayPal. Payoneer,
   Wise (`WiseConnector`, já existe) ou gastar em PayPal. Nenhum código
   resolve isto — é uma limitação da conta, não do programa.
2. **Zero transacções reais.** Tudo provado contra um PayPal falso. A
   primeira cobrança a sério será a primeira vez que o caminho completo
   corre contra a PayPal.
3. **1 ferramenta em falta:** `metagpt_pipeline`. O `micro_saas_squad`
   produz o mesmo artefacto sem ela.
4. **11 ferramentas degradadas** por falta de chave/pacote. Cada uma diz
   qual é a alternativa que está a usar.
5. **A deduplicação de receita cresce linearmente** com o número de eventos
   (procura linear em `revenue_events`). Com dezenas de milhares de eventos
   passa a notar-se. A solução é um índice por `(source, external_ref)`;
   não se fez porque, a este volume, seria optimização sem problema.

---

## 9. Estado final verificado

| Verificação | Resultado |
|---|---|
| `pytest backend/tests` | **194 passed**, 0 falhas |
| Mutações apanhadas | **14/14** |
| `compileall backend` | exit 0 |
| `pyflakes backend` | 13 (variáveis de diagnóstico, por desenho) |
| Símbolos Kotlin sem import | **0** |
| Build do APK (CI) | **success** |
| Emulador (CI) | **success**, 18 verificações OK, 0 falhas |
| Rotas novas no OpenAPI | 6 |
| Segredos em código | 0 |
