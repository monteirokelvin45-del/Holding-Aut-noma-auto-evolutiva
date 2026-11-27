package com.automaton.holding.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.automaton.holding.data.ApiException
import com.automaton.holding.data.AiCatalog
import com.automaton.holding.data.ApiService
import com.automaton.holding.data.ApprovalItem
import com.automaton.holding.data.GovernanceResponse
import com.automaton.holding.data.KillSwitchState
import com.automaton.holding.data.LiveEvent
import com.automaton.holding.data.PortfolioResponse
import com.automaton.holding.data.SystemStatusResponse
import com.automaton.holding.data.TreasuryResponse
import com.automaton.holding.data.ValidationReport
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import org.json.JSONObject

/**
 * Estado único da aplicação. Mantém os dados das 5 abas e o stream da LiveOps.
 *
 * Estratégia de actualização:
 *  - WebSocket alimenta os eventos (pensamento dos agentes) em tempo real;
 *  - *polling* leve a cada 20s actualiza números (tesouraria, aprovações),
 *    porque o backend pode estar atrás de um proxy que fecha sockets ociosos.
 *
 * Regra de ouro: nenhuma acção destrutiva (kill switch, saque, abate) é
 * enviada sem biometria confirmada no APK.
 */
class HoldingViewModel(application: Application) : AndroidViewModel(application) {

    private val api = ApiService()

    private val _status = MutableStateFlow<SystemStatusResponse?>(null)
    val status: StateFlow<SystemStatusResponse?> = _status.asStateFlow()

    private val _treasury = MutableStateFlow<TreasuryResponse?>(null)
    val treasury: StateFlow<TreasuryResponse?> = _treasury.asStateFlow()

    private val _approvals = MutableStateFlow<List<ApprovalItem>>(emptyList())
    val approvals: StateFlow<List<ApprovalItem>> = _approvals.asStateFlow()

    private val _portfolio = MutableStateFlow<PortfolioResponse?>(null)
    val portfolio: StateFlow<PortfolioResponse?> = _portfolio.asStateFlow()

    private val _governance = MutableStateFlow<GovernanceResponse?>(null)
    val governance: StateFlow<GovernanceResponse?> = _governance.asStateFlow()

    private val _validation = MutableStateFlow<ValidationReport?>(null)
    val validation: StateFlow<ValidationReport?> = _validation.asStateFlow()

    private val _ai = MutableStateFlow<AiCatalog?>(null)
    val ai: StateFlow<AiCatalog?> = _ai.asStateFlow()

    private val _events = MutableStateFlow<List<LiveEvent>>(emptyList())
    val events: StateFlow<List<LiveEvent>> = _events.asStateFlow()

    private val _loading = MutableStateFlow(true)
    val loading: StateFlow<Boolean> = _loading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private val _toast = MutableStateFlow<String?>(null)
    val toast: StateFlow<String?> = _toast.asStateFlow()

    private val _busyAction = MutableStateFlow<String?>(null)
    val busyAction: StateFlow<String?> = _busyAction.asStateFlow()

    /** Instante do último sinal de vida do backend (snapshot ou heartbeat). */
    private val _lastSignalAt = MutableStateFlow(0L)
    val lastSignalAt: StateFlow<Long> = _lastSignalAt.asStateFlow()

    private var pollJob: Job? = null
    private var streamJob: Job? = null
    private val maxEvents = 300
    private var lastSequence = 0L

    init {
        refreshAll()
        startPolling()
        startLiveStream()
    }

    // ----------------------------------------------------------- carregamento
    fun refreshAll() {
        viewModelScope.launch {
            _loading.value = true
            _error.value = null

            api.getSystemStatus()
                .onSuccess { _status.value = it }
                .onFailure { registerError("estado do sistema", it) }
            api.getTreasury()
                .onSuccess { _treasury.value = it }
                .onFailure { registerError("tesouraria", it) }
            api.getApprovals()
                .onSuccess { _approvals.value = it.items }
                .onFailure { registerError("aprovações", it) }
            api.getPortfolio()
                .onSuccess { _portfolio.value = it }
                .onFailure { registerError("portefólio", it) }
            api.getGovernance()
                .onSuccess { _governance.value = it }
                .onFailure { registerError("governança", it) }
            api.getValidation()
                .onSuccess { _validation.value = it }
                .onFailure { registerError("validação", it) }
            // Catálogo de agentes e modo do motor de IA (CrewAI vs nativo).
            api.getAgents()
                .onSuccess { _ai.value = it }
                .onFailure { registerError("agentes de IA", it) }

            _loading.value = false
        }
    }

