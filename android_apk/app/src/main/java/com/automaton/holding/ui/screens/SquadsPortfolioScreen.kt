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
import androidx.compose.material.icons.filled.PowerSettingsNew
import androidx.compose.material.icons.filled.WorkOff
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import com.automaton.holding.data.BiometricAuthManager
import com.automaton.holding.data.PortfolioResponse
import com.automaton.holding.data.SquadRecord
import com.automaton.holding.ui.theme.HoldingsColors
import kotlinx.coroutines.launch

/**
 * Aba 5 — SquadsPortfolio: negócios activos com TTFD, CAC, LTV.
 *
 * Cada cartão mostra a saúde da unidade e permite **desligar** o esquadrão
 * (com biometria). O abate grava automaticamente a lição no cemitério.
 */
@Composable
fun SquadsPortfolioScreen(
    portfolio: PortfolioResponse?,
    biometrics: BiometricAuthManager,
    activity: FragmentActivity,
    onKill: (String, String, Boolean, (Boolean) -> Unit) -> Unit,
    busyAction: String?,
) {
    val scope = rememberCoroutineScope()
    val squads = portfolio?.squads ?: emptyList()
    val summary = portfolio?.summary
    var killTarget by remember { mutableStateOf<SquadRecord?>(null) }
    var killCause by remember { mutableStateOf("") }

    Column(modifier = Modifier.fillMaxSize().padding(horizontal = 14.dp)) {
        Spacer(Modifier.height(12.dp))
        Row(modifier = Modifier.fillMaxWidth()) {
            SummaryPill("ACTIVOS", summary?.active ?: 0, HoldingsColors.Success,
                Modifier.weight(1f))
            SummaryPill("ABATIDOS", summary?.killed ?: 0, HoldingsColors.TextMuted,
                Modifier.weight(1f))
            SummaryPill("RECEITA", "$${"%.0f".format(summary?.revenueUsd ?: 0.0)}",
                HoldingsColors.Accent, Modifier.weight(1f))
        }
        Spacer(Modifier.height(12.dp))

        if (squads.isEmpty()) {
            EmptyPortfolio()
        } else {
            LazyColumn(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                items(squads, key = { it.id }) { squad ->
                    SquadCard(
                        squad = squad,
                        busy = busyAction == squad.id,
                        onKill = {
                            killTarget = squad
                            killCause = ""
                        },
                    )
                }
                item { Spacer(Modifier.height(60.dp)) }
            }
        }
    }

    // ---------------------- Diálogo de abate ----------------------
    killTarget?.let { squad ->
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { killTarget = null },
            containerColor = HoldingsColors.Surface,
            titleContentColor = HoldingsColors.Danger,
            textContentColor = HoldingsColors.TextMuted,
            title = { Text("Desligar ${squad.name}?") },
            text = {
                Column {
                    Text(
                        "A unidade é encerrada e a lição fica registada no " +
                            "cemitério de negócios (nunca repetir o erro).",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Spacer(Modifier.height(10.dp))
                    OutlinedTextField(
                        value = killCause,
                        onValueChange = { killCause = it },
                        label = { Text("Causa do abate") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = false,
                    )
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        val target = squad
                        val cause = killCause.ifBlank { "abate manual pelo Presidente" }
                        killTarget = null
                        scope.launch {
                            when (
                                biometrics.authenticate(
                                    activity,
                                    title = "Abater unidade",
                                    subtitle = target.name.take(60),
                                )
                            ) {
                                BiometricAuthManager.Result.SUCCESS ->
                                    onKill(target.id, cause, true) { }
                                BiometricAuthManager.Result.UNAVAILABLE ->
                                    onKill(target.id, cause, false) { }
                                else -> Unit
                            }
                        }
                    },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = HoldingsColors.Danger,
                        contentColor = Color.White,
                    ),
                ) { Text("Abater com biometria") }
            },
            dismissButton = {
                TextButton(onClick = { killTarget = null }) {
                    Text("Cancelar", color = HoldingsColors.TextMuted)
                }
            },
        )
    }
}

