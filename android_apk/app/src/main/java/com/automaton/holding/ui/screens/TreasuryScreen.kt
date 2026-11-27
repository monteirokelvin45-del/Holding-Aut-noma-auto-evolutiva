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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountBalance
import androidx.compose.material.icons.filled.ArrowOutward
import androidx.compose.material.icons.filled.Savings
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.automaton.holding.data.TreasuryResponse
import com.automaton.holding.ui.theme.HoldingsColors

/**
 * Aba 3 — Treasury: cofres das fases 1/2/3, saldos USD/MZN e saques.
 *
 * O fluxo de saque tem 2 passos: (1) reservar -> cria aprovação;
 * (2) aprovar com biometria na Mesa de Aprovações -> o CEO executa via
 * M-Pesa/e-Mola/PayPal/Wise. Nunca há envio sem aprovação biométrica.
 */
@Composable
fun TreasuryScreen(
    treasury: TreasuryResponse?,
    ownerPocketUsd: Double,
    onRequestPayout: (Double, String, (String) -> Unit) -> Unit,
    onOpenApprovals: () -> Unit,
    onRefreshQuote: () -> Unit,
    busyAction: String?,
) {
    val snapshot = treasury?.snapshot
    val channels = treasury?.channels
    val rate = channels?.fx?.usdMzn ?: 63.5
    val scroll = rememberScrollState()

    var amountText by remember { mutableStateOf("") }
    var channel by remember { mutableStateOf("mpesa") }
    var pendingApprovalId by remember { mutableStateOf<String?>(null) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(scroll)
            .padding(horizontal = 14.dp),
    ) {
        Spacer(Modifier.height(12.dp))

        // ------------------------- Cabeçalho da fase -------------------------
        Card(
            colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
            shape = RoundedCornerShape(16.dp),
            modifier = Modifier
                .fillMaxWidth()
                .border(1.dp, HoldingsColors.Border, RoundedCornerShape(16.dp)),
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        Icons.Filled.AccountBalance,
                        contentDescription = null,
                        tint = HoldingsColors.Accent,
                        modifier = Modifier.size(18.dp),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "FASE ${snapshot?.phase ?: 1} · " +
                            (snapshot?.phaseMeta?.name ?: "Lançamento").uppercase(),
                        style = MaterialTheme.typography.labelSmall,
                        color = HoldingsColors.Accent,
                        fontWeight = FontWeight.Bold,
                    )
                    Spacer(Modifier.weight(1f))
                    IconButton(
                        onClick = onRefreshQuote,
                        modifier = Modifier.size(28.dp),
                    ) {
                        Icon(
                            Icons.Filled.Sync,
                            contentDescription = "Actualizar",
                            tint = HoldingsColors.TextMuted,
                            modifier = Modifier.size(16.dp),
                        )
                    }
                }
                Spacer(Modifier.height(8.dp))
                Text(
                    "$${"%,.2f".format(snapshot?.totalAssetsUsd ?: 0.0)}",
                    style = MaterialTheme.typography.headlineLarge,
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    "≈ MZN ${"%,.2f".format((snapshot?.totalAssetsUsd ?: 0.0) * rate)}",
                    style = MaterialTheme.typography.labelSmall,
                    color = HoldingsColors.TextMuted,
                )
                Spacer(Modifier.height(12.dp))
                Text(
                    snapshot?.phaseMeta?.description ?: "",
                    style = MaterialTheme.typography.bodyMedium,
                    color = HoldingsColors.TextMuted,
                )
                Spacer(Modifier.height(12.dp))
                PhaseProgress(snapshot?.monthlyRevenueUsd ?: 0.0)
            }
        }

        Spacer(Modifier.height(12.dp))

        // ------------------------------ Cofres ------------------------------
        Text(
            "COFRES (USD / MZN)",
            style = MaterialTheme.typography.labelSmall,
            color = HoldingsColors.TextMuted,
        )
        Spacer(Modifier.height(8.dp))
        VaultRow(
            label = "Bolso do Presidente",
            usd = snapshot?.balancesUsd?.owner ?: 0.0,
            mzn = snapshot?.balancesMzn?.owner ?: 0.0,
            color = HoldingsColors.Success,
        )
        VaultRow(
            label = "Cofre de Poupança",
            usd = snapshot?.balancesUsd?.savings ?: 0.0,
            mzn = snapshot?.balancesMzn?.savings ?: 0.0,
            color = HoldingsColors.Primary,
        )
        VaultRow(
            label = "Fundo de Reinvestimento",
            usd = snapshot?.balancesUsd?.reinvestment ?: 0.0,
            mzn = snapshot?.balancesMzn?.reinvestment ?: 0.0,
            color = HoldingsColors.Accent,
        )
        VaultRow(
            label = "Reserva de Impostos (intocável)",
            usd = snapshot?.balancesUsd?.tax ?: 0.0,
            mzn = snapshot?.balancesMzn?.tax ?: 0.0,
            color = HoldingsColors.Tax,
        )

        Spacer(Modifier.height(16.dp))

        // ------------------------------ Saques ------------------------------
        Card(
            colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
            shape = RoundedCornerShape(16.dp),
            modifier = Modifier
                .fillMaxWidth()
                .border(1.dp, HoldingsColors.Border, RoundedCornerShape(16.dp)),
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        Icons.Filled.ArrowOutward,
                        contentDescription = null,
                        tint = HoldingsColors.Success,
                        modifier = Modifier.size(18.dp),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "Saque Transfronteiriço",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                    )
                }
                Spacer(Modifier.height(4.dp))
                Text(
                    "Disponível no bolso: ${"%.2f".format(ownerPocketUsd)} USD · " +
                        "1 USD ≈ ${"%.2f".format(rate)} MZN",
                    style = MaterialTheme.typography.labelSmall,
                    color = HoldingsColors.TextMuted,
                )
                Spacer(Modifier.height(12.dp))

                OutlinedTextField(
                    value = amountText,
                    onValueChange = { value ->
                        amountText = value.filter { it.isDigit() || it == '.' }
                    },
                    label = { Text("Montante em USD") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                if (amountText.toDoubleOrNull() != null && (amountText.toDoubleOrNull() ?: 0.0) > 0) {
                    Text(
                        "Receberás ≈ MZN ${"%,.2f".format((amountText.toDouble() * rate))}",
                        style = MaterialTheme.typography.labelSmall,
                        color = HoldingsColors.Accent,
                    )
                }

                Spacer(Modifier.height(12.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    ChannelChip("mpesa", "M-Pesa", channel, channels?.channels?.get("mpesa")?.available ?: false) {
                        channel = "mpesa"
                    }
                    ChannelChip("emola", "e-Mola", channel, channels?.channels?.get("emola")?.available ?: false) {
                        channel = "emola"
                    }
                    ChannelChip("wise", "Wise", channel, channels?.channels?.get("wise")?.available ?: false) {
                        channel = "wise"
                    }
                }
                Spacer(Modifier.height(6.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    ChannelChip("paypal", "PayPal", channel, channels?.channels?.get("paypal")?.available ?: false) {
                        channel = "paypal"
                    }
                }

                val selectedMode = channels?.channels?.get(channel)?.mode ?: "dry_run"
                Spacer(Modifier.height(10.dp))
                Text(
                    if (selectedMode == "live")
                        "Canal em modo LIVE — o dinheiro é movido após aprovação biométrica."
                    else "Canal em DRY-RUN: faltam credenciais; o pedido é auditado mas "
                        + "nenhum dinheiro é movido.",
                    style = MaterialTheme.typography.labelSmall,
                    color = if (selectedMode == "live") HoldingsColors.Success
                    else HoldingsColors.Warning,
                )

                Spacer(Modifier.height(14.dp))
                Button(
                    onClick = {
                        val amount = amountText.toDoubleOrNull() ?: 0.0
                        if (amount > 0.0) {
                            onRequestPayout(amount, channel) { approvalId ->
                                pendingApprovalId = approvalId
                                amountText = ""
                            }
                        }
                    },
                    enabled = (amountText.toDoubleOrNull() ?: 0.0) > 0 &&
                        busyAction == null,
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = HoldingsColors.Success,
                        contentColor = Color(0xFF04120A),
                    ),
                ) {
                    if (busyAction?.startsWith("payout") == true) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(14.dp),
                            strokeWidth = 2.dp,
                            color = Color(0xFF04120A),
                        )
                        Spacer(Modifier.width(8.dp))
                    }
                    Text("Pedir saque (requer aprovação)")
                }

                if (pendingApprovalId != null) {
                    Spacer(Modifier.height(10.dp))
                    Card(
                        colors = CardDefaults.cardColors(
                            containerColor = HoldingsColors.Accent.copy(alpha = 0.1f)
                        ),
                        shape = RoundedCornerShape(10.dp),
                    ) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Text(
                                "Pedido criado: ${pendingApprovalId}",
                                style = MaterialTheme.typography.labelSmall,
                                color = HoldingsColors.Accent,
                            )
                            Text(
                                "Aprova na Mesa de Aprovações com a tua biometria.",
                                style = MaterialTheme.typography.labelSmall,
                                color = HoldingsColors.TextMuted,
                            )
                            Spacer(Modifier.height(8.dp))
                            Button(
                                onClick = onOpenApprovals,
                                shape = RoundedCornerShape(10.dp),
                                colors = ButtonDefaults.buttonColors(
                                    containerColor = HoldingsColors.Accent,
                                    contentColor = Color(0xFF04121C),
                                ),
                            ) { Text("Ir para Aprovações") }
                        }
                    }
                }
            }
        }

        Spacer(Modifier.height(12.dp))
        Card(
            colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
            shape = RoundedCornerShape(16.dp),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        Icons.Filled.Savings,
                        contentDescription = null,
                        tint = HoldingsColors.Tax,
                        modifier = Modifier.size(18.dp),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "Provisão Fiscal",
                        style = MaterialTheme.typography.titleMedium,
                    )
                }
                Spacer(Modifier.height(6.dp))
                Text(
                    "5% de cada receita bruta é retido automaticamente e nunca é " +
                        "usado para despesas ou saques.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = HoldingsColors.TextMuted,
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    "Acumulado: $${"%.2f".format(snapshot?.balancesUsd?.tax ?: 0.0)} " +
                        "(≈ MZN ${"%.2f".format(snapshot?.balancesMzn?.tax ?: 0.0)})",
                    style = MaterialTheme.typography.titleMedium,
                    color = HoldingsColors.Tax,
                    fontWeight = FontWeight.Bold,
                )
            }
        }
        Spacer(Modifier.height(60.dp))
    }
}

