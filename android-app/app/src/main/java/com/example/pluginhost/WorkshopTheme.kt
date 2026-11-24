package com.example.pluginhost

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

@Composable
internal fun WorkshopTheme(content: @Composable () -> Unit) {
    val defaults = Typography()
    val colors = if (isSystemInDarkTheme()) WorkshopDark else WorkshopLight
    MaterialTheme(
        colorScheme = colors,
        typography = defaults.copy(
            titleLarge = defaults.titleLarge.copy(fontWeight = FontWeight.Bold),
            titleMedium = defaults.titleMedium.copy(fontWeight = FontWeight.SemiBold),
            headlineMedium = defaults.headlineMedium.copy(fontWeight = FontWeight.Bold),
            headlineSmall = defaults.headlineSmall.copy(fontWeight = FontWeight.Bold),
        ),
    ) {
        CompositionLocalProvider(LocalContentColor provides colors.onBackground, content = content)
    }
}

private val WorkshopLight = lightColorScheme(
    primary = Color(0xFF624AE5),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFEDE8FF),
    onPrimaryContainer = Color(0xFF3D29A9),
    secondaryContainer = Color(0xFFF1EDFA),
    onSecondaryContainer = Color(0xFF4F4667),
    background = Color(0xFFF8F7FC),
    onBackground = Color(0xFF211C36),
    surface = Color.White,
    onSurface = Color(0xFF211C36),
    onSurfaceVariant = Color(0xFF625C74),
    surfaceContainerLow = Color(0xFFF4F1FA),
    surfaceContainerHigh = Color(0xFFEEEBF6),
    outline = Color(0xFF79718B),
    outlineVariant = Color(0xFFE5E0F0),
)

private val WorkshopDark = darkColorScheme(
    primary = Color(0xFFC6B8FF),
    onPrimary = Color(0xFF2E1A83),
    primaryContainer = Color(0xFF3E2B79),
    onPrimaryContainer = Color(0xFFE7DEFF),
    secondaryContainer = Color(0xFF302943),
    onSecondaryContainer = Color(0xFFE2D8F5),
    background = Color(0xFF151222),
    onBackground = Color(0xFFF4EEFF),
    surface = Color(0xFF211B30),
    onSurface = Color(0xFFF4EEFF),
    onSurfaceVariant = Color(0xFFC7BCD8),
    surfaceContainerLow = Color(0xFF211B30),
    surfaceContainerHigh = Color(0xFF302943),
    outline = Color(0xFF9589A9),
    outlineVariant = Color(0xFF3E3551),
)

internal enum class WorkshopGlyph {
    Menu, Back, Spark, Computer, Plus, ArrowRight, Send, Notes, Check, Wallet, Plugins, Chevron, Flask,
}

@Composable
internal fun WorkshopIconButton(
    icon: WorkshopGlyph,
    label: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    filled: Boolean = false,
) {
    val colors = MaterialTheme.colorScheme
    val container = when {
        !enabled && filled -> colors.surfaceContainerHigh
        filled -> colors.primary
        else -> colors.surface
    }
    val ink = when {
        !enabled -> colors.onSurfaceVariant
        filled -> colors.onPrimary
        else -> colors.onSurface
    }
    Surface(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.size(48.dp).semantics {
            contentDescription = label
            role = Role.Button
        },
        color = container,
        contentColor = ink,
        shape = RoundedCornerShape(16.dp),
        border = if (filled) null else BorderStroke(1.dp, colors.outlineVariant),
    ) {
        Box(contentAlignment = Alignment.Center) { WorkshopIcon(icon, ink) }
    }
}

@Composable
internal fun WorkshopTopBar(
    title: String,
    icon: WorkshopGlyph,
    label: String,
    onClick: () -> Unit,
    action: @Composable () -> Unit = {},
) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        WorkshopIconButton(icon, label, onClick)
        Text(
            title,
            modifier = Modifier.weight(1f),
            style = MaterialTheme.typography.titleLarge,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        action()
    }
}

