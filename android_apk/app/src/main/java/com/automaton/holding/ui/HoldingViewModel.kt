package com.automaton.holding.ui

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.automaton.holding.data.ApiException
import com.automaton.holding.data.CobrancaResponse
import com.automaton.holding.data.PayPalStatus
import com.automaton.holding.data.AiCatalog
import com.automaton.holding.data.ApiService
import com.automaton.holding.data.EndpointRules
import com.automaton.holding.data.EndpointSettings
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
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Dispatchers
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

    private val contexto: Context = application.applicationContext

    // O ApiService arranca com o que estiver guardado no telefone (ou com a
    // sugestão do build, se nunca foi configurado).
    private val api = ApiService(
        baseUrl = EndpointSettings.lerUrl(contexto),
        token = EndpointSettings.lerToken(contexto),
    )

    /** Endereço actualmente em uso (mostrado no ecrã de ligação). */
    private val _endpoint = MutableStateFlow(api.endpointAtual())
    val endpoint: StateFlow<String> = _endpoint.asStateFlow()

    /** Verdadeiro quando o backend respondeu ao último teste. */
    private val _ligado = MutableStateFlow<Boolean?>(null)
    val ligado: StateFlow<Boolean?> = _ligado.asStateFlow()

    private val _testando = MutableStateFlow(false)
    val testando: StateFlow<Boolean> = _testando.asStateFlow()

    /** Leitura mais recente do /health, para diagnóstico no ecrã. */
    private val _diagnostico = MutableStateFlow<String?>(null)
    val diagnostico: StateFlow<String?> = _diagnostico.asStateFlow()

    private val _status = MutableStateFlow<SystemStatusResponse?>(null)
    val status: StateFlow<SystemStatusResponse?> = _status.asStateFlow()

    private val _treasury = MutableStateFlow<TreasuryResponse?>(null)
    val treasury: StateFlow<TreasuryResponse?> = _treasury.asStateFlow()

    private val _paypal = MutableStateFlow<PayPalStatus?>(null)
    val paypal: StateFlow<PayPalStatus?> = _paypal.asStateFlow()

    private val _cobranca = MutableStateFlow<CobrancaResponse?>(null)
    val cobranca: StateFlow<CobrancaResponse?> = _cobranca.asStateFlow()

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
    /**
     * Cria uma cobrança PayPal. Sem chaves o backend devolve dry-run e a
     * app mostra-o como aviso — nunca como sucesso silencioso.
     */
    fun criarCobranca(valorUsd: Double, descricao: String) {
        viewModelScope.launch {
            _busyAction.value = "cobranca_paypal"
            api.criarCobranca(valorUsd, descricao)
                .onSuccess { _cobranca.value = it }
                .onFailure { registerError("cobrança PayPal", it) }
            _busyAction.value = null
        }
    }

    fun limparCobranca() {
        _cobranca.value = null
    }

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
            // O PayPal é opcional: sem chaves devolve `configurado=false` e
            // a app mostra o que falta. Não é um erro, é um estado.
            api.getPayPalStatus()
                .onSuccess { _paypal.value = it }
                .onFailure { _paypal.value = null }
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
            "não consigo chegar a $baseUrl (toque no ícone de ligação " +
                "no topo para mudar o endereço)"
        is java.net.SocketTimeoutException -> "o backend não respondeu a tempo"
        else -> error.message ?: "erro de rede"
    }

    /** Endereço em uso; lido de `api` porque pode mudar em execução. */
    private val baseUrl: String get() = api.baseUrl

    // ------------------------------------------------------- ligação ao backend
    /**
     * Testa o endereço **antes** de o adoptar.
     *
     * Guardar primeiro e descobrir depois que estava errado deixava o
     * utilizador sem backend e sem saber porquê. Aqui o teste é explícito:
     * `GET /health`, e só se responder é que se grava e se recomeça o stream.
     * Não grava nada em caso de falha.
     */
    fun testarELigar(endereco: String, token: String) {
        viewModelScope.launch {
            _testando.value = true
            _diagnostico.value = null
            _ligado.value = null

            val validacao = EndpointRules.validar(endereco)
            if (validacao is EndpointRules.Validacao.Erro) {
                _diagnostico.value = validacao.motivo
                _ligado.value = false
                _testando.value = false
                return@launch
            }
            val url = (validacao as EndpointRules.Validacao.Ok).url

            val sonda = ApiService(baseUrl = url, token = token)
            val inicio = System.currentTimeMillis()
            // Com 20 s de connectTimeout, um IP errado na mesma Wi-Fi ficava
            // 20 s a "testar…". Uma sonda de saúde deve desistir depressa; o
            // timeout normal do cliente serve para tráfego, não para isto.
            val resultado = withContext(Dispatchers.IO) {
                withTimeoutOrNull(8_000) { sonda.health() }
                    ?: Result.failure(
                        java.net.SocketTimeoutException("sonda de /health excedeu 8 s")
                    )
            }
            val ms = System.currentTimeMillis() - inicio

            resultado
                .onSuccess { saude ->
                    // Só agora se adopta o endereço — respondeu de facto.
                    EndpointSettings.gravar(contexto, url, token)
                    api.reconfigurar(url, token)
                    _endpoint.value = api.endpointAtual()
                    _ligado.value = true
                    _diagnostico.value = buildString {
                        append("ligado em ${ms} ms")
                        if (!saude.status.isNullOrBlank()) append(" · ${saude.status}")
                        if (!saude.version.isNullOrBlank()) append(" · v${saude.version}")
                        if (token.isBlank()) append(" · sem token (só leitura)")
                    }
                    reiniciarTudo()
                }
                .onFailure {
                    _ligado.value = false
                    _diagnostico.value = explicarFalhaDeLigacao(url, it)
                }
            _testando.value = false
        }
    }

    /**
     * Falhas de ligação explicadas por causa provável, em vez de
     * «erro de rede». Cada caso corresponde a um problema real que o
     * utilizador pode resolver sozinho.
     */
    private fun explicarFalhaDeLigacao(url: String, erro: Throwable): String {
        val alvo = url.substringAfter("://")
        return when (erro) {
            is java.net.UnknownHostException ->
                "não encontro \"$alvo\". Confirme o endereço e que o telemóvel " +
                    "está na mesma rede Wi-Fi do computador."
            is java.net.ConnectException ->
                "\"$alvo\" recusou a ligação. O backend está a correr? Está " +
                    "publicado com --host 0.0.0.0 (e não 127.0.0.1)?"
            is java.net.SocketTimeoutException ->
                "\"$alvo\" não respondeu a tempo. Firewall do computador a " +
                    "bloquear a porta?"
            is ApiException -> when (erro.code) {
                401 -> "o backend respondeu, mas o token não serve (401)."
                404 -> "o backend respondeu, mas /health não existe (404). " +
                    "É o serviço certo?"
                else -> "o backend respondeu HTTP ${erro.code}."
            }
            else -> {
                val msg = erro.message.orEmpty()
                if (errPatternCleartext.containsMatchIn(msg)) {
                    "o Android bloqueou HTTP em claro para \"$alvo\". " +
                        "Use https:// ou um endereço da rede local."
                } else {
                    "falhou: ${msg.ifBlank { erro::class.simpleName ?: "erro" }}"
                }
            }
        }
    }

    private val errPatternCleartext =
        Regex("Cleartext|cleartext", RegexOption.IGNORE_CASE)

    /** Recomeça polling e stream com o endpoint actual. */
    private fun reiniciarTudo() {
        pollJob?.cancel()
        streamJob?.cancel()
        _events.value = emptyList()
        lastSequence = 0L
        refreshAll()
        startPolling()
        startLiveStream()
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
                    // O `sequence` tem de ser ÚNICO. Este aviso sintético
                    // herdava o `sequence` do envelope (frequentemente 0) e
                    // podia colidir com um evento do backlog — e o
                    // `LazyColumn` de eventos usa `key = { it.sequence }`.
                    // Duas chaves iguais = IllegalArgumentException e a app
                    // FECHA. Aconteceu mesmo no emulador (2026-11-28), ao
                    // premir «Ciclo»: "Key 0 was already used".
                    //
                    // Um aviso de ligação não é um evento do bus: dar-lhe um
                    // número negativo garante que nunca colide com o
                    // historico real, que começa em 1.
                    addEvent(
                        event.copy(
                            type = "connected",
                            sequence = -1L,
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
                    // `sequence` NEGATIVO, pela mesma razão do aviso de
                    // ligação: este evento é local, não veio do bus. Sem
                    // sequence próprio colidia com o aviso «connected» — os
                    // dois a 0 — e o LazyColumn rebentava com "Key 0 was
                    // already used". O crash do botão «Ciclo» era este mesmo
                    // defeito, num caminho vizinho.
                    addEvent(
                        LiveEvent(
                            sequence = -2L,
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
                    // -3L: os eventos locais usam sequências negativas
                    // (-1 ligação, -2 kill switch, -3 comando) para nunca
                    // colidirem com o historico real do bus, que começa em 1.
                    sequence = -3L,
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
                            sequence = -4L,
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