@Composable
private fun PhaseProgress(monthlyRevenue: Double) {
    val phase2 = 1_500.0
    val phase3 = 15_000.0
    val (progress, label) = when {
        monthlyRevenue >= phase3 -> 1f to "Fase 3 · Colheita (50/30/20)"
        monthlyRevenue >= phase2 -> {
            val p = ((monthlyRevenue - phase2) / (phase3 - phase2)).toFloat()
            p to "Fase 2 · Consolidação → Fase 3 em " +
                "$${"%,.0f".format(phase3 - monthlyRevenue)}"
        }
        else -> {
            val p = (monthlyRevenue / phase2).toFloat()
            p to "Fase 1 · Lançamento → Fase 2 em " +
                "$${"%,.0f".format(phase2 - monthlyRevenue)}"
        }
    }
    Column {
        LinearProgressIndicator(
            progress = { progress.coerceIn(0f, 1f) },
            modifier = Modifier
                .fillMaxWidth()
                .height(6.dp)
                .clip(RoundedCornerShape(3.dp)),
            color = HoldingsColors.Accent,
            trackColor = HoldingsColors.SurfaceVariant,
        )
        Spacer(Modifier.height(6.dp))
        Text(
            label,
            style = MaterialTheme.typography.labelSmall,
            color = HoldingsColors.TextMuted,
        )
    }
}