@Composable
internal fun WorkshopIcon(icon: WorkshopGlyph, color: Color, modifier: Modifier = Modifier.size(22.dp)) {
    Canvas(modifier) {
        scale(size.width / 24, size.height / 24, Offset.Zero) {
            drawWorkshopIcon(icon, color)
        }
    }
}

private fun DrawScope.drawWorkshopIcon(icon: WorkshopGlyph, color: Color) {
    val ink = Stroke(width = 1.75f, cap = StrokeCap.Round, join = StrokeJoin.Round)
    fun line(x1: Float, y1: Float, x2: Float, y2: Float) =
        drawLine(color, Offset(x1, y1), Offset(x2, y2), ink.width, ink.cap)
    fun outline(left: Float, top: Float, width: Float, height: Float, radius: Float) =
        drawRoundRect(color, Offset(left, top), Size(width, height), CornerRadius(radius), style = ink)
    fun path(draw: Path.() -> Unit) = drawPath(Path().apply(draw), color, style = ink)
    when (icon) {
        WorkshopGlyph.Menu -> {
            line(4f, 8f, 20f, 8f)
            line(4f, 12f, 16f, 12f)
            line(4f, 16f, 20f, 16f)
        }
        WorkshopGlyph.Back -> {
            line(20f, 12f, 5f, 12f)
            path { moveTo(11f, 6f); lineTo(5f, 12f); lineTo(11f, 18f) }
        }
        WorkshopGlyph.Spark -> drawPath(Path().apply {
            moveTo(12f, 2f)
            quadraticTo(13.8f, 10.2f, 22f, 12f)
            quadraticTo(13.8f, 13.8f, 12f, 22f)
            quadraticTo(10.2f, 13.8f, 2f, 12f)
            quadraticTo(10.2f, 10.2f, 12f, 2f)
            close()
        }, color)
        WorkshopGlyph.Computer -> {
            outline(3f, 4f, 18f, 13f, 2f)
            line(12f, 17f, 12f, 21f)
            line(8f, 21f, 16f, 21f)
        }
        WorkshopGlyph.Plus -> {
            line(5f, 12f, 19f, 12f)
            line(12f, 5f, 12f, 19f)
        }
        WorkshopGlyph.ArrowRight -> {
            line(5f, 12f, 19f, 12f)
            path { moveTo(13f, 6f); lineTo(19f, 12f); lineTo(13f, 18f) }
        }
        WorkshopGlyph.Send -> {
            line(12f, 20f, 12f, 4f)
            path { moveTo(5f, 11f); lineTo(12f, 4f); lineTo(19f, 11f) }
        }
        WorkshopGlyph.Notes -> {
            outline(5f, 3f, 14f, 18f, 3f)
            line(9f, 9f, 15f, 9f)
            line(9f, 13f, 15f, 13f)
            line(9f, 17f, 13f, 17f)
        }
        WorkshopGlyph.Check -> {
            outline(4f, 4f, 16f, 16f, 4f)
            path { moveTo(8f, 12f); lineTo(11f, 15f); lineTo(16f, 9f) }
        }
        WorkshopGlyph.Wallet -> {
            outline(3f, 6f, 18f, 13f, 3f)
            line(4f, 6f, 16f, 3f)
            outline(14f, 10f, 7f, 5f, 2f)
            drawCircle(color, 0.85f, Offset(17f, 12.5f))
        }
        WorkshopGlyph.Plugins -> {
            for (x in listOf(4f, 13f)) {
                for (y in listOf(4f, 13f)) outline(x, y, 7f, 7f, 1.6f)
            }
        }
        WorkshopGlyph.Chevron -> path { moveTo(9f, 5f); lineTo(16f, 12f); lineTo(9f, 19f) }
        WorkshopGlyph.Flask -> {
            path {
                moveTo(9f, 3f); lineTo(9f, 9f); lineTo(4f, 18f)
                quadraticTo(3f, 21f, 6f, 21f)
                lineTo(18f, 21f); quadraticTo(21f, 21f, 20f, 18f)
                lineTo(15f, 9f); lineTo(15f, 3f)
            }
            line(8f, 3f, 16f, 3f)
            line(7f, 14f, 17f, 14f)
        }
    }
}
