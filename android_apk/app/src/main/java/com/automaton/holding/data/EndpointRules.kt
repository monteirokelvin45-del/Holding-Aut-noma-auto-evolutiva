package com.automaton.holding.data

/**
 * Regras de endereço do backend, sem dependências de Android.
 *
 * Vive separado de [EndpointSettings] por uma razão prática: assim pode ser
 * testada em JVM (testes unitários rápidos, sem emulador nem Robolectric).
 * As duas barreiras — esta validação e o `network_security_config` — têm de
 * concordar sobre o que é "rede local".
 */
object EndpointRules {

    sealed class Validacao {
        data class Ok(val url: String) : Validacao()
        data class Erro(val motivo: String) : Validacao()
    }

    /**
     * Valida o que o utilizador escreveu.
     *
     * Aceita o que as pessoas escrevem na prática: `192.168.1.42:8000` sem
     * esquema, que se assume `http://`. Recusa o que não pode funcionar, com o
     * motivo em português — «erro de rede» não ensina nada a quem instala.
     */
    fun validar(entrada: String): Validacao {
        val bruto = entrada.trim()
        if (bruto.isEmpty()) return Validacao.Erro("indique o endereço do backend")

        val comEsquema = when {
            bruto.startsWith("http://") || bruto.startsWith("https://") -> bruto
            "://" in bruto -> return Validacao.Erro("só http:// ou https:// são suportados")
            else -> "http://$bruto"
        }

        val url = try {
            java.net.URI(comEsquema)
        } catch (e: Exception) {
            return Validacao.Erro("endereço inválido: ${e.message}")
        }

        val anfitriao = url.host
            ?: return Validacao.Erro("falta o anfitrião (ex.: 192.168.1.42:8000)")
        if (url.port > 65535) return Validacao.Erro("porta inválida")

        val seguro = comEsquema.startsWith("https://")
        if (!seguro && !ehRedePrivada(anfitriao)) {
            return Validacao.Erro(
                "HTTP em claro só para a rede local. Para $anfitriao use https://"
            )
        }
        return Validacao.Ok(comEsquema.trimEnd('/'))
    }

    /**
     * `10.0.2.2` (emulador), loopback, as três gamas privadas da RFC 1918,
     * link-local de hotspot e nomes `.local` (mDNS).
     */
    fun ehRedePrivada(anfitriao: String): Boolean {
        val h = anfitriao.trim().lowercase()
        if (h == "localhost" || h == "10.0.2.2") return true
        if (h.endsWith(".local")) return true
        val partes = h.split(".")
        if (partes.size != 4) return false
        val octetos = partes.map { it.toIntOrNull() ?: return false }
        if (octetos.any { it !in 0..255 }) return false
        val (a, b) = octetos
        return when {
            a == 127 -> true                      // loopback
            a == 10 -> true                       // 10/8
            a == 192 && b == 168 -> true          // 192.168/16
            a == 172 && b in 16..31 -> true       // 172.16/12
            a == 169 && b == 254 -> true          // link-local / hotspot
            else -> false
        }
    }
}