@Composable
private fun VaultRow(label: String, usd: Double, mzn: Double, color: Color) {
    Card(
        colors = CardDefaults.cardColors(containerColor = HoldingsColors.Surface),
        shape = RoundedCornerShape(12.dp),
        modifier = Modifier
            .fillMaxWidth()
            .padding(bottom = 8.dp),
    ) {
        Row(
            modifier = Modifier.padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(8.dp)
                    .clip(RoundedCornerShape(4.dp))
                    .background(color),
            )
            Spacer(Modifier.width(10.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(label, style = MaterialTheme.typography.bodyMedium)
                Text(
                    "≈ MZN ${"%,.2f".format(mzn)}",
                    style = MaterialTheme.typography.labelSmall,
                    color = HoldingsColors.TextMuted,
                )
            }
            Text(
                "$${"%,.2f".format(usd)}",
                style = MaterialTheme.typography.titleMedium,
                color = color,
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun ChannelChip(
    id: String,
    label: String,
    selected: String,
    live: Boolean,
    onClick: () -> Unit,
) {
    AssistChip(
        onClick = onClick,
        label = {
            Text(
                if (live) label else "$label · dry",
                style = MaterialTheme.typography.labelSmall,
            )
        },
        colors = AssistChipDefaults.assistChipColors(
            containerColor = if (selected == id) HoldingsColors.Accent.copy(alpha = 0.18f)
            else HoldingsColors.SurfaceVariant,
            labelColor = if (selected == id) HoldingsColors.Accent
            else HoldingsColors.TextMuted,
        ),
        border = null,
    )
}
