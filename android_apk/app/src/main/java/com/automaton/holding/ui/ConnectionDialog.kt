package com.automaton.holding.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.automaton.holding.ui.theme.HoldingsColors

/**
 * Ecrã de ligação ao backend.
 *
 * Existe porque o endereço vinha fixo do build e, num telemóvel, apontava para
 * o emulador. Aqui o utilizador escreve o endereço que tem — o IP do
 * computador na Wi-Fi, ou um domínio HTTPS — testa, e só se responder é que
 * fica guardado.
 *
 * O botão «Testar» é o mesmo código que valida, por isso não há forma de
 * guardar um endereço que não funcione.
 */
@Composable
fun ConnectionDialog(
    endpointAtual: String,
    tokenAtual: String,
    ligado: Boolean?,
    testando: Boolean,
    diagnostico: String?,
    onTestar: (String, String) -> Unit,
    onFechar: () -> Unit,
) {
    var url by remember { mutableStateOf(endpointAtual) }
    var token by remember { mutableStateOf(tokenAtual) }

    AlertDialog(
        onDismissRequest = onFechar,
        containerColor = HoldingsColors.Surface,
        titleContentColor = HoldingsColors.TextPrimary,
        textContentColor = HoldingsColors.TextMuted,
        title = { Text("Ligar ao backend") },
        text = {
            Column {
                Text(
                    "Endereço onde o backend está a correr. No computador: " +
                        "`hostname -I` mostra o IP da rede Wi-Fi.",
                    style = MaterialTheme.typography.bodySmall,
                    color = HoldingsColors.TextMuted,
                )
                Spacer(Modifier.height(14.dp))

                OutlinedTextField(
                    value = url,
                    onValueChange = { url = it },
                    label = { Text("Endereço") },
                    placeholder = { Text("192.168.1.42:8000") },
                    singleLine = true,
                    enabled = !testando,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    value = token,
                    onValueChange = { token = it },
                    label = { Text("Token (opcional para leitura)") },
                    placeholder = { Text("X-Automaton-Token") },
                    singleLine = true,
                    enabled = !testando,
                    modifier = Modifier.fillMaxWidth(),
                )

                // ---- resultado do teste ----
                if (testando) {
                    Spacer(Modifier.height(14.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(16.dp),
                            color = HoldingsColors.Accent,
                            strokeWidth = 2.dp,
                        )
                        Spacer(Modifier.width(10.dp))
                        Text("a testar…", style = MaterialTheme.typography.bodySmall)
                    }
                }

                diagnostico?.let { texto ->
                    if (!testando) {
                        Spacer(Modifier.height(14.dp))
                        val cor = if (ligado == true) HoldingsColors.Accent
                        else HoldingsColors.Danger
                        Text(
                            (if (ligado == true) "✓ " else "✕ ") + texto,
                            style = MaterialTheme.typography.bodySmall,
                            color = cor,
                            fontFamily = FontFamily.Monospace,
                            fontSize = 12.sp,
                        )
                    }
                }

                if (ligado != true && !testando) {
                    Spacer(Modifier.height(14.dp))
                    Text(
                        "Dicas:\n" +
                            "• mesma Wi-Fi do computador, sem isolamento de clientes;\n" +
                            "• no PC: uvicorn --host 0.0.0.0 --port 8000;\n" +
                            "• HTTP em claro só funciona na rede local — na internet " +
                            "tem de ser https://.",
                        style = MaterialTheme.typography.bodySmall,
                        color = HoldingsColors.TextMuted,
                        fontSize = 11.sp,
                    )
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { onTestar(url, token) },
                enabled = !testando,
                colors = ButtonDefaults.buttonColors(
                    containerColor = HoldingsColors.Accent,
                    contentColor = HoldingsColors.Background,
                ),
            ) {
                Text(
                    if (ligado == true) "Ligado" else "Testar e ligar",
                    fontWeight = FontWeight.SemiBold,
                )
            }
        },
        dismissButton = {
            Row(horizontalArrangement = Arrangement.End) {
                if (ligado == true) {
                    TextButton(onClick = onFechar) {
                        Text("Fechar", color = HoldingsColors.Accent)
                    }
                } else {
                    TextButton(onClick = onFechar) {
                        Text("Cancelar", color = HoldingsColors.TextMuted)
                    }
                }
            }
        },
    )
}
