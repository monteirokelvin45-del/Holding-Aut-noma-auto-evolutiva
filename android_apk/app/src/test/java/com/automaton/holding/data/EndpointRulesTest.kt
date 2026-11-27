package com.automaton.holding.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regras do ecrã de ligação. Sem estas, "configurável no telefone" só muda
 * onde o erro aparece: um endereço inválido guardado é pior do que um fixo.
 */
class EndpointRulesTest {

    private fun aceita(entrada: String): String {
        val r = EndpointRules.validar(entrada)
        assertTrue("devia aceitar \"$entrada\", disse: $r", r is EndpointRules.Validacao.Ok)
        return (r as EndpointRules.Validacao.Ok).url
    }

    private fun recusa(entrada: String): String {
        val r = EndpointRules.validar(entrada)
        assertTrue("devia recusar \"$entrada\"", r is EndpointRules.Validacao.Erro)
        return (r as EndpointRules.Validacao.Erro).motivo
    }

    @Test
    fun `ip de wifi sem esquema assume http`() {
        assertEquals("http://192.168.1.42:8000", aceita("192.168.1.42:8000"))
        assertEquals("http://192.168.1.42:8000", aceita("  http://192.168.1.42:8000/  "))
    }

    @Test
    fun `aceita as tres gamas privadas`() {
        aceita("10.1.2.3:8000")
        aceita("172.16.0.9:8000")
        aceita("172.31.255.254:8000")
        aceita("192.168.0.10")
    }

    @Test
    fun `aceita emulador e loopback`() {
        aceita("10.0.2.2:8000")
        aceita("127.0.0.1:8000")
        aceita("localhost:8000")
    }

    @Test
    fun `172_32 ja nao e privado`() {
        // A gama privada é 172.16-31; 172.32 é público. Fronteira clássica.
        assertFalse(EndpointRules.ehRedePrivada("172.32.0.1"))
        recusa("172.32.0.1:8000")
    }

    @Test
    fun `http em claro para a internet e recusado`() {
        val motivo = recusa("http://meu-servidor.com:8000")
        assertTrue(motivo.contains("https", ignoreCase = true))
        // e o mesmo endereço com https passa
        aceita("https://meu-servidor.com:8000")
    }

    @Test
    fun `recusa lixo e esquemas desconhecidos`() {
        recusa("")
        recusa("   ")
        recusa("ftp://192.168.1.1")
        recusa("192.168.1.999:8000")
        recusa("http://")
    }
}
