package com.automaton.holding.data

import android.content.Context
import android.util.Log
import com.automaton.holding.BuildConfig

/**
 * Endereço do backend e token, guardados no próprio telefone.
 *
 * Porquê: o endereço vinha fixo do build (`BuildConfig.BASE_URL`). Num
 * telemóvel real apontava para `10.0.2.2`, que só existe dentro do emulador —
 * a app instalava-se e não falava com ninguém. Obrigar a recompilar o APK por
 * cada mudança de IP (DHCP muda-o, a rede de casa não é a do trabalho) tornava
 * o binário inútil fora do ambiente de quem o compilou.
 *
 * Regras:
 *  - `BuildConfig.BASE_URL` continua a ser a **sugestão inicial**: o build
 *    sabe qual é o endereço previsto, o utilizador pode corrigi-lo;
 *  - só se aceita HTTP em claro para endereços de rede privada ou loopback
 *    (ver EndpointRules.validar); para a internet é HTTPS obrigatório;
 *  - nenhum valor é inventado: se não houver nada guardado nem configurado no
 *    build, não há endereço e a app pede um.
 */
object EndpointSettings {

    private const val TAG = "EndpointSettings"
    private const val PREFS = "automaton_endpoint"
    private const val CHAVE_URL = "base_url"
    private const val CHAVE_TOKEN = "api_token"

    /** Endereço previsto no build, ou vazio se o build não definiu nenhum. */
    val sugestaoDoBuild: String
        get() = BuildConfig.BASE_URL.trim().trimEnd('/')

    fun lerUrl(context: Context): String {
        val guardado = prefs(context).getString(CHAVE_URL, null)?.trim().orEmpty()
        if (guardado.isNotBlank()) return guardado.trimEnd('/')
        return sugestaoDoBuild
    }

    fun lerToken(context: Context): String {
        val guardado = prefs(context).getString(CHAVE_TOKEN, null)?.trim().orEmpty()
        if (guardado.isNotBlank()) return guardado
        return BuildConfig.API_TOKEN.trim()
    }

    fun gravar(context: Context, url: String, token: String) {
        val limpo = url.trim().trimEnd('/')
        prefs(context).edit()
            .putString(CHAVE_URL, limpo)
            .putString(CHAVE_TOKEN, token.trim())
            .apply()
        Log.i(TAG, "endpoint guardado: $limpo")
    }

    /** Repõe a sugestão do build (útil depois de uma configuração errada). */
    fun esquecer(context: Context) {
        prefs(context).edit().remove(CHAVE_URL).remove(CHAVE_TOKEN).apply()
    }

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    // A validação em si vive em EndpointRules (sem Android) para poder ser
    // testada em JVM. Aqui ficam só os atalhos, para quem já usa este objecto.
    fun validar(entrada: String): EndpointRules.Validacao = EndpointRules.validar(entrada)

    fun ehRedePrivada(anfitriao: String): Boolean = EndpointRules.ehRedePrivada(anfitriao)
}
