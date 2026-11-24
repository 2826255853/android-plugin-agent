package com.example.pluginhost

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
internal fun WorkshopPlugins(
    installed: List<LocalPluginStore.Installed>,
    status: String,
    busy: Boolean,
    onBack: () -> Unit,
    onImport: () -> Unit,
    onOpen: (LocalPluginStore.Installed) -> Unit,
    onRemove: (LocalPluginStore.Installed) -> Unit,
    onRollback: (LocalPluginStore.Installed) -> Unit,
    onDiagnostics: () -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    Column(Modifier.fillMaxSize().background(colors.background).safeDrawingPadding()) {
        WorkshopTopBar("我的插件", WorkshopGlyph.Back, "返回会话", onBack) {
            Button(onClick = onImport, enabled = !busy) { Text("导入 ZIP") }
        }
        LazyColumn(
            modifier = Modifier.weight(1f).fillMaxWidth(),
            contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 12.dp, bottom = 12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            if (status.isNotEmpty()) item {
                Surface(color = colors.secondaryContainer, shape = RoundedCornerShape(14.dp)) {
                    Text(status, Modifier.fillMaxWidth().padding(14.dp), color = colors.onSecondaryContainer)
                }
            }
            if (installed.isEmpty()) item {
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    color = colors.surface,
                    shape = RoundedCornerShape(22.dp),
                    border = BorderStroke(1.dp, colors.outlineVariant),
                ) {
                    Column(
                        Modifier.padding(horizontal = 20.dp, vertical = 34.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        PluginMark()
                        Text("暂无已安装插件", style = MaterialTheme.typography.titleLarge)
                        Text("导入电脑上准备好的 ZIP 插件包。", color = colors.onSurfaceVariant)
                    }
                }
            }
            items(installed, key = { it.id }) { plugin ->
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    color = colors.surface,
                    shape = RoundedCornerShape(20.dp),
                    border = BorderStroke(1.dp, colors.outlineVariant),
                ) {
                    Column(Modifier.padding(start = 14.dp, end = 14.dp, top = 14.dp, bottom = 5.dp)) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(12.dp),
                        ) {
                            PluginMark()
                            Column(Modifier.weight(1f)) {
                                Text("${plugin.name}  ${plugin.version}", style = MaterialTheme.typography.titleMedium)
                                Text(plugin.id, style = MaterialTheme.typography.bodySmall, color = colors.onSurfaceVariant)
                            }
                        }
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                            plugin.rollbackVersion?.let {
                                TextButton(enabled = !busy, onClick = { onRollback(plugin) }) { Text("回退到 $it") }
                            }
                            TextButton(enabled = !busy, onClick = { onRemove(plugin) }) { Text("移除") }
                            TextButton(enabled = !busy, onClick = { onOpen(plugin) }) { Text("打开") }
                        }
                    }
                }
            }
        }
        TextButton(onClick = onDiagnostics, modifier = Modifier.align(Alignment.End).padding(end = 16.dp, bottom = 6.dp)) {
            WorkshopIcon(WorkshopGlyph.Flask, colors.primary, Modifier.size(18.dp))
            Text("桥接实验", Modifier.padding(start = 6.dp))
        }
    }
}

@Composable
private fun PluginMark() {
    val colors = MaterialTheme.colorScheme
    Box(
        Modifier.size(48.dp).background(colors.primaryContainer, RoundedCornerShape(15.dp)),
        contentAlignment = Alignment.Center,
    ) {
        WorkshopIcon(WorkshopGlyph.Plugins, colors.onPrimaryContainer)
    }
}
