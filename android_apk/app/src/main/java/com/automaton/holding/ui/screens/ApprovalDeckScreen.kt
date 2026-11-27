package com.automaton.holding.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Fingerprint
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import com.automaton.holding.data.ApprovalItem
import com.automaton.holding.data.BiometricAuthManager
import com.automaton.holding.ui.theme.HoldingsColors
import kotlinx.coroutines.launch

/**
 * Aba 2 — ApprovalDeck: cartões de decisão.
 *
 * Anti-fadiga: os itens chegam já agrupados/pontuados pelo CEO; a lista abre
 * com os **críticos primeiro**. Aprovar ou vetar exige biometria (o backend
 * recusa qualquer decisão financeira sem `biometric_verified=true`).
 */
@Composable
fun ApprovalDeckScreen(
    approvals: List<ApprovalItem>,
    biometrics: BiometricAuthManager,
    activity: FragmentActivity,
    currencyRate: Double,
    onDecide: (String, Boolean, Boolean) -> Unit,
    busyAction: String?,
) {
    val scope = rememberCoroutineScope()
    var pendingBiometric by remember { mutableStateOf<Pair<ApprovalItem, Boolean>?>(null) }

    val pending = remember(approvals) {
        approvals.filter { it.status == "pending" }
            .sortedByDescending { if (it.priority == "critical") 1 else 0 }
    }

    Column(modifier = Modifier.fillMaxSize().padding(horizontal = 14.dp)) {
        Spacer(Modifier.height(12.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    "MESA DE APROVAÇÕES",
                    style = MaterialTheme.typography.labelSmall,
                    color = HoldingsColors.TextMuted,
                )
                Text(
                    "${pending.size} decisão(ões) pendente(s)",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold,
                )
            }
            Icon(
                Icons.Filled.Fingerprint,
                contentDescription = null,
                tint = if (biometrics.isAvailable()) HoldingsColors.Accent
                else HoldingsColors.Danger,
            )
        }
        Spacer(Modifier.height(4.dp))
        Text(
            biometrics.describeAvailability(),
            style = MaterialTheme.typography.labelSmall,
            color = HoldingsColors.TextMuted,
        )
        Spacer(Modifier.height(12.dp))

        if (pending.isEmpty()) {
            EmptyDeck()
        } else {
            LazyColumn(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                items(pending, key = { it.id }) { item ->
                    ApprovalCard(
                        item = item,
                        currencyRate = currencyRate,
                        busy = busyAction == item.id,
                        onApprove = { pendingBiometric = item to true },
                        onReject = { pendingBiometric = item to false },
                    )
                }
                item { Spacer(Modifier.height(60.dp)) }
            }
        }
    }

    // ------------------- Diálogo de biometria -------------------
    pendingBiometric?.let { (item, approve) ->
        val actionLabel = if (approve) "APROVAR" else "VETAR"
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { pendingBiometric = null },
            containerColor = HoldingsColors.Surface,
            titleContentColor = HoldingsColors.TextPrimary,
            textContentColor = HoldingsColors.TextMuted,
            title = { Text("$actionLabel: ${item.title}") },
            text = {
                Column {
                    Text(
                        if (item.amountUsd > 0)
                            "Montante: $${"%.2f".format(item.amountUsd)}"
                        else "Sem impacto financeiro directo.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Confirma com a tua impressão digital. A decisão fica " +
                            "registada na auditoria.",
                        style = MaterialTheme.typography.labelSmall,
                    )
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        val target = item
                        val approved = approve
                        pendingBiometric = null
                        scope.launch {
                            when (
                                biometrics.authenticate(
                                    activity,
                                    title = "$actionLabel operação",
                                    subtitle = target.title.take(60),
                                )
                            ) {
                                BiometricAuthManager.Result.SUCCESS ->
                                    onDecide(target.id, approved, true)
                                BiometricAuthManager.Result.UNAVAILABLE ->
                                    onDecide(target.id, approved, false)
                                else -> Unit // cancelado: nada acontece
                            }
                        }
                    },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = if (approve) HoldingsColors.Success
                        else HoldingsColors.Danger,
                        contentColor = Color.White,
                    ),
                ) { Text("Confirmar com biometria") }
            },
            dismissButton = {
                androidx.compose.material3.TextButton(
                    onClick = { pendingBiometric = null }
                ) { Text("Cancelar", color = HoldingsColors.TextMuted) }
            },
        )
    }
}

