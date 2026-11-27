package com.automaton.holding.data

import android.util.Log
import com.automaton.holding.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

private const val TAG = "ApiService"

// ==========================================================================
// Modelos serializáveis — espelham exactamente os contratos do FastAPI.
//
// Regra: todos os campos opcionais/aninhados têm valor por omissão e o Json
// usa `ignoreUnknownKeys`, para que o backend possa evoluir sem quebrar APKs
// já instalados. Campos que o APK não consome ficam como [JsonElement].
// ==========================================================================

// ------------------------------------------------------------- segurança --
@Serializable
data class KillSwitchState(
    val engaged: Boolean = false,
    val reason: String = "",
    @SerialName("engaged_by") val engagedBy: String = "",
    @SerialName("engaged_at") val engagedAt: String? = null,
    @SerialName("released_at") val releasedAt: String? = null,
    val version: Int = 0,
)

@Serializable
data class KillSwitchResponse(
    val ok: Boolean = false,
    val state: KillSwitchState = KillSwitchState(),
)

// ------------------------------------------------------------ tesouraria --
@Serializable
data class Balances(
    @SerialName("tax_reserve_usd") val tax: Double = 0.0,
    @SerialName("owner_pocket_usd") val owner: Double = 0.0,
    @SerialName("savings_vault_usd") val savings: Double = 0.0,
    @SerialName("reinvestment_pool_usd") val reinvestment: Double = 0.0,
)

@Serializable
data class PhaseSplit(
    @SerialName("owner_pocket") val owner: Double = 0.10,
    @SerialName("savings_vault") val savings: Double = 0.20,
    @SerialName("reinvestment_pool") val reinvestment: Double = 0.70,
)

@Serializable
data class PhaseMeta(
    val name: String = "Lançamento",
    @SerialName("min_usd") val minUsd: Double = 0.0,
    @SerialName("max_usd") val maxUsd: Double = 1500.0,
    val split: PhaseSplit = PhaseSplit(),
    val description: String = "",
)

@Serializable
data class FxInfo(
    @SerialName("usd_mzn") val usdMzn: Double = 63.5,
    val source: String = "",
    @SerialName("updated_at") val updatedAt: String = "",
)

@Serializable
data class PhaseThresholds(
    @SerialName("phase_2") val phase2: Double = 1500.0,
    @SerialName("phase_3") val phase3: Double = 15000.0,
)

@Serializable
data class TreasurySnapshot(
    val phase: Int = 1,
    @SerialName("computed_phase") val computedPhase: Int = 1,
    @SerialName("phase_meta") val phaseMeta: PhaseMeta = PhaseMeta(),
    val split: PhaseSplit = PhaseSplit(),
    @SerialName("balances_usd") val balancesUsd: Balances = Balances(),
    @SerialName("balances_mzn") val balancesMzn: Balances = Balances(),
    @SerialName("total_assets_usd") val totalAssetsUsd: Double = 0.0,
    @SerialName("gross_revenue_usd") val grossRevenueUsd: Double = 0.0,
    @SerialName("net_revenue_usd") val netRevenueUsd: Double = 0.0,
    @SerialName("spent_from_reinvestment_usd") val spentUsd: Double = 0.0,
    @SerialName("paid_out_usd") val paidOutUsd: Double = 0.0,
    @SerialName("monthly_revenue_usd") val monthlyRevenueUsd: Double = 0.0,
    @SerialName("phase_thresholds_usd") val thresholds: PhaseThresholds = PhaseThresholds(),
    @SerialName("distributable_owner_usd") val distributableOwnerUsd: Double = 0.0,
    @SerialName("tax_reserve_rate") val taxReserveRate: Double = 0.05,
)

@Serializable
data class PayoutChannel(
    val label: String = "",
    val available: Boolean = false,
    val mode: String = "dry_run",
    @SerialName("missing_config") val missingConfig: List<String> = emptyList(),
)

