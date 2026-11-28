package com.automaton.holding.data

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Contrato APK ↔ backend, verificado contra **respostas reais** do FastAPI.
 *
 * As fixtures em `src/test/resources/fixtures/` não foram escritas à mão: são
 * a saída literal da API deste repositório. Se o backend mudar um nome de
 * campo, estes testes falham no CI em vez de o APK mostrar zeros em produção.
 *
 * Correm na JVM (`./gradlew :app:testDebugUnitTest`), sem emulador.
 */
class ApiContractTest {

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        coerceInputValues = true
    }

    private fun fixture(name: String): String =
        javaClass.getResourceAsStream("/fixtures/$name.json")
            ?.bufferedReader()?.readText()
            ?: error("fixture ausente: /fixtures/$name.json")

    // ------------------------------------------------------------- sistema --
    @Test
    fun `health decodifica e tem kill switch desligado`() {
        val health = json.decodeFromString(HealthResponse.serializer(), fixture("health"))
        assertEquals("ok", health.status)
        assertTrue(health.version.isNotBlank())
        assertFalse(health.killSwitch.engaged)
    }

    @Test
    fun `system status decodifica os contadores todos`() {
        val status = json.decodeFromString(
            SystemStatusResponse.serializer(), fixture("system_status")
        )
        assertTrue("fase fora de 1..3: ${status.treasury.phase}", status.treasury.phase in 1..3)
        assertEquals("USD", "USD") // âncora de leitura; os campos seguintes são o que importa
        assertTrue(status.treasury.balancesUsd.tax >= 0.0)
        assertTrue(status.approvals.pending >= 0)
        assertNotNull(status)
    }

    // ----------------------------------------------------------- tesouraria --
    @Test
    fun `treasury mantem o split da fase e o imposto de 5 por cento`() {
        val treasury = json.decodeFromString(TreasuryResponse.serializer(), fixture("treasury"))
        val snap = treasury.snapshot

        // Regra inegociável da holding: 5% de imposto retido na fonte.
        assertEquals(0.05, snap.taxReserveRate, 0.0001)

        // O split que chega ao APK tem de somar 1.0 (arredondamento incluído).
        val soma = snap.split.owner + snap.split.savings + snap.split.reinvestment
        assertEquals(1.0, soma, 0.001)

        // Limiares de fase da especificação: 1 500 e 15 000 USD/mês.
        assertEquals(1500.0, snap.thresholds.phase2, 0.01)
        assertEquals(15000.0, snap.thresholds.phase3, 0.01)

        // Fase 1 = 10/20/70; fase 2 = 10/40/50; fase 3 = 50/30/20.
        when (snap.phase) {
            1 -> assertEquals(0.70, snap.split.reinvestment, 0.001)
            2 -> assertEquals(0.50, snap.split.reinvestment, 0.001)
            3 -> assertEquals(0.20, snap.split.reinvestment, 0.001)
            else -> error("fase inválida: ${snap.phase}")
        }
        assertTrue(treasury.channels.fx.usdMzn > 0.0)
    }

    @Test
    fun `reserve payout codifica os nomes que o backend espera`() {
        // O FastAPI lê `amount_usd` e `channel`. Se o APK enviasse `amountUsd`
        // o backend devolveria 422 e a aprovação nunca era criada.
        val corpo = json.encodeToString(
            ReservePayoutBody.serializer(),
            ReservePayoutBody(12.5, "mpesa", "841234567", "teste")
        )
        assertTrue(corpo, corpo.contains("\"amount_usd\""))
        assertTrue(corpo, corpo.contains("\"channel\""))
        assertFalse("não deve usar camelCase", corpo.contains("amountUsd"))
    }

    @Test
    fun `decide approval codifica biometric_verified`() {
        val corpo = json.encodeToString(
            DecideBody.serializer(), DecideBody(approve = true, biometricVerified = true)
        )
        assertTrue(corpo, corpo.contains("\"approve\":true"))
        assertTrue(corpo, corpo.contains("\"biometric_verified\":true"))
    }

    // ----------------------------------------------------------- aprovações --
    @Test
    fun `approvals traz itens e deck coerentes`() {
        val resposta = json.decodeFromString(
            ApprovalsResponse.serializer(), fixture("approvals")
        )
        assertTrue("devia haver pelo menos a aprovação de teste", resposta.items.isNotEmpty())
        val item = resposta.items.first()
        assertTrue(item.id.isNotBlank())
        // O deck é o que alimenta o ecrã de aprovações; não pode vir vazio
        // quando há itens pendentes.
        if (resposta.items.any { it.status == "pending" }) {
            assertTrue(
                "deck.pending_count=${resposta.deck.pendingCount} com itens pendentes",
                resposta.deck.pendingCount >= 1
            )
        }
    }

    // ---------------------------------------------------------- esquadrões --
    @Test
    fun `squads decodifica portefolio e politica de abate`() {
        val portfolio = json.decodeFromString(
            PortfolioResponse.serializer(), fixture("squads")
        )
        assertTrue(portfolio.summary.total >= 0)
        assertEquals(portfolio.summary.total, portfolio.squads.size)
    }

    @Test
    fun `agents decodifica modo do motor de IA`() {
        val catalogo = json.decodeFromString(AiCatalog.serializer(), fixture("agents"))
        assertTrue(catalogo.bridge.mode.isNotBlank())
        assertNotNull(catalogo.validation)
        // O catálogo tem de chegar INTEIRO ao ecrã da Diretoria.
        //
        // Antes este teste só verificava o `bridge`: os 11 agentes que a API
        // enviava eram descartados (AiCatalog não tinha o campo) e nada disto
        // falhava. Passou a 17 agentes em 2026-11-28 — 6 novos, todos no
        // motor local.
        assertTrue(
            "catálogo sem agentes: ${catalogo.agents.size}",
            catalogo.agents.isNotEmpty(),
        )
        assertTrue(catalogo.agents.all { it.id.isNotBlank() })
        assertTrue(catalogo.agents.any { it.llmTier.isNotBlank() })
        assertTrue("catálogo sem tarefas", catalogo.tasks.isNotEmpty())
    }

    // --------------------------------------------------------- governança ---
    @Test
    fun `governance decodifica juiz com limiar de 85`() {
        val gov = json.decodeFromString(
            GovernanceResponse.serializer(), fixture("governance")
        )
        assertEquals("limiar do juiz é regra inegociável", 85.0, gov.judge.threshold, 0.001)
        assertTrue(gov.legal.allowedLicenses.isNotEmpty())
        // Nenhuma licença proibida pode aparecer na lista de permitidas.
        gov.legal.forbiddenLicenses.forEach { proibida ->
            assertFalse(
                "licença $proibida aparece como permitida",
                gov.legal.allowedLicenses.any { it.equals(proibida, ignoreCase = true) }
            )
        }
    }

    @Test
    fun `validation decodifica o resumo`() {
        val rel = json.decodeFromString(ValidationReport.serializer(), fixture("validation"))
        assertTrue(rel.report.summary.total >= 0)
        assertEquals(rel.report.summary.total, rel.tests.size)
    }

    // ------------------------------------------------------------ websocket --
    @Test
    fun `snapshot do websocket entrega o backlog`() {
        // O envelope `snapshot` não traz `sequence` nem `level` (é um aviso de
        // sistema, não um evento do bus). O APK tem de o aceitar e ir buscar o
        // histórico a payload.events — é o que este teste fixa.
        val envelope = json.decodeFromString(
            LiveEvent.serializer(), fixture("ws_live_snapshot")
        )
        assertEquals("snapshot", envelope.type)
        assertEquals("envelope sem sequence usa o default", 0L, envelope.sequence)

        val backlog = ApiService().parseSnapshotEvents(envelope)
        assertTrue("snapshot sem eventos legíveis", backlog.isNotEmpty())

        // Cada evento interno tem de trazer os campos que a LiveOps mostra.
        backlog.forEach { ev ->
            assertTrue("evento sem sequence: $ev", ev.sequence > 0L)
            assertTrue("evento sem type: $ev", ev.type.isNotBlank())
            assertTrue("evento sem agent: $ev", ev.agent.isNotBlank())
            assertTrue("evento sem level: $ev", ev.level.isNotBlank())
            assertTrue("evento sem ts: $ev", ev.timestamp.isNotBlank())
        }
        // O backlog vem ordenado: a UI depende disso para não baralhar a linha
        // temporal ao reconectar.
        assertEquals(backlog.map { it.sequence }.sorted(), backlog.map { it.sequence })
    }

    @Test
    fun `snapshot e heartbeat trazem o estado do kill switch`() {
        // O backend envia payload.kill_switch no snapshot E no heartbeat (aos
        // 25 s). Se o APK ignorasse esse campo, o ecrã só reflectiria o
        // sistema parado no polling seguinte — 20 s de atraso num indicador
        // de emergência.
        val snapshot = json.decodeFromString(
            LiveEvent.serializer(), fixture("ws_live_snapshot")
        )
        val bruto = (snapshot.payload as? kotlinx.serialization.json.JsonObject)
            ?.get("kill_switch")
        assertNotNull("snapshot sem payload.kill_switch", bruto)
        val estado = json.decodeFromString(KillSwitchState.serializer(), bruto.toString())
        assertFalse(estado.engaged)
        assertTrue(estado.version >= 0)

        // O heartbeat tem a mesma forma (sem sequence/level).
        val heartbeat = """
            {"type":"heartbeat","agent":"api","message":"ping",
             "payload":{"kill_switch":{"engaged":true,"reason":"teste",
             "engaged_by":"pytest","engaged_at":null,"released_at":null,
             "version":7}},"ts":"2026-11-27T00:00:00+00:00"}
        """.trimIndent()
        val ev = json.decodeFromString(LiveEvent.serializer(), heartbeat)
        assertEquals("heartbeat", ev.type)
        val kb = (ev.payload as? kotlinx.serialization.json.JsonObject)!!.get("kill_switch")
        val st = json.decodeFromString(KillSwitchState.serializer(), kb.toString())
        assertTrue("o APK tem de ver o sistema parado", st.engaged)
        assertEquals(7, st.version)
    }

    // -------------------------------------------------------------- robustez --
    @Test
    fun `campos desconhecidos nao partem a app`() {
        // Um backend mais novo pode acrescentar campos; um APK antigo não pode
        // rebentar. É a razão do ignoreUnknownKeys.
        val hostil = """
            {"status":"ok","version":"9.9.9","env":"test","campo_futuro":{"a":1},
             "kill_switch":{"engaged":false,"extra_novo":[1,2,3]}}
        """.trimIndent()
        val health = json.decodeFromString(HealthResponse.serializer(), hostil)
        assertEquals("9.9.9", health.version)
    }

    @Test
    fun `corpo de erro em texto simples nao impede a leitura`() {
        // O backend devolve HTML em alguns erros (proxy). O cliente tem de
        // reportar o código, não estourar ao descodificar.
        val excecao = ApiException(502, "<html>bad gateway</html>")
        assertEquals(502, excecao.code)
        assertTrue(excecao.message!!.contains("502"))
    }
}
