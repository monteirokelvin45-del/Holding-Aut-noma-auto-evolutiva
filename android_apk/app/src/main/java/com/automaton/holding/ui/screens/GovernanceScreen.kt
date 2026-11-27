package com.automaton.holding.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountBalance
import androidx.compose.material.icons.filled.Gavel
import androidx.compose.material.icons.filled.Groups
import androidx.compose.material.icons.filled.Rule
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.automaton.holding.data.GovernanceResponse
import com.automaton.holding.data.SystemStatusResponse
import com.automaton.holding.data.ValidationReport
import com.automaton.holding.ui.theme.HoldingsColors

/**
 * Aba 4 — Governance: painel da Diretoria.
 *
 * Quatro blocos: RH de IA (ROI dos agentes), Contabilidade (impostos e fase),
 * Jurídico (licenças/ToS) e Juiz Eval (qualidade média e taxa de aprovação).
 */
@Composable
fun GovernanceScreen(
    governance: GovernanceResponse?,
    status: SystemStatusResponse?,
    validation: ValidationReport?,
    busy: Boolean,
) {
    val scroll = rememberScrollState()
    val hr = governance?.hr
    val accounting = governance?.accounting?.report
    val legal = governance?.legal
    val judge = governance?.judge

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(scroll)
            .padding(horizontal = 14.dp),
    ) {
        Spacer(Modifier.height(12.dp))
        Text(
            "DIRETORIA",
            style = MaterialTheme.typography.labelSmall,
            color = HoldingsColors.TextMuted,
        )
        Spacer(Modifier.height(10.dp))

        // ------------------------------- RH -------------------------------
        GovernanceCard(
            icon = Icons.Filled.Groups,
            title = "RH de IA",
            accent = HoldingsColors.Accent,
        ) {
            Row(modifier = Modifier.fillMaxWidth()) {
                StatBlock(
                    label = "Agentes",
                    value = (hr?.summary?.totalAgents ?: 0).toString(),
                    modifier = Modifier.weight(1f),
                )
                StatBlock(
                    label = "A rever",
                    value = (hr?.summary?.needingAttention ?: 0).toString(),
                    accent = if ((hr?.summary?.needingAttention ?: 0) > 0)
                        HoldingsColors.Warning else HoldingsColors.Success,
                    modifier = Modifier.weight(1f),
                )
            }
            Spacer(Modifier.height(10.dp))
            hr?.agents?.take(6)?.forEach { agent ->
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        agent.id.replace("_", " "),
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.weight(1f),
                    )
                    Text(
                        "v${agent.promptVersion}",
                        style = MaterialTheme.typography.labelSmall,
                        color = HoldingsColors.TextMuted,
                    )
                    Spacer(Modifier.width(10.dp))
                    Text(
                        agent.successRate?.let { "${(it * 100).toInt()}%" } ?: "—",
                        style = MaterialTheme.typography.labelSmall,
                        color = when {
                            agent.successRate == null -> HoldingsColors.TextMuted
                            agent.successRate >= 0.8 -> HoldingsColors.Success
                            agent.successRate >= 0.6 -> HoldingsColors.Warning
                            else -> HoldingsColors.Danger
                        },
                        fontWeight = FontWeight.Bold,
                    )
                }
            }
            if (hr?.agents.isNullOrEmpty()) {
                Text(
                    "Sem execuções registadas ainda.",
                    style = MaterialTheme.typography.labelSmall,
                    color = HoldingsColors.TextMuted,
                )
            }
        }

        // -------------------------- Contabilidade --------------------------
        GovernanceCard(
            icon = Icons.Filled.AccountBalance,
            title = "Contabilidade & Impostos",
            accent = HoldingsColors.Primary,
        ) {
            Row(modifier = Modifier.fillMaxWidth()) {
                StatBlock(
                    label = "Receita 30d",
                    value = "$${"%,.2f".format(accounting?.monthlyRevenueUsd ?: 0.0)}",
                    modifier = Modifier.weight(1f),
                )
                StatBlock(
                    label = "Impostos retidos",
                    value = "$${"%.2f".format(accounting?.taxReserveUsd ?: 0.0)}",
                    accent = HoldingsColors.Tax,
                    modifier = Modifier.weight(1f),
                )
                StatBlock(
                    label = "Fase",
                    value = (accounting?.phase ?: 1).toString(),
                    modifier = Modifier.weight(1f),
                )
            }
            Spacer(Modifier.height(8.dp))
            Text(
                "Regra de abate: 7 dias sem tracção ou 21 dias sem o primeiro " +
                    "dólar => proposta de abate ao Presidente.",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted,
            )
        }

        // ------------------------------ Juiz ------------------------------
        GovernanceCard(
            icon = Icons.Filled.Rule,
            title = "Juiz Eval (qualidade)",
            accent = HoldingsColors.Success,
        ) {
            Row(modifier = Modifier.fillMaxWidth()) {
                StatBlock(
                    label = "Avaliações",
                    value = (judge?.totalEvaluations ?: 0).toString(),
                    modifier = Modifier.weight(1f),
                )
                StatBlock(
                    label = "Aprovadas",
                    value = (judge?.passed ?: 0).toString(),
                    accent = HoldingsColors.Success,
                    modifier = Modifier.weight(1f),
                )
                StatBlock(
                    label = "Reprovadas",
                    value = (judge?.failed ?: 0).toString(),
                    accent = HoldingsColors.Danger,
                    modifier = Modifier.weight(1f),
                )
            }
            Spacer(Modifier.height(10.dp))
            Text(
                "Nota média: ${judge?.averageScore?.let { "%.1f".format(it) } ?: "—"}/100",
                style = MaterialTheme.typography.bodyMedium,
            )
            Spacer(Modifier.height(6.dp))
            LinearProgressIndicator(
                progress = { ((judge?.averageScore ?: 0.0) / 100.0).toFloat() },
                modifier = Modifier
                    .fillMaxWidth()
                    .height(6.dp)
                    .clip(RoundedCornerShape(3.dp)),
                color = when {
                    (judge?.averageScore ?: 0.0) >= 85 -> HoldingsColors.Success
                    (judge?.averageScore ?: 0.0) >= 70 -> HoldingsColors.Warning
                    else -> HoldingsColors.Danger
                },
                trackColor = HoldingsColors.SurfaceVariant,
            )
            Spacer(Modifier.height(6.dp))
            Text(
                "Limiar obrigatório: 85/100. Abaixo disto, refazer.",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted,
            )
        }

        // ----------------------------- Jurídico ----------------------------
        GovernanceCard(
            icon = Icons.Filled.Gavel,
            title = "Jurídico & Compliance",
            accent = HoldingsColors.Warning,
        ) {
            Text(
                "Licenças permitidas: MIT · Apache-2.0 · BSD · LGPL",
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(
                "Proibido: CC-BY-NC (ChatTTS, F5-TTS) e código inacabado.",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.Danger,
            )
            Spacer(Modifier.height(10.dp))
            val tools = legal?.toolsAvailable ?: emptyMap()
            val available = tools.count { it.value }
            Text(
                "Ferramentas detectadas: $available/${tools.size.coerceAtLeast(1)}",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted,
            )
            tools.entries.take(8).forEach { (name, present) ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        modifier = Modifier
                            .size(6.dp)
                            .clip(RoundedCornerShape(3.dp))
                            .background(
                                if (present) HoldingsColors.Success
                                else HoldingsColors.TextMuted.copy(alpha = 0.4f)
                            ),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        name.replace("_", " "),
                        style = MaterialTheme.typography.labelSmall,
                        color = if (present) HoldingsColors.TextPrimary
                        else HoldingsColors.TextMuted,
                    )
                }
            }
        }

        // ---------------------------- Validação ----------------------------
        GovernanceCard(
            icon = Icons.Filled.Rule,
            title = "Validação de Demanda",
            accent = HoldingsColors.Accent,
        ) {
            val summary = validation?.report?.summary
            Row(modifier = Modifier.fillMaxWidth()) {
                StatBlock(
                    label = "Em curso",
                    value = (summary?.running ?: 0).toString(),
                    modifier = Modifier.weight(1f),
                )
                StatBlock(
                    label = "Aprovados",
                    value = (summary?.passed ?: 0).toString(),
                    accent = HoldingsColors.Success,
                    modifier = Modifier.weight(1f),
                )
                StatBlock(
                    label = "Reprovados",
                    value = (summary?.failed ?: 0).toString(),
                    accent = HoldingsColors.Danger,
                    modifier = Modifier.weight(1f),
                )
            }
            Spacer(Modifier.height(8.dp))
            Text(
                "Cliques totais: ${summary?.totalClicks ?: 0} · Produção " +
                    "desbloqueada para ${summary?.productionUnlocked ?: 0} negócio(s).",
                style = MaterialTheme.typography.labelSmall,
                color = HoldingsColors.TextMuted,
            )
        }

        Spacer(Modifier.height(60.dp))
    }
}

@Composable
private fun GovernanceCard(
    icon: ImageVector,
    title: String,
    accent: Color,
    content: @Composable () -> Unit,
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
        shape = RoundedCornerShape(16.dp),
        modifier = Modifier
            .fillMaxWidth()
            .padding(bottom = 12.dp)
            .border(1.dp, HoldingsColors.Border, RoundedCornerShape(16.dp)),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(icon, contentDescription = null, tint = accent,
                    modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(8.dp))
                Text(
                    title,
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                )
            }
            Spacer(Modifier.height(12.dp))
            content()
        }
    }
}

@Composable
private fun StatBlock(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    accent: Color = HoldingsColors.TextPrimary,
) {
    Column(modifier = modifier) {
        Text(
            label.uppercase(),
            style = MaterialTheme.typography.labelSmall,
            color = HoldingsColors.TextMuted,
        )
        Text(
            value,
            style = MaterialTheme.typography.titleMedium,
            color = accent,
            fontWeight = FontWeight.Bold,
        )
    }
}
