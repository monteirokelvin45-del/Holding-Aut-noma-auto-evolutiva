package com.automaton.holding.data

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Contrato APK ↔ backend para o PayPal, contra **respostas reais** do FastAPI.
 *
 * As fixtures foram geradas com o backend a correr, não escritas à mão. O
 * teste que justifica o ficheiro é `paypal status mapeia snake_case`: sem os
 * `@SerialName`, `sacar_para_mocambique` leria como ausente e o cartão no APK
 * apareceria sem o aviso — em silêncio, que é o pior desfecho possível para
 * um aviso.
 *
 * Corre na JVM (`./gradlew :app:testDebugUnitTest`), sem emulador.
 */
class PayPalContractTest {

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        coerceInputValues = true
        explicitNulls = false
    }

    private fun fixture(name: String): String =
        javaClass.getResourceAsStream("/fixtures/$name.json")
            ?.bufferedReader()?.readText()
            ?: error("fixture ausente: /fixtures/$name.json")

    @Test
    fun `paypal status decodifica`() {
        val status = json.decodeFromString(PayPalStatus.serializer(), fixture("paypal"))
        assertFalse("sem chaves não pode dizer configurado", status.configurado)
        assertTrue("modo deve estar preenchido", status.modo.isNotBlank())
        assertTrue("deve listar o que falta", status.faltam.isNotEmpty())
    }

    @Test
    fun `paypal status mapeia snake_case`() {
        // O erro silencioso: se o @SerialName faltasse, este campo viria "".
        val status = json.decodeFromString(PayPalStatus.serializer(), fixture("paypal"))
        assertTrue(
            "sacar_para_mocambique não mapeou: o aviso desaparecia da app",
            status.sacarParaMocambique.isNotBlank()
        )
        assertTrue(
            "o aviso tem de dizer que não está resolvido",
            status.sacarParaMocambique.contains("NÃO RESOLVIDO")
        )
        assertTrue("moedas vazias", status.moedas.isNotEmpty())
    }

    @Test
    fun `ferramentas do agente chegam com 42 e com nome`() {
        val resposta = json.decodeFromString(
            PayPalToolsResponse.serializer(), fixture("paypal_ferramentas")
        )
        assertEquals(42, resposta.total)
        assertEquals(resposta.total, resposta.ferramentas.size)
        assertTrue(
            "as ferramentas precisam de nome legível",
            resposta.ferramentas.all { it.nome.isNotBlank() }
        )
        assertTrue(resposta.ferramentas.any { it.metodo == "create_order" })
    }

    @Test
    fun `cobranca em dry run diz que nada aconteceu`() {
        val cobranca = json.decodeFromString(
            CobrancaResponse.serializer(), fixture("paypal_cobranca")
        )
        assertTrue("devia ser dry-run", cobranca.dryRun)
        assertFalse("dry-run nunca é pago", cobranca.pago)
        assertFalse(cobranca.ok)
        assertTrue("sem credenciais não se inventa order_id", cobranca.orderId.isBlank())
        assertTrue("moeda preenchida", cobranca.currency.isNotBlank())
    }

    @Test
    fun `cobranca mapeia amount_usd e approval_url`() {
        // Simula o que a PayPal devolveria com chaves: o mapeamento é o mesmo.
        val texto = """
            {"ok":true,"order_id":"ABC123","status":"CREATED",
             "approval_url":"https://www.sandbox.paypal.com/checkoutnow?token=ABC123",
             "amount_usd":19.9,"currency":"USD","dry_run":false,"pago":false,
             "message":"pedido criado"}
        """.trimIndent()
        val cobranca = json.decodeFromString(CobrancaResponse.serializer(), texto)
        assertEquals("ABC123", cobranca.orderId)
        assertEquals(19.9, cobranca.amountUsd, 0.001)
        assertTrue(cobranca.approvalUrl.startsWith("https://"))
        assertFalse("criado não é pago", cobranca.pago)
    }

    @Test
    fun `resposta sem campos nao rebenta`() {
        // O backend pode acrescentar campos; o APK antigo não pode crashar.
        val cobranca = json.decodeFromString(CobrancaResponse.serializer(), "{}")
        assertFalse(cobranca.ok)
        assertTrue(cobranca.orderId.isBlank())
    }
}
