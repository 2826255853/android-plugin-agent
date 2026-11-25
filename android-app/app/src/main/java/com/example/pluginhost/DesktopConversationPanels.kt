package com.example.pluginhost

import android.content.Context
import android.os.Build
import android.text.InputFilter
import android.text.InputType
import android.text.method.PasswordTransformationMethod
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import org.json.JSONObject

@Composable
internal fun DesktopDialogueState(event: JSONObject, rejected: Boolean) {
    val type = event.getString("type")
    val state = when (type) {
        "desktop.status" -> when (event.getString("state")) {
            "queued" -> "规格会话排队中"
            "running" -> "规格会话处理中"
            "cancelled" -> if (rejected) "规格已拒绝" else "规格会话已取消"
            "confirmed" -> "规格已确认"
            else -> "规格会话已连接"
        }
        "desktop.question" -> "规格会话等待补充"
        "desktop.draft" -> "规格草案已收到"
        "desktop.review" -> "规格等待审阅"
        else -> "规格会话处理失败"
    }
    Text(state, style = MaterialTheme.typography.labelLarge)
    when (type) {
        "desktop.status" -> if (event.optString("state") == "confirmed") {
            Text("规格版本 ${event.getInt("specVersion")}")
            Text("审阅摘要 SHA-256 ${event.getString("specSha256")}", style = MaterialTheme.typography.bodySmall)
            Text("保存摘要 SHA-256 ${event.getString("savedSpecSha256")}", style = MaterialTheme.typography.bodySmall)
        }
        "desktop.question" -> Text(event.getString("question"))
        "desktop.draft", "desktop.review" -> {
            val draft = JSONObject(event.getString("specJson"))
            (draft.opt("title") as? String)?.let { Text(it, style = MaterialTheme.typography.titleMedium) }
            (draft.opt("goal") as? String)?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            if (type == "desktop.review") {
                Text(event.getString("summary"))
                UnsupportedItems(draft)
            }
        }
        "desktop.error" -> Text(desktopFailureText(event.getString("code")), style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
internal fun DesktopSpecReview(event: JSONObject, onDismiss: () -> Unit, onDecision: (String) -> Unit) {
    val draft = remember(event) { JSONObject(event.getString("specJson")) }
    val maxHeight = (LocalConfiguration.current.screenHeightDp * 0.68f).dp
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("审阅规格") },
        text = {
            Column(
                Modifier.fillMaxWidth().heightIn(max = maxHeight).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                SelectionContainer {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(draft.getString("title"), style = MaterialTheme.typography.titleMedium)
                        Text(draft.getString("goal"))
                        Text("规格版本 ${event.getInt("specVersion")}")
                        Text("审阅摘要 SHA-256 ${event.getString("specSha256")}")
                        Text("页面与用途", style = MaterialTheme.typography.labelLarge)
                        draft.getJSONArray("pages").let { pages ->
                            for (index in 0 until pages.length()) {
                                val page = pages.getJSONObject(index)
                                Text("${page.getString("name")} · ${page.getString("id")}\n${page.getString("purpose")}")
                            }
                        }
                        Text("数据集合与字段", style = MaterialTheme.typography.labelLarge)
                        Text(if (draft.getBoolean("storesData")) "保存数据" else "不保存数据")
                        draft.getJSONArray("collections").let { collections ->
                            for (index in 0 until collections.length()) {
                                val collection = collections.getJSONObject(index)
                                Text(collection.getString("id"), style = MaterialTheme.typography.bodyMedium)
                                val fields = collection.getJSONArray("fields")
                                for (fieldIndex in 0 until fields.length()) {
                                    val field = fields.getJSONObject(fieldIndex)
                                    val limits = listOf("maxLength", "minimum", "maximum")
                                        .filter(field::has).joinToString(", ") { key -> "$key ${field.get(key)}" }
                                    val required = if (field.getBoolean("required")) "必填" else "选填"
                                    Text("${field.getString("id")} · ${field.getString("type")} · $required" +
                                        if (limits.isEmpty()) "" else " · $limits")
                                }
                            }
                            if (collections.length() == 0) Text("未声明数据集合")
                        }
                        Text("操作流程", style = MaterialTheme.typography.labelLarge)
                        draft.getJSONArray("workflows").let { workflows ->
                            for (index in 0 until workflows.length()) {
                                val workflow = workflows.getJSONObject(index)
                                Text("${workflow.getString("id")} · ${workflow.getString("trigger")}")
                                val steps = workflow.getJSONArray("steps")
                                for (stepIndex in 0 until steps.length()) Text("${stepIndex + 1}. ${steps.getString(stepIndex)}")
                            }
                            if (workflows.length() == 0) Text("未声明操作流程")
                        }
                        Text("权限及理由", style = MaterialTheme.typography.labelLarge)
                        draft.getJSONArray("permissions").let { permissions ->
                            for (index in 0 until permissions.length()) {
                                val permission = permissions.getJSONObject(index)
                                Text("${permission.getString("capability")} · ${permission.getString("justification")}")
                            }
                            if (permissions.length() == 0) Text("未请求宿主能力")
                        }
                        UnsupportedItems(draft)
                        Text("验收条件", style = MaterialTheme.typography.labelLarge)
                        draft.getJSONArray("acceptanceCriteria").let { criteria ->
                            for (index in 0 until criteria.length()) {
                                val criterion = criteria.getJSONObject(index)
                                Text("${criterion.getString("id")} · ${criterion.getString("scenario")}\n${criterion.getString("expected")}")
                            }
                        }
                    }
                }
                Text("确认后，电脑将保存此版规格。")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { onDecision("phone.confirm") }) { Text("确认此规格") }
                    TextButton(onClick = { onDecision("phone.reject") }) { Text("拒绝此规格") }
                }
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("返回会话") } },
    )
}

