package com.automaton.holding

import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountBalanceWallet
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.Gavel
import androidx.compose.material.icons.filled.HowToVote
import androidx.compose.material.icons.filled.PowerSettingsNew
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Wifi
import androidx.compose.material.icons.filled.WifiOff
import androidx.compose.material.icons.filled.Work
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.viewmodel.compose.viewModel
import com.automaton.holding.data.BiometricAuthManager
import com.automaton.holding.ui.ConnectionDialog
import com.automaton.holding.ui.HoldingViewModel
import com.automaton.holding.ui.screens.ApprovalDeckScreen
import com.automaton.holding.ui.screens.GovernanceScreen
import com.automaton.holding.ui.screens.LiveOpsScreen
import com.automaton.holding.ui.screens.SquadsPortfolioScreen
import com.automaton.holding.ui.screens.TreasuryScreen
import com.automaton.holding.ui.theme.AutomatonHoldingTheme
import com.automaton.holding.ui.theme.HoldingsColors

/** Abas do APK (5 camadas de supervisão). */
enum class HoldingTab(val label: String, val icon: ImageVector) {
    LIVE_OPS("Ao Vivo", Icons.Filled.Dashboard),
    APPROVALS("Aprovar", Icons.Filled.HowToVote),
    TREASURY("Tesouro", Icons.Filled.AccountBalanceWallet),
    GOVERNANCE("Diretoria", Icons.Filled.Gavel),
    SQUADS("Esquadrões", Icons.Filled.Work),
}

class MainActivity : FragmentActivity() {

    private lateinit var biometrics: BiometricAuthManager

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        biometrics = BiometricAuthManager(this)

