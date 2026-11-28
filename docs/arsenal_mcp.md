# Arsenal de ferramentas — MCP e o que ficou de fora

Última actualização: 2026-11-28.

Documento de decisões. Para cada família de ferramentas diz **o que entrou**,
**o que foi recusado** e **porquê**. Uma lista de 16 repositórios não é um
arsenal; um arsenal tem o que arranca.

---

## 1. O que funciona hoje, sem pedir nada a ninguém

| Ferramenta | Estado | Prova |
|---|---|---|
| Servidor MCP `filesystem` | **A arrancar** | 14 ferramentas descobertas, `list_directory` devolveu conteúdo real |
| Servidor MCP `postgres` | Configurado | `pronto: true` (`npx` presente) |
| 49 ferramentas internas do registo | 37 prontas / 11 degradadas / 1 em falta | `GET /agents/tools` |
| Guardas de dinheiro e publicação | **A bloquear** | `executar("create_payment")` → `executado: false` |

Nenhuma destas precisa de chave. É o resultado prático de uma regra que
parece só filosófica: **o que exige chave não pode ser a base de nada.**

---

## 2. Licenças: 5 recusadas, e o motivo fica escrito

A regra da holding é MIT / Apache-2.0 / BSD / LGPL. Verifiquei cada
repositório na API do GitHub a 2026-11-28:

| Repositório | Licença | Decisão |
|---|---|---|
| `modelcontextprotocol/python-sdk` | MIT | ✅ mas ver §3 |
| `github/github-mcp-server` | MIT | ✅ declarado |
| `makenotion/notion-mcp-server` | MIT | ✅ declarado |
| `microsoft/playwright-mcp` | Apache-2.0 | ✅ permitida |
| `ComposioHQ/composio` | MIT | ✅ (não ligado: precisa de conta) |
| `postalserver/postal` | MIT | ✅ |
| `umami-software/umami` | MIT | ✅ (alternativa ao PostHog) |
| `NanmiCoder/MediaCrawler` | **NOASSERTION** | ❌ não é licença OSI |
| `EvolutionAPI/evolution-api` | **sem licença** | ❌ API devolve `null` |
| `plausible/analytics` | **AGPL-3.0** | ❌ fora da lista |
| `PostHog/posthog` | **NOASSERTION** | ❌ usar Umami (MIT) |
| `dreammis/social-auto-upload` | MIT | ⚠️ **licença boa, uso não** |

### O caso mais interessante: `social-auto-upload`

É MIT — passaria no teste de licença. Mas automatiza upload em plataformas
cujos termos proíbem automação. A licença do código e a permissão da
plataforma são coisas diferentes, e confundi-las é como ter um contrato de
casa em terreno alheio.

Não está declarado em `config/mcp_servers.json`.

### Dois repositórios da lista não existem

`stripe/stripe-agent-toolkit` e `twilio/twilio-mcp` devolvem **404** na API
do GitHub. Podem ter mudado de nome ou estar privados. Não se declara o que
não se verificou — e um `mcpServers` com um comando que não arranca é pior
do que não ter nada: gasta 25 segundos por tentativa e engana quem lê a
configuração.

---

## 3. Porque não se usou o SDK oficial do MCP

Instalou-se o `mcp` 2.3.0 (MIT, oficial). **Partiu o backend:**

```
mcp 2.3.0  →  sse-starlette>=3.0.0  →  starlette>=0.49.1
fastapi 0.115.6 exige starlette <0.42
→ TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'
```

O `TestClient` deixou de conseguir instanciar a app. Revertido e
confirmado: 194 testes voltaram a passar.

Uma dependência **opcional** não pode derrubar o servidor. O protocolo MCP é
JSON-RPC 2.0 sobre stdio; o cliente em `backend/core/mcp_bridge.py` são ~150
linhas e zero risco para o que já funciona. Quando o FastAPI subir de
versão, trocar é uma alteração local a esse ficheiro.

---

## 4. A regra do Jurídico, implementada

> «Toda tool que gasta dinheiro ou publica conteúdo passa pelo
> `pedir_aprovacao` antes.»

`backend/core/agent_tools.py` classifica cada ferramenta em três categorias:

| Categoria | O que faz | Exemplo |
|---|---|---|
| `ler` | Executa | `read_file`, `list_transactions`, `get_order_details` |
| `escrever` | Executa (reversível) | `write_file`, `create_note` |
| `sensivel` | **Não executa.** Cria pedido pendente | `create_payment`, `send_email`, `registrar_saques` |

Prova:

```python
executar("create_payment", {"amount": 500}, agente="cfo")
# {'executado': False,
#  'approval_id': 'apr_...',
#  'o_que_falta': 'O Presidente tem de aprovar. Uma ferramenta que gasta
#                  dinheiro ou publica não corre sozinha.'}
```

### Duas armadilhas que a heurística tinha

**1. `create_payment` passava como «escrita».** O verbo `create` não é
sensível sozinho — criar uma nota não é criar uma cobrança. Foi preciso
acrescentar substantivos de dinheiro (`payment`, `invoice`, `order`,
`saque`, `payout`…).

**2. Ao corrigir isso, bloqueei `read_invoice` e `get_order_details`.** Este
erro é mais grave do que parece: **bloquear consultas é a forma mais rápida
de o utilizador desligar a regra toda.** Quem pede aprovação para ver o
saldo ao terceiro pedido desliga o sistema — e aí nada está protegido.

A regra final: substantivo de dinheiro só torna a ferramenta sensível
**quando não há verbo de leitura**.

---

## 5. Conteúdo vindo de fora passa pelo escudo

A descrição de uma ferramenta MCP é texto de um processo externo. Um
servidor comprometido pode esconder instruções no campo `description` — que
acaba no prompt do agente.

Por isso cada descrição passa pelo `prompt_shield`, exactamente como uma
página web. O que for marcado fica registado em `avisos`, e não no silêncio.

---

## 6. Contagem honesta

| Origem | Ferramentas | Prontas sem chave |
|---|---|---|
| Registo interno (49 nomes) | 49 | 37 |
| Servidor MCP `filesystem` | 14 | 14 |
| Servidor MCP `postgres` | variável | requer base de dados |
| Servidor MCP `github` | ~30 | ❌ exige token |
| Servidor MCP `notion` | ~20 | ❌ exige token |

**Não são «centenas de ferramentas reais» hoje.** São ~51 que arrancam
neste ambiente, e mais ~50 que arrancariam com uma chave configurada. As
centenas existem no ecossistema; não existem nesta máquina.

Dizer «centenas» quando o que corre são 51 é o mesmo erro que este projecto
tem vindo a corrigir: uma lista que promete mais do que entrega.

---

## 7. O que faria subir de 51 para centenas

Por ordem de retorno sobre esforço:

1. **Uma chave `GITHUB_PERSONAL_ACCESS_TOKEN`** → ~30 ferramentas de repos,
   issues, PRs e Actions. É a melhor relação custo/benefício da lista.
2. **Notion** (`NOTION_API_TOKEN`) → ~20 ferramentas documentais.
3. **Composio** — 250+ apps com uma integração só, mas exige conta e plano;
   não é zero-custo.
4. **Servidores MCP próprios** para o que é específico da holding
   (cofre, squads, validação de demanda). Estes não têm custo nem chave, e
   são os que ninguém vai escrever por nós.

O caminho 4 é o mais subestimado: um servidor MCP que expõe o próprio cofre
dá ao agente as ferramentas que ele realmente precisa, em vez de adaptar-se
a 200 que não servem.