@Serializable
data class ChannelsStatus(
    val channels: Map<String, PayoutChannel> = emptyMap(),
    val fx: FxInfo = FxInfo(),
    @SerialName("owner_pocket_usd") val ownerPocketUsd: Double = 0.0,
    @SerialName("generated_at") val generatedAt: String = "",
)

@Serializable
data class TreasuryResponse(
    val snapshot: TreasurySnapshot = TreasurySnapshot(),
    val channels: ChannelsStatus = ChannelsStatus(),
)

@Serializable
data class QuoteResponse(
    val usd: Double = 0.0,
    val rate: Double = 0.0,
    val mzn: Double = 0.0,
    @SerialName("reference_note") val referenceNote: String = "",
)

@Serializable
data class ReservePayoutBody(
    @SerialName("amount_usd") val amountUsd: Double,
    val channel: String,
    val destination: String = "",
    val reason: String = "",
)

@Serializable
data class ReserveResponse(
    val ok: Boolean = false,
    @SerialName("approval_id") val approvalId: String = "",
    val quote: QuoteResponse = QuoteResponse(),
    @SerialName("next_step") val nextStep: String = "",
)

// ------------------------------------------------------------ aprovações --
@Serializable
data class ApprovalItem(
    val id: String,
    val title: String = "",
    val kind: String = "custom",
    @SerialName("amount_usd") val amountUsd: Double = 0.0,
    val priority: String = "normal",
    val status: String = "pending",
    val reason: String = "",
    @SerialName("requested_by") val requestedBy: String = "",
    val metadata: JsonObject? = null,
    @SerialName("created_at") val createdAt: String = "",
    @SerialName("expires_at") val expiresAt: String = "",
    @SerialName("biometric_verified") val biometricVerified: Boolean = false,
    @SerialName("batch_id") val batchId: String = "",
    @SerialName("execution_result") val executionResult: JsonObject? = null,
)

@Serializable
data class ApprovalDeck(
    @SerialName("pending_count") val pendingCount: Int = 0,
    @SerialName("critical_count") val criticalCount: Int = 0,
    @SerialName("total_pending_usd") val totalPendingUsd: Double = 0.0,
    val critical: List<ApprovalItem> = emptyList(),
    val batches: Map<String, List<ApprovalItem>> = emptyMap(),
    @SerialName("generated_at") val generatedAt: String = "",
)

@Serializable
data class ApprovalsResponse(
    val items: List<ApprovalItem> = emptyList(),
    val deck: ApprovalDeck = ApprovalDeck(),
)

@Serializable
data class DecideBody(
    val approve: Boolean,
    @SerialName("biometric_verified") val biometricVerified: Boolean,
    val note: String = "",
)

@Serializable
data class DecideResponse(
    val ok: Boolean = false,
    val item: ApprovalItem? = null,
    val execution: JsonElement? = null,
)

// ------------------------------------------------------------- esquadrões --
@Serializable
data class SquadRecord(
    val id: String,
    val name: String = "",
    val type: String = "",
    val niche: String = "",
    @SerialName("owner_agent") val ownerAgent: String = "",
    val status: String = "active",
    val views: Int = 0,
    val clicks: Int = 0,
    val conversions: Int = 0,
    @SerialName("revenue_usd") val revenueUsd: Double = 0.0,
    @SerialName("cost_usd") val costUsd: Double = 0.0,
    @SerialName("cac_usd") val cacUsd: Double? = null,
    @SerialName("ltv_usd") val ltvUsd: Double? = null,
    @SerialName("ttfd_days") val ttfdDays: Double? = null,
    @SerialName("conversion_rate") val conversionRate: Double = 0.0,
    val ctr: Double = 0.0,
    @SerialName("days_alive") val daysAlive: Double = 0.0,
    @SerialName("profit_usd") val profitUsd: Double = 0.0,
)

@Serializable
data class PortfolioSummary(
    val total: Int = 0,
    val active: Int = 0,
    val killed: Int = 0,
    val paused: Int = 0,
    @SerialName("revenue_usd") val revenueUsd: Double = 0.0,
    @SerialName("cost_usd") val costUsd: Double = 0.0,
    @SerialName("with_first_dollar") val withFirstDollar: Int = 0,
)