@Composable
private fun ApprovalCard(
    item: ApprovalItem,
    currencyRate: Double,
    busy: Boolean,
    onApprove: () -> Unit,
    onReject: () -> Unit,
) {
    val critical = item.priority == "critical"
    val accent = if (critical) HoldingsColors.Danger else HoldingsColors.Accent
    val kindLabel = when (item.kind) {
        "payout" -> "SAQUE"
        "spend" -> "GASTO"
        "kill_squad" -> "ABATE"
        "production" -> "PRODUÇÃO"
        "phase_change" -> "FASE"
        "license" -> "LICENÇA"
        "hire" -> "CONTRATAÇÃO"
        "fire" -> "DESPEDIMENTO"
        else -> item.kind.uppercase()
    }

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, if (critical) accent.copy(alpha = 0.6f) else HoldingsColors.Border,
                RoundedCornerShape(16.dp)),
        colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
        shape = RoundedCornerShape(16.dp),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(6.dp))
                        .background(accent.copy(alpha = 0.16f))
                        .padding(horizontal = 8.dp, vertical = 3.dp),
                ) {
                    Text(
                        kindLabel,
                        style = MaterialTheme.typography.labelSmall,
                        color = accent,
                        fontWeight = FontWeight.Bold,
                    )
                }
                Spacer(Modifier.width(8.dp))
                if (critical) {
                    Text(
                        "CRÍTICO",
                        style = MaterialTheme.typography.labelSmall,
                        color = HoldingsColors.Danger,
                        fontWeight = FontWeight.Bold,
                    )
                }
                Spacer(Modifier.weight(1f))
                Text(
                    item.createdAt.take(16).replace("T", " "),
                    style = MaterialTheme.typography.labelSmall,
                    color = HoldingsColors.TextMuted,
                )
            }
            Spacer(Modifier.height(10.dp))
            Text(
                item.title,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
            )
            if (item.reason.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(
                    item.reason,
                    style = MaterialTheme.typography.bodyMedium,
                    color = HoldingsColors.TextMuted,
                )
            }
            if (item.amountUsd > 0) {
                Spacer(Modifier.height(10.dp))
                Row {
                    Text(
                        "$${"%.2f".format(item.amountUsd)}",
                        style = MaterialTheme.typography.headlineMedium,
                        color = accent,
                        fontWeight = FontWeight.Bold,
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "≈ MZN ${"%.2f".format(item.amountUsd * currencyRate)}",
                        style = MaterialTheme.typography.labelSmall,
                        color = HoldingsColors.TextMuted,
                        modifier = Modifier.align(Alignment.Bottom).padding(bottom = 6.dp),
                    )
                }
            }
            Spacer(Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = onApprove,
                    enabled = !busy,
                    modifier = Modifier.weight(1f),
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = HoldingsColors.Success,
                        contentColor = Color(0xFF04120A),
                    ),
                ) {
                    if (busy) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(14.dp),
                            strokeWidth = 2.dp,
                            color = Color(0xFF04120A),
                        )
                    } else {
                        Icon(Icons.Filled.Check, contentDescription = null,
                            modifier = Modifier.size(16.dp))
                    }
                    Spacer(Modifier.width(6.dp))
                    Text("Aprovar")
                }
                OutlinedButton(
                    onClick = onReject,
                    enabled = !busy,
                    modifier = Modifier.weight(1f),
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.outlinedButtonColors(
                        contentColor = HoldingsColors.Danger,
                    ),
                ) {
                    Icon(Icons.Filled.Close, contentDescription = null,
                        modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(6.dp))
                    Text("Vetar")
                }
            }
        }
    }
}

@Composable
private fun EmptyDeck() {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .padding(bottom = 60.dp),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(
                Icons.Filled.Check,
                contentDescription = null,
                tint = HoldingsColors.Success,
                modifier = Modifier.size(42.dp),
            )
            Spacer(Modifier.height(10.dp))
            Text(
                "Nada para decidir agora",
                style = MaterialTheme.typography.titleMedium,
            )
            Text(
                "O CEO agrupa decisões para evitar fadiga de aprovação.",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted,
            )
        }
    }
}
