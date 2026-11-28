package com.automaton.holding.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/** Paleta da holding: fundo profundo, ciano para acção, vermelho para perigo. */
object HoldingsColors {
    val Background = Color(0xFF0B0F17)
    val Surface = Color(0xFF131A26)
    val SurfaceVariant = Color(0xFF1B2432)
    val Accent = Color(0xFF22D3EE)
    val AccentDark = Color(0xFF0891B2)
    val Primary = Color(0xFF3B82F6)
    val Danger = Color(0xFFEF4444)
    val DangerDark = Color(0xFFB91C1C)
    val Success = Color(0xFF22C55E)
    val Warning = Color(0xFFF59E0B)
    val TextPrimary = Color(0xFFE8EEF7)
    val TextMuted = Color(0xFF94A3B8)
    val Border = Color(0xFF1F2A3A)
    val Tax = Color(0xFFA78BFA)
}

private val HoldingColorScheme = darkColorScheme(
    primary = HoldingsColors.Accent,
    onPrimary = Color(0xFF04121C),
    primaryContainer = HoldingsColors.AccentDark,
    onPrimaryContainer = HoldingsColors.TextPrimary,
    secondary = HoldingsColors.Primary,
    onSecondary = Color.White,
    tertiary = HoldingsColors.Success,
    background = HoldingsColors.Background,
    onBackground = HoldingsColors.TextPrimary,
    surface = HoldingsColors.Surface,
    onSurface = HoldingsColors.TextPrimary,
    surfaceVariant = HoldingsColors.SurfaceVariant,
    onSurfaceVariant = HoldingsColors.TextMuted,
    error = HoldingsColors.Danger,
    onError = Color.White,
    outline = HoldingsColors.Border,
)

private val HoldingTypography = Typography(
    headlineLarge = TextStyle(
        fontFamily = FontFamily.Default,
        fontWeight = FontWeight.Bold,
        fontSize = 30.sp,
        lineHeight = 36.sp,
    ),
    headlineMedium = TextStyle(
        fontWeight = FontWeight.Bold,
        fontSize = 24.sp,
        lineHeight = 30.sp,
    ),
    titleLarge = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 19.sp),
    titleMedium = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 16.sp),
    bodyLarge = TextStyle(fontSize = 15.sp, lineHeight = 22.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    labelLarge = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 13.sp),
    labelSmall = TextStyle(fontSize = 11.sp, letterSpacing = 0.6.sp),
)

/** Estilo monoespaçado para o terminal da LiveOps. */
val TerminalTextStyle = TextStyle(
    fontFamily = FontFamily.Monospace,
    fontSize = 12.sp,
    lineHeight = 17.sp,
    color = HoldingsColors.TextPrimary,
)

@Composable
fun AutomatonHoldingTheme(
    @Suppress("UNUSED_PARAMETER") darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    // O APK é sempre escuro: é um centro de comando, não uma app de consumo.
    MaterialTheme(
        colorScheme = HoldingColorScheme,
        typography = HoldingTypography,
        content = content,
    )
}