@Composable
private fun UnsupportedItems(draft: JSONObject) {
    draft.optJSONArray("unsupportedItems")?.let { items ->
        for (index in 0 until items.length()) {
            val item = items.optJSONObject(index) ?: continue
            val request = item.opt("request") as? String ?: continue
            val reason = item.opt("reason") as? String ?: continue
            val alternative = item.opt("alternative") as? String ?: continue
            Text("不支持的请求 $request", style = MaterialTheme.typography.labelLarge)
            Text(reason, style = MaterialTheme.typography.bodySmall)
            Text("替代方案 $alternative", style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
internal fun DesktopPairingForm(session: DesktopConversationSession, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val endpoint = session.link?.endpoint
    var address by remember(endpoint) { mutableStateOf(endpoint?.address.orEmpty()) }
    var fingerprint by remember(endpoint) { mutableStateOf(endpoint?.fingerprint.orEmpty()) }
    val codeInput = remember(context) { pairingCodeInput(context) }
    val focus = LocalFocusManager.current
    val colors = MaterialTheme.colorScheme
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
        modifier = modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 22.dp, vertical = 12.dp),
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
        if (session.link?.blockedReason == null && endpoint != null) {
            TextButton(enabled = !session.busy && address.isNotBlank(), onClick = {
                focus.clearFocus()
                session.updateAddress(address)
            }) {
                Text(if (address.trim() == endpoint.address) "检查连接" else "验证并更新电脑地址")
            }
        }
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
            update = {
                it.isEnabled = !session.busy
                it.setTextColor(colors.onSurface.toArgb())
                it.setHintTextColor(colors.onSurfaceVariant.toArgb())
            },
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
    transformationMethod = PasswordTransformationMethod.getInstance()
    isSaveEnabled = false
    isSaveFromParentEnabled = false
    isLongClickable = false
    if (Build.VERSION.SDK_INT >= 26) {
        importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
        imeOptions = EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING
    }
}

internal fun connectionText(session: DesktopConversationSession): String {
    val link = session.link ?: return "尚未配对"
    val status = link.blockedReason ?: session.connection
    return when (status) {
        "CONNECTED" -> "上次连接成功 · ${link.endpoint.address}"
        null -> "已配对，等待连接 · ${link.endpoint.address}"
        else -> desktopFailureText(status)
    }
}