@Serializable
data class PortfolioResponse(
    val squads: List<SquadRecord> = emptyList(),
    val summary: PortfolioSummary = PortfolioSummary(),
    @SerialName("kill_policy") val killPolicy: JsonObject? = null,
)

@Serializable
data class KillSquadBody(
    val cause: String,
    @SerialName("biometric_verified") val biometricVerified: Boolean,
)

@Serializable
data class KillSquadResponse(
    val ok: Boolean = false,
    @SerialName("approval_id") val approvalId: String = "",
    @SerialName("squad_id") val squadId: String = "",
    val status: String = "",
    @SerialName("post_mortem") val postMortem: JsonObject? = null,
)

// ------------------------------------------------------------- governança --
@Serializable
data class HrAgent(
    val id: String,
    val calls: Int = 0,
    @SerialName("success_rate") val successRate: Double? = null,
    @SerialName("avg_judge_score") val avgJudgeScore: Double? = null,
    @SerialName("cost_usd") val costUsd: Double = 0.0,
    val tokens: Int = 0,
    @SerialName("prompt_version") val promptVersion: Int = 1,
    val rewrites: Int = 0,
    @SerialName("last_review") val lastReview: String = "",
    val status: String = "active",
)

@Serializable
data class HrSummary(
    @SerialName("total_agents") val totalAgents: Int = 0,
    @SerialName("needing_attention") val needingAttention: Int = 0,
    @SerialName("total_tokens") val totalTokens: Int = 0,
    @SerialName("total_cost_usd") val totalCostUsd: Double = 0.0,
)

@Serializable
data class HrReport(
    val agents: List<HrAgent> = emptyList(),
    val summary: HrSummary = HrSummary(),
)

@Serializable
data class AccountingPayload(
    val treasury: JsonElement? = null,
    val portfolio: JsonElement? = null,
    @SerialName("ledger_audit") val ledgerAudit: JsonObject? = null,
    @SerialName("monthly_revenue_usd") val monthlyRevenueUsd: Double = 0.0,
    val phase: Int = 1,
    @SerialName("tax_rate") val taxRate: Double = 0.05,
    @SerialName("tax_reserve_usd") val taxReserveUsd: Double = 0.0,
)

@Serializable
data class LegalReport(
    @SerialName("tools_available") val toolsAvailable: Map<String, Boolean> = emptyMap(),
    @SerialName("allowed_licenses") val allowedLicenses: List<String> = emptyList(),
    @SerialName("forbidden_licenses") val forbiddenLicenses: List<String> = emptyList(),
    @SerialName("forbidden_packages") val forbiddenPackages: List<String> = emptyList(),
    @SerialName("recent_audits") val recentAudits: JsonElement? = null,
)

@Serializable
data class JudgeReport(
    val threshold: Double = 85.0,
    @SerialName("total_evaluations") val totalEvaluations: Int = 0,
    val passed: Int = 0,
    val failed: Int = 0,
    @SerialName("pass_rate") val passRate: Double = 0.0,
    @SerialName("average_score") val averageScore: Double? = null,
    @SerialName("by_content_type") val byContentType: JsonElement? = null,
)

@Serializable
data class GovernanceResponse(
    val hr: HrReport = HrReport(),
    val accounting: AccountingPayload = AccountingPayload(),
    val legal: LegalReport = LegalReport(),
    val judge: JudgeReport = JudgeReport(),
    val cemetery: JsonElement? = null,
)

// --------------------------------------------------------------- agentes --
@Serializable
data class BridgeInfo(
    val mode: String = "nativo",
    @SerialName("crewai_installed") val crewaiInstalled: Boolean = false,
    @SerialName("native_engine") val nativeEngine: String = "",
    val providers: List<String> = emptyList(),
)

@Serializable
data class AiCatalog(
    val bridge: BridgeInfo = BridgeInfo(),
    val validation: JsonObject? = null,
    val llm: JsonObject? = null,
)

