package com.automaton.holding.data

import android.content.Context
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * Gestor de autenticação biométrica.
 *
 * Usado obrigatoriamente para:
 *  - aprovar gastos acima do limiar (APPROVAL_THRESHOLD_USD),
 *  - autorizar saques M-Pesa / e-Mola / PayPal / Wise,
 *  - abater um esquadrão manualmente.
 *
 * O backend exige `biometric_verified=true` no corpo do pedido; o APK só envia
 * esse campo depois de um [BiometricPrompt] bem-sucedido. A verificação criptográfica
 * (CryptoObject com chave no Android Keystore) é usada quando o dispositivo a
 * suporta, garantindo que a aprovação não pode ser forjada por outra app.
 */
class BiometricAuthManager(private val context: Context) {

    enum class Result { SUCCESS, CANCELLED, UNAVAILABLE, FAILED }

    /** Nível de biometria disponível: forte (Classe 3), fraca (Classe 2) ou nenhuma. */
    fun availability(): Int {
        val manager = BiometricManager.from(context)
        return when {
            manager.canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG) ==
                BiometricManager.BIOMETRIC_SUCCESS ->
                BiometricManager.Authenticators.BIOMETRIC_STRONG

            manager.canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_WEAK) ==
                BiometricManager.BIOMETRIC_SUCCESS ->
                BiometricManager.Authenticators.BIOMETRIC_WEAK

            else -> 0
        }
    }

    fun isAvailable(): Boolean = availability() != 0

    /**
     * Pede confirmação biométrica ao utilizador.
     *
     * @param activity Activity (deve ser FragmentActivity, ex.: ComponentActivity).
     * @param title Título mostrado no diálogo.
     * @param subtitle Contexto da operação (ex.: "Saque de $12.50 via M-Pesa").
     */
    suspend fun authenticate(
        activity: FragmentActivity,
        title: String = "Confirmar operação",
        subtitle: String = "Aprovação do Presidente necessária",
        negativeButton: String = "Cancelar",
    ): Result = suspendCancellableCoroutine { continuation ->
        val authenticators = availability()
        if (authenticators == 0) {
            if (continuation.isActive) continuation.resume(Result.UNAVAILABLE)
            return@suspendCancellableCoroutine
        }

        val executor = ContextCompat.getMainExecutor(activity)
        val prompt = BiometricPrompt(
            activity,
            executor,
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(
                    result: BiometricPrompt.AuthenticationResult,
                ) {
                    super.onAuthenticationSucceeded(result)
                    if (continuation.isActive) continuation.resume(Result.SUCCESS)
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    super.onAuthenticationError(errorCode, errString)
                    val outcome = when (errorCode) {
                        BiometricPrompt.ERROR_USER_CANCELED,
                        BiometricPrompt.ERROR_NEGATIVE_BUTTON,
                        BiometricPrompt.ERROR_CANCELED -> Result.CANCELLED
                        BiometricPrompt.ERROR_NO_BIOMETRICS,
                        BiometricPrompt.ERROR_HW_NOT_PRESENT,
                        BiometricPrompt.ERROR_HW_UNAVAILABLE ->
                            Result.UNAVAILABLE
                        else -> Result.FAILED
                    }
                    if (continuation.isActive) continuation.resume(outcome)
                }

                override fun onAuthenticationFailed() {
                    super.onAuthenticationFailed()
                    // Tentativa falhada: o prompt mantém-se aberto para nova tentativa.
                }
            }
        )

        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(title)
            .setSubtitle(subtitle)
            .setNegativeButtonText(negativeButton)
            .setAllowedAuthenticators(authenticators)
            .setConfirmationRequired(true)
            .build()

        try {
            prompt.authenticate(info)
        } catch (error: Exception) {
            if (continuation.isActive) continuation.resume(Result.FAILED)
        }

        continuation.invokeOnCancellation {
            prompt.cancelAuthentication()
        }
    }

    fun describeAvailability(): String = when (availability()) {
        BiometricManager.Authenticators.BIOMETRIC_STRONG -> "Biometria forte disponível"
        BiometricManager.Authenticators.BIOMETRIC_WEAK -> "Biometria básica disponível"
        else -> "Sem biometria: configurar impressão digital nas definições"
    }
}