    private fun registerError(scope: String, error: Throwable) {
        _error.value = "Falha ao carregar $scope: ${describe(error)}"
    }

    /** Traduz erros técnicos em mensagens acçãoáveis para o Presidente. */
    private fun describe(error: Throwable): String = when (error) {
        is ApiException -> when (error.code) {
            401 -> "token do APK inválido (X-Automaton-Token)"
            404 -> "recurso não encontrado"
            428 -> "aprovação biométrica obrigatória"
            in 500..599 -> "backend indisponível (${error.code})"
            else -> "HTTP ${error.code}"
        }
        // Um UnknownHostException/SocketTimeout diz só "Unable to resolve host":
        // quem instala o APK precisa de saber que a URL foi fixada no build.
        is java.net.UnknownHostException,
        is java.net.ConnectException ->
            "não consigo chegar a ${com.automaton.holding.BuildConfig.BASE_URL} " +
                "(verifique a rede; o endereço vem do build)"
        is java.net.SocketTimeoutException -> "o backend não respondeu a tempo"
        else -> error.message ?: "erro de rede"
    }

    private fun startPolling() {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            while (true) {
                delay(20_000)
                api.getSystemStatus().onSuccess { _status.value = it }
                api.getTreasury().onSuccess { _treasury.value = it }
                api.getApprovals().onSuccess { _approvals.value = it.items }
            }
        }
    }

    private fun startLiveStream() {
        streamJob?.cancel()
        streamJob = viewModelScope.launch {
            var backoff = 1_000L
            while (true) {
                try {
                    api.liveEvents().collect { event ->
                        backoff = 1_000L
                        ingest(event)
                    }
                } catch (error: Exception) {
                    _error.value = "Stream em pausa: ${describe(error)}"
                }
                delay(backoff)
                backoff = (backoff * 2).coerceAtMost(30_000L)
            }
        }
    }

    /** O evento `snapshot` traz o backlog completo; os restantes são pontuais. */
    /**
     * O snapshot e o heartbeat trazem ambos o estado do kill switch em
     * `payload.kill_switch`. Aplica-o ao estado da UI, se legível.
     */
    private fun aplicarKillSwitchDoPayload(event: LiveEvent) {
        val bruto = (event.payload as? JsonObject)?.get("kill_switch") ?: return
        runCatching {
            kotlinx.serialization.json.Json.decodeFromString(
                KillSwitchState.serializer(), bruto.toString()
            )
        }.onSuccess { estado ->
            _status.value = _status.value?.copy(killSwitch = estado)
        }
    }

    private suspend fun ingest(event: LiveEvent) {
        when (event.type) {
            "snapshot" -> {
                _lastSignalAt.value = System.currentTimeMillis()
                // O backend manda o estado do kill switch no snapshot. Ignorá-lo
                // obrigava a esperar pelo polling (20 s) para o ecrã reflectir
                // que o sistema estava parado — precisamente o instante em que
                // essa informação mais importa.
                aplicarKillSwitchDoPayload(event)
                val backlog = api.parseSnapshotEvents(event)
                val known = _events.value.filter { it.sequence <= lastSequence }
                val merged = (known + backlog)
                    .distinctBy { it.sequence }
                    .sortedBy { it.sequence }
                _events.value = merged.takeLast(maxEvents)
                lastSequence = _events.value.maxOfOrNull { it.sequence } ?: lastSequence
                if (event.message.isNotBlank()) {
                    addEvent(
                        event.copy(
                            type = "connected",
                            message = "Ligado ao backend · ${backlog.size} evento(s) em histórico"
                        )
                    )
                }
            }
            "heartbeat" -> {
                // Não aparece na lista (seria ruído), mas alimenta o indicador
                // de ligação e actualiza o kill switch: o heartbeat traz-no.
                _lastSignalAt.value = System.currentTimeMillis()
                aplicarKillSwitchDoPayload(event)
            }
            else -> {
                addEvent(event)
                lastSequence = maxOf(lastSequence, event.sequence)
                // Eventos críticos actualizam o estado imediatamente.
                // Este bloco corre dentro do collect do startLiveStream, que é
                // uma corrotina — daí a função ser `suspend`. Chamar
                // getSystemStatus() de uma função normal não compila
                // ("Suspend function should be called only from a coroutine").
                if (event.level == "critical" || event.level == "error") {
                    api.getSystemStatus().onSuccess { _status.value = it }
                }
            }
        }
    }

    private fun addEvent(event: LiveEvent) {
        val current = _events.value
        val updated = if (current.size >= maxEvents) {
            current.drop(current.size - maxEvents + 1) + event
        } else {
            current + event
        }
        _events.value = updated
    }

    fun clearEvents() {
        _events.value = emptyList()
        lastSequence = 0L
    }

    // ---------------------------------------------------------------- acções
    fun engageKillSwitch(reason: String) {
        viewModelScope.launch {
            _busyAction.value = "kill"
            api.engageKillSwitch(reason)
                .onSuccess {
                    _toast.value = "KILL SWITCH accionado — tudo congelado"
                    addEvent(
                        LiveEvent(
                            type = "kill_switch", agent = "security", level = "critical",
                            message = "Kill switch accionado pelo APK: $reason",
                        )
                    )
                    refreshAll()
                }
                .onFailure { _toast.value = "Falha ao accionar: ${describe(it)}" }
            _busyAction.value = null
        }
    }

    fun releaseKillSwitch() {
        viewModelScope.launch {
            _busyAction.value = "release"
            api.releaseKillSwitch()
                .onSuccess {
                    _toast.value = "Sistema libertado — operações retomadas"
                    refreshAll()
                }
                .onFailure { _toast.value = "Falha ao libertar: ${describe(it)}" }
            _busyAction.value = null
        }
    }

    fun decideApproval(
        approvalId: String,
        approve: Boolean,
        biometricVerified: Boolean,
        onDone: (Boolean) -> Unit = {},
    ) {
        viewModelScope.launch {
            _busyAction.value = approvalId
            api.decideApproval(approvalId, approve, biometricVerified)
                .onSuccess { response ->
                    val executed = extractExecutionStatus(response.execution?.toString())
                    _toast.value = when {
                        !approve -> "Decisão vetada e registada"
                        executed == "dry_run" ->
                            "Aprovado (dry-run: faltam credenciais do canal)"
                        executed != null && executed != "" -> "Aprovado e executado"
                        else -> "Aprovado"
                    }
                    refreshAll()
                    onDone(true)
                }
                .onFailure {
                    _toast.value = "Falha na decisão: ${describe(it)}"
                    onDone(false)
                }
            _busyAction.value = null
        }
    }

    /** Lê `execution.status` do JSON devolvido pelo backend (pode ser nulo). */
    private fun extractExecutionStatus(raw: String?): String? {
        if (raw.isNullOrBlank() || raw == "null") return null
        return try {
            JSONObject(raw).optString("status", "")
        } catch (error: Exception) {
            null
        }
    }

    fun killSquad(
        squadId: String,
        cause: String,
        biometricVerified: Boolean,
        onDone: (Boolean) -> Unit = {},
    ) {
        viewModelScope.launch {
            _busyAction.value = squadId
            api.killSquad(squadId, cause, biometricVerified)
                .onSuccess {
                    _toast.value = "Unidade abatida · lição gravada no cemitério"
                    refreshAll()
                    onDone(true)
                }
                .onFailure {
                    _toast.value = "Falha ao abater: ${describe(it)}"
                    onDone(false)
                }
            _busyAction.value = null
        }
    }

    fun requestPayout(amountUsd: Double, channel: String, onCreated: (String) -> Unit) {
        viewModelScope.launch {
            _busyAction.value = "payout_$channel"
            api.reservePayout(amountUsd, channel)
                .onSuccess {
                    _toast.value =
                        "Pedido criado · ${it.quote.mzn.let { mzn -> "%.2f MZN".format(mzn) }}" +
                            " — falta a tua biometria"
                    onCreated(it.approvalId)
                    refreshAll()
                }
                .onFailure { _toast.value = "Falha no saque: ${describe(it)}" }
            _busyAction.value = null
        }
    }

    fun runCycle(forceScan: Boolean = true) {
        viewModelScope.launch {
            _busyAction.value = "cycle"
            addEvent(
                LiveEvent(
                    type = "command", agent = "ceo_orchestrator", level = "info",
                    message = "Ciclo pedido manualmente pelo APK"
                )
            )
            api.runCycle(forceScan = forceScan)
                .onSuccess { report ->
                    _toast.value = report.summary.ifBlank {
                        "Ciclo ${report.cycleId} — ${report.status}"
                    }
                    refreshAll()
                }
                .onFailure {
                    _toast.value = "Falha no ciclo: ${describe(it)}"
                    addEvent(
                        LiveEvent(
                            type = "error", agent = "ceo_orchestrator", level = "error",
                            message = "Ciclo falhou: ${describe(it)}"
                        )
                    )
                }
            _busyAction.value = null
        }
    }

    fun consumeToast() {
        _toast.value = null
    }

    fun dismissError() {
        _error.value = null
    }

    override fun onCleared() {
        super.onCleared()
        pollJob?.cancel()
        streamJob?.cancel()
    }
}