// ------------------------------------------------------------- validação --
@Serializable
data class ValidationSummary(
    val total: Int = 0,
    val running: Int = 0,
    val passed: Int = 0,
    val failed: Int = 0,
    @SerialName("total_clicks") val totalClicks: Int = 0,
    @SerialName("total_signups") val totalSignups: Int = 0,
    @SerialName("overall_conversion") val overallConversion: Double = 0.0,
    @SerialName("production_unlocked") val productionUnlocked: Int = 0,
)

@Serializable
data class ValidationPayload(
    val summary: ValidationSummary = ValidationSummary(),
    val thresholds: JsonObject? = null,
    val active: List<JsonElement> = emptyList(),
)

@Serializable
data class ValidationReport(
    val report: ValidationPayload = ValidationPayload(),
    val tests: List<JsonElement> = emptyList(),
)

// ------------------------------------------------------------------ ciclo --
@Serializable
data class CycleStep(
    val step: String = "",
    val status: String = "",
    val detail: JsonElement? = null,
)

@Serializable
data class CycleRecord(
    @SerialName("cycle_id") val cycleId: String = "",
    val status: String = "",
    val summary: String = "",
    @SerialName("started_at") val startedAt: String = "",
    @SerialName("finished_at") val finishedAt: String = "",
    @SerialName("duration_seconds") val durationSeconds: Double = 0.0,
    val steps: List<CycleStep> = emptyList(),
    val errors: List<JsonElement> = emptyList(),
)

@Serializable
data class CycleRunBody(
    @SerialName("force_scan") val forceScan: Boolean = false,
    val produce: Boolean = true,
)

// ----------------------------------------------------------------- sistema --
@Serializable
data class TreasuryCounters(
    val phase: Int = 1,
    @SerialName("phase_name") val phaseName: String = "",
    @SerialName("total_assets_usd") val totalAssetsUsd: Double = 0.0,
    @SerialName("monthly_revenue_usd") val monthlyRevenueUsd: Double = 0.0,
    @SerialName("balances_usd") val balancesUsd: Balances = Balances(),
)

@Serializable
data class ApprovalsCounters(
    val pending: Int = 0,
    val critical: Int = 0,
    @SerialName("pending_usd") val pendingUsd: Double = 0.0,
)

@Serializable
data class SystemStatusResponse(
    @SerialName("kill_switch") val killSwitch: KillSwitchState = KillSwitchState(),
    val treasury: TreasuryCounters = TreasuryCounters(),
    val portfolio: PortfolioSummary = PortfolioSummary(),
    val approvals: ApprovalsCounters = ApprovalsCounters(),
    val validation: ValidationSummary = ValidationSummary(),
    @SerialName("last_cycle") val lastCycle: CycleRecord? = null,
)

@Serializable
data class HealthResponse(
    val status: String = "unknown",
    val version: String = "",
    val env: String = "",
    @SerialName("kill_switch") val killSwitch: KillSwitchState = KillSwitchState(),
)

/** Evento do bus em tempo real (`/ws/live`). */
@Serializable
data class LiveEvent(
    val sequence: Long = 0,
    val type: String = "",
    val agent: String = "",
    val level: String = "info",
    val message: String = "",
    @SerialName("ts") val timestamp: String = "",
    val payload: JsonElement? = null,
)

@Serializable
data class KillSwitchBody(val reason: String)

