package com.example.pluginhost

import android.content.Context
import android.os.Build
import android.text.InputFilter
import android.text.InputType
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

@Composable
internal fun DesktopConversations(onClose: () -> Unit) {
    val context = LocalContext.current
    val session = remember { DesktopConversationSession.forApp(context) }
    var settings by rememberSaveable { mutableStateOf(false) }
    var newProject by remember { mutableStateOf(false) }
    val project = session.project
    val goBack = {
        when {
            settings -> settings = false
            session.project != null -> session.closeProject()
            else -> onClose()
        }
    }
    BackHandler(onBack = goBack)
    LaunchedEffect(session) { session.start() }
    Column(
        modifier = Modifier.fillMaxSize().safeDrawingPadding().imePadding().padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            TextButton(onClick = goBack) {
                Text(if (settings || project != null) "返回会话" else "返回插件列表")
            }
            if (!settings) TextButton(onClick = { settings = true }) { Text("连接电脑") }
        }
        if (settings) {
            DesktopPairingForm(session)
        } else {
            Text(connectionText(session), style = MaterialTheme.typography.bodyMedium)
            session.notice?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (project == null) {
                Text("项目会话", style = MaterialTheme.typography.headlineSmall)
                Button(enabled = !session.busy && session.link != null, onClick = { newProject = true }) {
                    Text("新建项目")
                }
                if (session.link == null) Text("请先连接电脑，再创建项目。")
                LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(session.projects, key = { it.id }) { item ->
                        Card(
                            modifier = Modifier.fillMaxWidth(),
                            enabled = !session.busy,
                            onClick = { session.openProject(item) },
                        ) {
                            Column(modifier = Modifier.padding(12.dp)) {
                                Text(item.name, style = MaterialTheme.typography.titleMedium)
                                Text(item.id, style = MaterialTheme.typography.bodySmall)
                                if (item.fingerprint != session.link?.endpoint?.fingerprint) {
                                    Text("此项目关联另一台电脑")
                                }
                            }
                        }
                    }
                }
            } else {
                DesktopProjectMessages(session, project)
            }
        }
    }
    if (newProject) {
        var name by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { newProject = false },
            title = { Text("新建项目") },
            text = {
                OutlinedTextField(
                    value = name,
                    onValueChange = { if (it.length <= 64) name = it },
                    label = { Text("项目名称") },
                    singleLine = true,
                )
            },
            confirmButton = {
                Button(enabled = name.isNotBlank() && !session.busy, onClick = {
                    session.createProject(name, onSaved = { newProject = false })
                }) { Text("创建") }
            },
            dismissButton = { TextButton(onClick = { newProject = false }) { Text("取消") } },
        )
    }
}

@Composable
private fun DesktopProjectMessages(session: DesktopConversationSession, project: DesktopProject) {
    var body by remember(project.id) { mutableStateOf("") }
    val matchesLink = session.link?.endpoint?.fingerprint == project.fingerprint
    val canRetry = matchesLink && session.link?.blockedReason == null &&
        session.messages.any { it.delivery == Delivery.QUEUED || it.delivery == Delivery.RETRYABLE }
    Column(modifier = Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(project.name, style = MaterialTheme.typography.headlineSmall)
        SelectionContainer { Text(project.id, style = MaterialTheme.typography.bodySmall) }
        if (!matchesLink) Text("此项目关联另一台电脑，重新连接该电脑后可发送。")
        Text("电脑回执仅确认消息已保存。", style = MaterialTheme.typography.bodySmall)
        LazyColumn(
            modifier = Modifier.fillMaxWidth().weight(1f),
            reverseLayout = true,
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(session.messages.asReversed(), key = { it.id }) { message ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(message.body)
                        Text(
                            if (session.sendingId == message.id) "发送中" else when (message.delivery) {
                                Delivery.QUEUED -> "本机排队"
                                Delivery.RETRYABLE -> "发送失败，可重试"
                                Delivery.RECEIVED -> "电脑已接收"
                                Delivery.REJECTED -> "发送失败"
                            },
                            style = MaterialTheme.typography.labelLarge,
                        )
                        message.detail?.let { Text(desktopFailureText(it), style = MaterialTheme.typography.bodySmall) }
                        SelectionContainer {
                            Column {
                                Text("消息 ID ${message.id}", style = MaterialTheme.typography.bodySmall)
                                message.receipt?.let {
                                    Text("回执 ID ${it.id}", style = MaterialTheme.typography.bodySmall)
                                    Text("电脑接收时间 ${it.receivedAt}", style = MaterialTheme.typography.bodySmall)
                                }
                            }
                        }
                    }
                }
            }
        }
        OutlinedTextField(
            value = body,
            onValueChange = { if (it.codePointCount(0, it.length) <= MessageProtocol.MAX_BODY_CHARS) body = it },
            modifier = Modifier.fillMaxWidth(),
            label = { Text("输入插件需求") },
            maxLines = 4,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            val focus = LocalFocusManager.current
            Button(enabled = !session.busy && body.isNotBlank(), onClick = {
                val submitted = body
                focus.clearFocus()
                session.enqueue(submitted, onSaved = { if (body == submitted) body = "" })
            }) { Text("发送") }
            if (canRetry) TextButton(enabled = !session.busy, onClick = session::retry) { Text("重试待发送消息") }
        }
    }
}