        setContent {
            AutomatonHoldingTheme {
                val viewModel: HoldingViewModel = viewModel()
                HoldingApp(
                    viewModel = viewModel,
                    biometrics = biometrics,
                    activity = this,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HoldingApp(
    viewModel: HoldingViewModel,
    biometrics: BiometricAuthManager,
    activity: FragmentActivity,
) {
    val status by viewModel.status.collectAsState()
    val treasury by viewModel.treasury.collectAsState()
    val approvals by viewModel.approvals.collectAsState()
    val portfolio by viewModel.portfolio.collectAsState()
    val governance by viewModel.governance.collectAsState()
    val validation by viewModel.validation.collectAsState()
    val ai by viewModel.ai.collectAsState()
    val events by viewModel.events.collectAsState()
    val loading by viewModel.loading.collectAsState()
    val error by viewModel.error.collectAsState()
    val toast by viewModel.toast.collectAsState()
    val busyAction by viewModel.busyAction.collectAsState()

    val endpoint by viewModel.endpoint.collectAsState()
    val ligado by viewModel.ligado.collectAsState()
    val testando by viewModel.testando.collectAsState()
    val diagnostico by viewModel.diagnostico.collectAsState()

    // Abre sozinho quando o primeiro refresh falhou: um ecrã vazio sem
    // explicação é o pior resultado possível para quem instala o APK.
    var mostrarLigacao by remember { mutableStateOf(false) }
    var abriuAutomaticamente by remember { mutableStateOf(false) }
    LaunchedEffect(status, loading, ligado) {
        val semDados = status == null && !loading
        if (semDados && ligado != true && !abriuAutomaticamente) {
            mostrarLigacao = true
            abriuAutomaticamente = true
        }
    }

    var tab by remember { mutableStateOf(HoldingTab.LIVE_OPS) }
    var showKillDialog by remember { mutableStateOf(false) }
    var killReason by remember { mutableStateOf("") }

    val snackbarHostState = remember { SnackbarHostState() }
    val killEngaged = status?.killSwitch?.engaged == true
    val pendingCount = status?.approvals?.pending ?: approvals.size
    val criticalCount = status?.approvals?.critical ?: 0

    LaunchedEffect(toast) {
        toast?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.consumeToast()
        }
    }
    LaunchedEffect(error) {
        error?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.dismissError()
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        containerColor = HoldingsColors.Background,
        topBar = {
            TopAppBar(
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = HoldingsColors.Surface,
                    titleContentColor = HoldingsColors.TextPrimary,
                ),
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Surface(
                            shape = RoundedCornerShape(10.dp),
                            color = HoldingsColors.Accent.copy(alpha = 0.16f),
                            modifier = Modifier.size(36.dp),
                        ) {
                            Box(contentAlignment = Alignment.Center) {
                                Icon(
                                    imageVector = Icons.Filled.PowerSettingsNew,
                                    contentDescription = null,
                                    tint = HoldingsColors.Accent,
                                    modifier = Modifier.size(20.dp),
                                )
                            }
                        }
                        Spacer(Modifier.width(10.dp))
                        Column {
                            Text(
                                "Automaton Holding",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold,
                            )
                            Text(
                                if (killEngaged) "SISTEMA CONGELADO"
                                else "Fase ${status?.treasury?.phase ?: 1} · " +
                                    "$pendingCount pendentes",
                                style = MaterialTheme.typography.labelSmall,
                                color = if (killEngaged) HoldingsColors.Danger
                                else HoldingsColors.TextMuted,
                            )
                        }
                    }
                },
                actions = {
                    // Indicador/troca de endpoint: verde quando o backend
                    // respondeu, vermelho quando não, cinza quando por testar.
                    IconButton(onClick = { mostrarLigacao = true }) {
                        Icon(
                            imageVector = if (ligado == false) Icons.Filled.WifiOff
                            else Icons.Filled.Wifi,
                            contentDescription = "Ligação ao backend: $endpoint",
                            tint = when (ligado) {
                                true -> HoldingsColors.Accent
                                false -> HoldingsColors.Danger
                                null -> HoldingsColors.TextMuted
                            },
                        )
                    }
                    IconButton(onClick = { viewModel.refreshAll() }) {
                        if (loading) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(18.dp),
                                color = HoldingsColors.Accent,
                                strokeWidth = 2.dp,
                            )
                        } else {
                            Icon(
                                Icons.Filled.Refresh,
                                contentDescription = "Actualizar",
                                tint = HoldingsColors.TextMuted,
                            )
                        }
                    }
                    // ====== BOTÃO VERMELHO: KILL SWITCH ======
                    Surface(
                        shape = RoundedCornerShape(12.dp),
                        color = if (killEngaged) HoldingsColors.Danger
                        else HoldingsColors.DangerDark,
                        border = BorderStroke(2.dp, HoldingsColors.Danger),
                        modifier = Modifier
                            .padding(end = 12.dp)
                            .size(46.dp)
                            .clickable {
                                if (killEngaged) viewModel.releaseKillSwitch()
                                else showKillDialog = true
                            },
                    ) {
                        Box(contentAlignment = Alignment.Center) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                Icon(
                                    Icons.Filled.PowerSettingsNew,
                                    contentDescription = "Kill switch",
                                    tint = Color.White,
                                    modifier = Modifier.size(20.dp),
                                )
                                Text(
                                    if (killEngaged) "LIVRE" else "STOP",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = Color.White,
                                    fontSize = MaterialTheme.typography.labelSmall.fontSize,
                                )
                            }
                        }
                    }
                },
            )
        },
        bottomBar = {
            NavigationBar(
                containerColor = HoldingsColors.Surface,
                tonalElevation = 0.dp,
            ) {
                HoldingTab.entries.forEach { item ->
                    val selected = tab == item
                    NavigationBarItem(
                        selected = selected,
                        onClick = { tab = item },
                        icon = {
                            if (item == HoldingTab.APPROVALS && pendingCount > 0) {
                                BadgedBox(badge = {
                                    Badge(
                                        containerColor = if (criticalCount > 0)
                                            HoldingsColors.Danger else HoldingsColors.Accent
                                    ) { Text(pendingCount.toString()) }
                                }) {
                                    Icon(item.icon, contentDescription = item.label)
                                }
                            } else {
                                Icon(item.icon, contentDescription = item.label)
                            }
                        },
                        label = { Text(item.label) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = HoldingsColors.Accent,
                            selectedTextColor = HoldingsColors.Accent,
                            unselectedIconColor = HoldingsColors.TextMuted,
                            unselectedTextColor = HoldingsColors.TextMuted,
                            indicatorColor = HoldingsColors.Accent.copy(alpha = 0.12f),
                        ),
                    )
                }
            }
        },
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .background(HoldingsColors.Background)
        ) {
            when (tab) {
                HoldingTab.LIVE_OPS -> LiveOpsScreen(
                    events = events,
                    status = status,
                    onClear = viewModel::clearEvents,
                    onRunCycle = viewModel::runCycle,
                    busy = busyAction == "cycle",
                )

                HoldingTab.APPROVALS -> ApprovalDeckScreen(
                    approvals = approvals,
                    biometrics = biometrics,
                    activity = activity,
                    currencyRate = treasury?.channels?.fx?.usdMzn ?: 63.5,
                    onDecide = { id, approve, bio ->
                        viewModel.decideApproval(id, approve, bio)
                    },
                    busyAction = busyAction,
                )

                HoldingTab.TREASURY -> TreasuryScreen(
                    treasury = treasury,
                    ownerPocketUsd = treasury?.channels?.ownerPocketUsd
                        ?: treasury?.snapshot?.distributableOwnerUsd ?: 0.0,
                    onRequestPayout = { amount, channel, onCreated ->
                        viewModel.requestPayout(amount, channel, onCreated)
                    },
                    onOpenApprovals = { tab = HoldingTab.APPROVALS },
                    onRefreshQuote = viewModel::refreshAll,
                    busyAction = busyAction,
                )

                HoldingTab.GOVERNANCE -> GovernanceScreen(
                    governance = governance,
                    status = status,
                    validation = validation,
                    ai = ai,
                    busy = loading,
                )

                HoldingTab.SQUADS -> SquadsPortfolioScreen(
                    portfolio = portfolio,
                    biometrics = biometrics,
                    activity = activity,
                    onKill = { id, cause, bio, onDone ->
                        viewModel.killSquad(id, cause, bio, onDone)
                    },
                    busyAction = busyAction,
                )
            }
        }
    }

    // ---------------- Diálogo de confirmação do kill switch ----------------
    if (showKillDialog) {
        AlertDialog(
            onDismissRequest = { showKillDialog = false },
            containerColor = HoldingsColors.Surface,
            titleContentColor = HoldingsColors.TextPrimary,
            textContentColor = HoldingsColors.TextMuted,
            title = { Text("Accionar KILL SWITCH?") },
            text = {
                Column {
                    Text(
                        "Congela todos os agentes, ciclos e gastos em menos de " +
                            "1 segundo. O sistema só retoma depois de libertares.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(
                        value = killReason,
                        onValueChange = { killReason = it },
                        label = { Text("Motivo (registado na auditoria)") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                    )
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        viewModel.engageKillSwitch(
                            killReason.ifBlank { "paragem de emergência (APK)" }
                        )
                        killReason = ""
                        showKillDialog = false
                    },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = HoldingsColors.Danger,
                        contentColor = Color.White,
                    ),
                ) { Text("PARAR TUDO") }
            },
            dismissButton = {
                TextButton(onClick = { showKillDialog = false }) {
                    Text("Cancelar", color = HoldingsColors.TextMuted)
                }
            },
        )
    }

    // ---------------- Ecrã de ligação ao backend ----------------
    if (mostrarLigacao) {
        ConnectionDialog(
            endpointAtual = endpoint,
            tokenAtual = tokenAtualDoModelo(viewModel),
            ligado = ligado,
            testando = testando,
            diagnostico = diagnostico,
            onTestar = { url, tk -> viewModel.testarELigar(url, tk) },
            onFechar = { mostrarLigacao = false },
        )
    }

    // Aviso persistente quando o sistema está congelado.
    AnimatedVisibility(visible = killEngaged) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
            Surface(
                color = HoldingsColors.Danger.copy(alpha = 0.92f),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 4.dp),
            ) {
                Text(
                    "🔴 KILL SWITCH ACTIVO — ${status?.killSwitch?.reason ?: "paragem"}",
                    modifier = Modifier.padding(8.dp),
                    color = Color.White,
                    textAlign = TextAlign.Center,
                    style = MaterialTheme.typography.labelLarge,
                )
            }
        }
    }
}

/**
 * O token nunca é mostrado no ecrã de ligação por omissão: expô-lo num ecrã
 * que se abre no meio do trabalho é um risco desnecessário. O campo começa
 * vazio e só substitui o existente se o utilizador escrever algo.
 */
private fun tokenAtualDoModelo(viewModel: HoldingViewModel): String = ""