// ==========================================================================
// Cliente REST + WebSocket
// ==========================================================================
class ApiService(
    private val baseUrl: String = BuildConfig.BASE_URL.trimEnd('/'),
    private val token: String = BuildConfig.API_TOKEN,
) {
    val json: Json = Json {
        ignoreUnknownKeys = true
        coerceInputValues = true
        explicitNulls = false
        isLenient = true
    }

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .pingInterval(20, TimeUnit.SECONDS)   // mantém o WS vivo atrás de proxies
        .retryOnConnectionFailure(true)
        .build()

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()

    // ------------------------------------------------------------------ REST
    private fun requestBuilder(path: String): Request.Builder {
        val builder = Request.Builder().url("$baseUrl$path")
        if (token.isNotBlank()) builder.header("X-Automaton-Token", token)
        builder.header("Accept", "application/json")
        return builder
    }

    private suspend fun <T> execute(
        request: Request,
        deserializer: (String) -> T,
    ): Result<T> = withContext(Dispatchers.IO) {
        try {
            val parsed = suspendCancellableCoroutine { continuation ->
                val call = client.newCall(request)
                continuation.invokeOnCancellation { call.cancel() }
                call.enqueue(object : Callback {
                    override fun onFailure(call: Call, e: IOException) {
                        if (continuation.isActive) {
                            continuation.resumeWithException(e)
                        }
                    }

                    override fun onResponse(call: Call, response: Response) {
                        response.use {
                            val body = it.body?.string().orEmpty()
                            if (!it.isSuccessful) {
                                Log.w(TAG, "HTTP ${it.code} em ${call.request().url}: $body")
                                if (continuation.isActive) {
                                    continuation.resumeWithException(
                                        ApiException(it.code, body.take(600))
                                    )
                                }
                                return
                            }
                            try {
                                val value = deserializer(body)
                                if (continuation.isActive) continuation.resume(value)
                            } catch (error: Exception) {
                                if (continuation.isActive) {
                                    continuation.resumeWithException(error)
                                }
                            }
                        }
                    }
                })
            }
            Result.success(parsed)
        } catch (error: Exception) {
            Result.failure(error)
        }
    }

    private fun bodyOf(payload: String) = payload.toRequestBody(jsonMedia)
    private val emptyBody get() = "{}".toRequestBody(jsonMedia)

    // ------------------------------------------------------------- sistema
    suspend fun health(): Result<HealthResponse> =
        execute(requestBuilder("/health").get().build()) { text ->
            json.decodeFromString(HealthResponse.serializer(), text)
        }

    suspend fun getSystemStatus(): Result<SystemStatusResponse> =
        execute(requestBuilder("/system/status").get().build()) { text ->
            json.decodeFromString(SystemStatusResponse.serializer(), text)
        }

    // ---------------------------------------------------------- tesouraria
    suspend fun getTreasury(): Result<TreasuryResponse> =
        execute(requestBuilder("/treasury").get().build()) { text ->
            json.decodeFromString(TreasuryResponse.serializer(), text)
        }

    suspend fun reservePayout(
        amountUsd: Double,
        channel: String,
        destination: String = "",
        reason: String = "",
    ): Result<ReserveResponse> {
        val payload = json.encodeToString(
            ReservePayoutBody.serializer(),
            ReservePayoutBody(amountUsd, channel, destination, reason)
        )
        return execute(
            requestBuilder("/treasury/payouts/reserve").post(bodyOf(payload)).build()
        ) { text -> json.decodeFromString(ReserveResponse.serializer(), text) }
    }

    // ----------------------------------------------------------- aprovações
    suspend fun getApprovals(): Result<ApprovalsResponse> =
        execute(requestBuilder("/approvals").get().build()) { text ->
            json.decodeFromString(ApprovalsResponse.serializer(), text)
        }

    suspend fun decideApproval(
        approvalId: String,
        approve: Boolean,
        biometricVerified: Boolean,
        note: String = "",
    ): Result<DecideResponse> {
        val payload = json.encodeToString(
            DecideBody.serializer(),
            DecideBody(approve, biometricVerified, note)
        )
        return execute(
            requestBuilder("/approvals/$approvalId/decide").post(bodyOf(payload)).build()
        ) { text -> json.decodeFromString(DecideResponse.serializer(), text) }
    }

    // ---------------------------------------------------------- esquadrões
    suspend fun getPortfolio(): Result<PortfolioResponse> =
        execute(requestBuilder("/squads").get().build()) { text ->
            json.decodeFromString(PortfolioResponse.serializer(), text)
        }

    suspend fun killSquad(
        squadId: String,
        cause: String,
        biometricVerified: Boolean,
    ): Result<KillSquadResponse> {
        val payload = json.encodeToString(
            KillSquadBody.serializer(),
            KillSquadBody(cause, biometricVerified)
        )
        return execute(
            requestBuilder("/squads/$squadId/kill").post(bodyOf(payload)).build()
        ) { text -> json.decodeFromString(KillSquadResponse.serializer(), text) }
    }

    // --------------------------------------------------------------- agentes
    suspend fun getAgents(): Result<AiCatalog> =
        execute(requestBuilder("/agents").get().build()) { text ->
            json.decodeFromString(AiCatalog.serializer(), text)
        }

    // ----------------------------------------------------------- governança
    suspend fun getGovernance(): Result<GovernanceResponse> =
        execute(requestBuilder("/governance").get().build()) { text ->
            json.decodeFromString(GovernanceResponse.serializer(), text)
        }

    suspend fun getValidation(): Result<ValidationReport> =
        execute(requestBuilder("/validation").get().build()) { text ->
            json.decodeFromString(ValidationReport.serializer(), text)
        }

    // --------------------------------------------------------- kill switch
    suspend fun engageKillSwitch(reason: String): Result<KillSwitchResponse> {
        val payload = json.encodeToString(KillSwitchBody.serializer(), KillSwitchBody(reason))
        return execute(
            requestBuilder("/kill-switch/engage").post(bodyOf(payload)).build()
        ) { text -> json.decodeFromString(KillSwitchResponse.serializer(), text) }
    }

    suspend fun releaseKillSwitch(): Result<KillSwitchResponse> =
        execute(requestBuilder("/kill-switch/release").post(emptyBody).build()) { text ->
            json.decodeFromString(KillSwitchResponse.serializer(), text)
        }

    // ---------------------------------------------------------------- ciclo
    suspend fun runCycle(
        forceScan: Boolean = false,
        produce: Boolean = true,
    ): Result<CycleRecord> {
        val payload = json.encodeToString(
            CycleRunBody.serializer(),
            CycleRunBody(forceScan, produce)
        )
        return execute(
            requestBuilder("/cycle/run").post(bodyOf(payload)).build()
        ) { text -> json.decodeFromString(CycleRecord.serializer(), text) }
    }

    // ------------------------------------------------------------ WebSocket
    /**
     * Abre o stream da LiveOpsScreen. Emite cada mensagem JSON como [LiveEvent]
     * (incluindo `snapshot` e `heartbeat`). O ViewModel decide o que mostrar.
     */
    fun liveEvents(): Flow<LiveEvent> = callbackFlow {
        val wsUrl = baseUrl
            .replaceFirst("https://", "wss://")
            .replaceFirst("http://", "ws://") + "/ws/live"
        val builder = Request.Builder().url(wsUrl)
        if (token.isNotBlank()) builder.header("X-Automaton-Token", token)

        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "WebSocket ligado a $wsUrl")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    trySend(json.decodeFromString(LiveEvent.serializer(), text))
                } catch (error: Exception) {
                    Log.w(TAG, "Evento ilegível: ${error.message}")
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "WebSocket falhou: ${t.message}")
                // Fecha o fluxo: o ViewModel reabre com backoff exponencial.
                close(t)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket fechado ($code): $reason")
                close()
            }
        }

        val socket = client.newWebSocket(builder.build(), listener)
        awaitClose { socket.close(1000, "cliente saiu") }
    }

    /** Extracção do backlog contido no evento `snapshot` do `/ws/live`. */
    fun parseSnapshotEvents(event: LiveEvent): List<LiveEvent> {
        val payload = event.payload as? JsonObject ?: return emptyList()
        val array = payload["events"] ?: return emptyList()
        return try {
            json.decodeFromString(
                kotlinx.serialization.builtins.ListSerializer(LiveEvent.serializer()),
                array.toString()
            )
        } catch (error: Exception) {
            Log.w(TAG, "Snapshot ilegível: ${error.message}")
            emptyList()
        }
    }
}

/** Erro HTTP com o corpo devolvido pelo backend (útil para mensagens ao Presidente). */
class ApiException(val code: Int, val payload: String) :
    IOException("HTTP $code: $payload")