@Composable
private fun DesktopPairingForm(session: DesktopConversationSession) {
    val context = LocalContext.current
    val endpoint = session.link?.endpoint
    var address by remember(endpoint) { mutableStateOf(endpoint?.address.orEmpty()) }
    var fingerprint by remember(endpoint) { mutableStateOf(endpoint?.fingerprint.orEmpty()) }
    val codeInput = remember(context) { pairingCodeInput(context) }
    val focus = LocalFocusManager.current
    DisposableEffect(context) {
        val window = (context as? ComponentActivity)?.window
        val alreadySecure = window != null && window.attributes.flags and WindowManager.LayoutParams.FLAG_SECURE != 0
        if (!alreadySecure) window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        onDispose {
            codeInput.text.clear()
            if (!alreadySecure) window?.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        }
    }
    Column(
        modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("电脑配对", style = MaterialTheme.typography.headlineSmall)
        Text(connectionText(session))
        Text("在电脑上启动接收服务，核对地址和证书指纹，再输入电脑显示的配对码。")
        session.device?.let {
            SelectionContainer { Text("本机设备 ID ${it.id}", style = MaterialTheme.typography.bodySmall) }
        }
        OutlinedTextField(
            value = address,
            onValueChange = { if (it.length <= 64) address = it },
            modifier = Modifier.fillMaxWidth(),
            label = { Text("电脑 HTTPS 地址") },
            singleLine = true,
            enabled = !session.busy,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
        )
        OutlinedTextField(
            value = fingerprint,
            onValueChange = { if (it.length <= 128) fingerprint = it },
            modifier = Modifier.fillMaxWidth(),
            label = { Text("证书 SHA-256 指纹") },
            maxLines = 3,
            enabled = !session.busy,
        )
        AndroidView(
            factory = { codeInput },
            modifier = Modifier.fillMaxWidth(),
            update = { it.isEnabled = !session.busy },
        )
        Button(enabled = !session.busy && session.device != null, onClick = {
            val code = codeInput.text.toString()
            codeInput.text.clear()
            codeInput.clearFocus()
            focus.clearFocus()
            session.pair(address, fingerprint, code)
        }) { Text("申请配对") }
        if (session.pairing) Text("正在连接，提交后请在电脑上核对设备 ID 并批准。")
        session.notice?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    }
}

private fun pairingCodeInput(context: Context) = EditText(context).apply {
    // Keep the secret in a native password field, outside snapshot and saved instance state.
    hint = "六位配对码"
    inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
    filters = arrayOf(InputFilter.LengthFilter(6))
    setSingleLine()
    isSaveEnabled = false
    isSaveFromParentEnabled = false
    isLongClickable = false
    if (Build.VERSION.SDK_INT >= 26) {
        importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
        imeOptions = EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING
    }
}

private fun connectionText(session: DesktopConversationSession): String {
    val link = session.link ?: return "尚未配对"
    val status = link.blockedReason ?: session.connection
    return when (status) {
        "CONNECTED" -> "上次连接成功 · ${link.endpoint.address}"
        null -> "已配对，等待连接 · ${link.endpoint.address}"
        else -> desktopFailureText(status)
    }
}
