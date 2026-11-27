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
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.DeleteSweep
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.automaton.holding.data.LiveEvent
import com.automaton.holding.data.SystemStatusResponse
import com.automaton.holding.ui.theme.HoldingsColors
import com.automaton.holding.ui.theme.TerminalTextStyle
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Aba 1 — LiveOps: terminal ao vivo do "pensamento" dos agentes.
 *
 * Mostra o Chain-of-Thought publicado pelo backend (eventos), o estado do
 * kill switch, o orçamento diário e o número de aprovações pendentes.
 */
@Composable
fun LiveOpsScreen(
    events: List<LiveEvent>,
    status: SystemStatusResponse?,
    onClear: () -> Unit,
    onRunCycle: () -> Unit,
    busy: Boolean,
) {
    val listState = rememberLazyListState()
    val timeFormatter = remember {
        DateTimeFormatter.ofPattern("HH:mm:ss").withZone(ZoneId.systemDefault())
    }

    LaunchedEffect(events.size) {
        if (events.isNotEmpty()) {
            listState.animateScrollToItem(events.lastIndex)
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(horizontal = 14.dp)) {
        Spacer(Modifier.height(12.dp))
        StatusStrip(status)
        Spacer(Modifier.height(10.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "TERMINAL DE OPERAÇÕES",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted,
                modifier = Modifier.weight(1f),
            )
            IconButton(onClick = onClear, modifier = Modifier.size(32.dp)) {
                Icon(
                    Icons.Filled.DeleteSweep,
                    contentDescription = "Limpar",
                    tint = HoldingsColors.TextMuted,
                    modifier = Modifier.size(18.dp),
                )
            }
            Button(
                onClick = onRunCycle,
                enabled = !busy,
                shape = RoundedCornerShape(10.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = HoldingsColors.Accent.copy(alpha = 0.15f),
                    contentColor = HoldingsColors.Accent,
                ),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(
                    horizontal = 12.dp, vertical = 6.dp
                ),
            ) {
                if (busy) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(14.dp),
                        strokeWidth = 2.dp,
                        color = HoldingsColors.Accent,
                    )
                } else {
                    Icon(
                        Icons.Filled.PlayArrow,
                        contentDescription = null,
                        modifier = Modifier.size(16.dp),
                    )
                }
                Spacer(Modifier.width(6.dp))
                Text("Ciclo", style = MaterialTheme.typography.labelSmall)
            }
        }

        Spacer(Modifier.height(8.dp))

        if (events.isEmpty()) {
            WaitingPlaceholder(Modifier.weight(1f))
        } else {
            LazyColumn(
                state = listState,
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(HoldingsColors.Surface)
                    .border(1.dp, HoldingsColors.Border, RoundedCornerShape(12.dp))
                    .padding(10.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                items(events, key = { it.sequence }) { event ->
                    EventRow(event, timeFormatter)
                }
            }
        }
    }
}

@Composable
private fun StatusStrip(status: SystemStatusResponse?) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        MetricChip(
            label = "PATRIMÓNIO",
            value = "$${"%.2f".format(status?.treasury?.totalAssetsUsd ?: 0.0)}",
            color = HoldingsColors.Success,
            modifier = Modifier.weight(1f),
        )
        MetricChip(
            label = "RECEITA 30D",
            value = "$${"%.2f".format(status?.treasury?.monthlyRevenueUsd ?: 0.0)}",
            color = HoldingsColors.Accent,
            modifier = Modifier.weight(1f),
        )
        MetricChip(
            label = "PENDENTES",
            value = (status?.approvals?.pending ?: 0).toString(),
            color = if ((status?.approvals?.critical ?: 0) > 0) HoldingsColors.Danger
            else HoldingsColors.Warning,
            modifier = Modifier.weight(1f),
        )
    }
}

@Composable
private fun MetricChip(
    label: String,
    value: String,
    color: Color,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier,
        colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Text(label, style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted)
            Spacer(Modifier.height(4.dp))
            Text(
                value,
                style = MaterialTheme.typography.titleMedium,
                color = color,
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun EventRow(
    event: LiveEvent,
    formatter: DateTimeFormatter,
) {
    val accent = when (event.level) {
        "critical" -> HoldingsColors.Danger
        "error" -> Color(0xFFF87171)
        "warning" -> HoldingsColors.Warning
        else -> HoldingsColors.Accent
    }
    val time = remember(event.timestamp) {
        runCatching {
            formatter.format(Instant.parse(event.timestamp.ifBlank { "1970-01-01T00:00:00Z" }))
        }.getOrDefault("--:--:--")
    }

    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.Top,
    ) {
        Box(
            modifier = Modifier
                .padding(top = 5.dp)
                .size(6.dp)
                .clip(CircleShape)
                .background(accent),
        )
        Spacer(Modifier.width(8.dp))
        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    time,
                    style = TerminalTextStyle.copy(color = HoldingsColors.TextMuted),
                )
                Spacer(Modifier.width(6.dp))
                Text(
                    event.type.uppercase(),
                    style = TerminalTextStyle.copy(
                        color = accent,
                        fontWeight = FontWeight.Bold,
                    ),
                )
            }
            Text(
                "[${event.agent}] ${event.message}",
                style = TerminalTextStyle,
            )
        }
    }
}

@Composable
private fun WaitingPlaceholder(modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(HoldingsColors.Surface)
            .border(1.dp, HoldingsColors.Border, RoundedCornerShape(12.dp))
            .padding(bottom = 12.dp),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            CircularProgressIndicator(
                color = HoldingsColors.Accent,
                strokeWidth = 2.dp,
                modifier = Modifier.size(26.dp),
            )
            Spacer(Modifier.height(12.dp))
            Text(
                "À espera do stream de agentes…",
                style = MaterialTheme.typography.bodyMedium,
                color = HoldingsColors.TextMuted,
            )
            Text(
                "Verifica a ligação ao backend nas definições de rede.",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted,
            )
        }
    }
}