@Composable
private fun SquadCard(
    squad: SquadRecord,
    busy: Boolean,
    onKill: () -> Unit,
) {
    val statusColor = when (squad.status) {
        "active" -> HoldingsColors.Success
        "paused" -> HoldingsColors.Warning
        "killed" -> HoldingsColors.TextMuted
        else -> HoldingsColors.TextMuted
    }
    val healthRatio = when {
        squad.revenueUsd > 0 && (squad.ltvUsd ?: 0.0) > (squad.cacUsd ?: 0.0) -> 0.9f
        squad.revenueUsd > 0 -> 0.6f
        squad.daysAlive > 14 -> 0.25f
        else -> 0.5f
    }

    Card(
        colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
        shape = RoundedCornerShape(16.dp),
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, HoldingsColors.Border, RoundedCornerShape(16.dp)),
    ) {
        Column(modifier = Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(7.dp)
                        .clip(RoundedCornerShape(4.dp))
                        .background(statusColor),
                )
                Spacer(Modifier.width(8.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        squad.name,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                        maxLines = 1,
                    )
                    Text(
                        "${squad.type.replace("_", " ")} · ${squad.niche}",
                        style = MaterialTheme.typography.labelSmall,
                        color = HoldingsColors.TextMuted,
                        maxLines = 1,
                    )
                }
                Text(
                    squad.status.uppercase(),
                    style = MaterialTheme.typography.labelSmall,
                    color = statusColor,
                    fontWeight = FontWeight.Bold,
                )
            }

            Spacer(Modifier.height(10.dp))
            Row(modifier = Modifier.fillMaxWidth()) {
                MiniMetric("TTFD", squad.ttfdDays?.let { "%.0fd".format(it) } ?: "—",
                    Modifier.weight(1f))
                MiniMetric("CAC", squad.cacUsd?.let { "$${"%.2f".format(it)}" } ?: "—",
                    Modifier.weight(1f))
                MiniMetric("LTV", squad.ltvUsd?.let { "$${"%.2f".format(it)}" } ?: "—",
                    Modifier.weight(1f))
                MiniMetric("RECEITA", "$${"%.2f".format(squad.revenueUsd)}",
                    Modifier.weight(1f))
            }

            Spacer(Modifier.height(10.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "Views ${squad.views} · Cliques ${squad.clicks} · " +
                        "Conv. ${squad.conversions}",
                    style = MaterialTheme.typography.labelSmall,
                    color = HoldingsColors.TextMuted,
                    modifier = Modifier.weight(1f),
                )
                Text(
                    "${squad.daysAlive.toInt()} dias",
                    style = MaterialTheme.typography.labelSmall,
                    color = HoldingsColors.TextMuted,
                )
            }
            Spacer(Modifier.height(6.dp))
            LinearProgressIndicator(
                progress = { healthRatio },
                modifier = Modifier
                    .fillMaxWidth()
                    .height(4.dp)
                    .clip(RoundedCornerShape(2.dp)),
                color = when {
                    healthRatio >= 0.8 -> HoldingsColors.Success
                    healthRatio >= 0.5 -> HoldingsColors.Warning
                    else -> HoldingsColors.Danger
                },
                trackColor = HoldingsColors.SurfaceVariant,
            )

            if (squad.status == "active") {
                Spacer(Modifier.height(10.dp))
                Button(
                    onClick = onKill,
                    enabled = !busy,
                    shape = RoundedCornerShape(10.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = HoldingsColors.Danger.copy(alpha = 0.14f),
                        contentColor = HoldingsColors.Danger,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (busy) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(14.dp),
                            strokeWidth = 2.dp,
                            color = HoldingsColors.Danger,
                        )
                        Spacer(Modifier.width(8.dp))
                    } else {
                        Icon(
                            Icons.Filled.PowerSettingsNew,
                            contentDescription = null,
                            modifier = Modifier.size(15.dp),
                        )
                        Spacer(Modifier.width(6.dp))
                    }
                    Text("Desligar Unidade", style = MaterialTheme.typography.labelLarge)
                }
            }
        }
    }
}

@Composable
private fun MiniMetric(label: String, value: String, modifier: Modifier = Modifier) {
    Column(modifier = modifier) {
        Text(label, style = MaterialTheme.typography.labelSmall,
            color = HoldingsColors.TextMuted)
        Text(
            value,
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

@Composable
private fun SummaryPill(
    label: String,
    value: Any,
    accent: Color,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier.padding(end = 6.dp),
        colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Text(label, style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted)
            Text(
                value.toString(),
                style = MaterialTheme.typography.titleMedium,
                color = accent,
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun EmptyPortfolio() {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .padding(bottom = 60.dp),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(
                Icons.Filled.WorkOff,
                contentDescription = null,
                tint = HoldingsColors.TextMuted,
                modifier = Modifier.size(42.dp),
            )
            Spacer(Modifier.height(10.dp))
            Text("Sem esquadrões activos", style = MaterialTheme.typography.titleMedium)
            Text(
                "O ciclo do CEO cria unidades quando a demanda é validada.",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted,
            )
        }
    }
}
