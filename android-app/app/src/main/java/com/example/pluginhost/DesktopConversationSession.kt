package com.example.pluginhost

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Process-scoped to serialize queue work across Activity recreation. */
internal class DesktopConversationSession private constructor(context: Context) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val store = DesktopConversationStore(context)
    private val pairingStore = DesktopPairingStore(context)
    private val client = DesktopMessageClient()
    private var started = false

    var projects by mutableStateOf<List<DesktopProject>>(emptyList())
        private set
    var project by mutableStateOf<DesktopProject?>(null)
        private set
    var messages by mutableStateOf<List<QueuedMessage>>(emptyList())
        private set
    var link by mutableStateOf<DesktopLink?>(null)
        private set
    var device by mutableStateOf<DesktopDevice?>(null)
        private set
    var busy by mutableStateOf(false)
        private set
    var pairing by mutableStateOf(false)
        private set
    var sendingId by mutableStateOf<String?>(null)
        private set
    var connection by mutableStateOf<String?>(null)
        private set
    var notice by mutableStateOf<String?>(null)
        private set

    fun start() {
        if (started) return
        started = true
        work {
            projects = io { store.projects() }
            device = io { pairingStore.device() }
            link = io { pairingStore.read()?.link }
            drain(includeRetries = false)
        }
    }

    fun pair(address: String, fingerprint: String, code: String) = work {
        val identity = device ?: return@work
        val endpoint = DesktopEndpoint.parse(address, fingerprint)
        if (!code.matches(Regex("[0-9]{6}"))) throw DesktopFailure("DESKTOP_CODE")
        pairing = true
        try {
            val newLink = DesktopLink(endpoint)
            io {
                val credential = client.pair(endpoint, identity, code)
                pairingStore.save(DesktopPairing(newLink, credential))
            }
            link = newLink
            connection = "CONNECTED"
        } finally {
            pairing = false
        }
        drain(includeRetries = true)
    }

    fun openProject(selected: DesktopProject) = work {
        messages = io { store.messages(selected.id) }
        project = selected
    }

    fun closeProject() {
        project = null
        messages = emptyList()
    }

    fun createProject(name: String, onSaved: () -> Unit) = work {
        val endpoint = link?.endpoint ?: return@work
        val title = name.trim()
        if (title.isEmpty() || title.length > 64) throw DesktopFailure("PROJECT_NAME")
        val created = io { store.createProject(title, endpoint.fingerprint) }
        projects = listOf(created) + projects
        messages = emptyList()
        project = created
        onSaved()
    }

    fun enqueue(body: String, onSaved: () -> Unit) = work {
        val selected = project ?: return@work
        if (body.isBlank() || body.codePointCount(0, body.length) > MessageProtocol.MAX_BODY_CHARS ||
            !Charsets.UTF_8.newEncoder().canEncode(body)
        ) throw DesktopFailure("MESSAGE_BODY")
        io { store.enqueue(selected.id, body) }
        onSaved()
        refreshMessages()
        drain(includeRetries = false)
    }

    fun retry() = work {
        if (!drain(includeRetries = true)) notice = "当前没有可重试的消息，请核对配对状态或稍后再试。"
    }

    private suspend fun drain(includeRetries: Boolean): Boolean {
        val identity = device ?: return false
        val current = link ?: return false
        if (current.blockedReason != null) return false
        var attempted = false
        while (true) {
            val message = io { store.next(current.endpoint.fingerprint, includeRetries) } ?: return attempted
            attempted = true
            sendingId = message.id
            try {
                val receipt = io {
                    if (System.currentTimeMillis() - message.createdAt > MESSAGE_RETRY_WINDOW_MS) {
                        throw DesktopFailure("RETRY_WINDOW_EXPIRED")
                    }
                    val credential = pairingStore.read()?.credential ?: throw DesktopFailure("PAIRING_REQUIRED")
                    client.send(current.endpoint, identity, credential, message)
                }
                io { store.received(message.id, receipt) }
                connection = "CONNECTED"
            } catch (failure: DesktopFailure) {
                val pairingLost = failure.code in setOf("DEVICE_REVOKED", "AUTH_REQUIRED", "PAIRING_REQUIRED", "PAIRING_EXPIRED")
                val canRetry = failure.retryable || pairingLost ||
                    failure.code in setOf("SERVER_IDENTITY_MISMATCH", "TLS_UNAVAILABLE")
                if (pairingLost) {
                    val blocked = current.copy(blockedReason = failure.code)
                    io { pairingStore.save(DesktopPairing(blocked, null)) }
                    link = blocked
                }
                io { store.failed(message.id, failure, canRetry) }
                if (canRetry) {
                    connection = failure.code
                    return attempted
                }
            } finally {
                sendingId = null
                refreshMessages()
            }
        }
    }

    private suspend fun refreshMessages() {
        val selected = project ?: return
        val updated = io { store.messages(selected.id) }
        if (project == selected) messages = updated
    }

    private fun work(action: suspend () -> Unit) {
        if (busy) return
        busy = true
        notice = null
        scope.launch {
            try {
                action()
            } catch (failure: DesktopFailure) {
                notice = desktopFailureText(failure.code)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                // Exception details can contain message or credential material.
                notice = "无法读取或保存会话或配对数据，请重试。"
            } finally {
                busy = false
            }
        }
    }

    private suspend fun <T> io(action: () -> T): T = withContext(Dispatchers.IO) { action() }

    companion object {
        private var singleton: DesktopConversationSession? = null

        fun forApp(context: Context): DesktopConversationSession =
            singleton ?: DesktopConversationSession(context.applicationContext).also { singleton = it }
    }
}

internal fun desktopFailureText(code: String) = when (code) {
    "DESKTOP_ADDRESS" -> "请输入 HTTPS 地址、局域网 IPv4 和端口。"
    "DESKTOP_FINGERPRINT" -> "请输入电脑显示的 64 位证书指纹。"
    "DESKTOP_CODE" -> "请输入六位配对码。"
    "PAIRING_REJECTED" -> "电脑拒绝了配对申请。"
    "PAIRING_EXPIRED" -> "配对申请或凭据已过期。"
    "PAIRING_RATE_LIMITED" -> "配对请求过多，请一分钟后重试。"
    "PAIRING_REQUIRED" -> "配对码无效或需要重新配对。"
    "AUTH_REQUIRED" -> "电脑已拒绝配对凭据，请重新配对。"
    "DEVICE_REVOKED" -> "配对已撤销，请重新配对。"
    "SERVER_IDENTITY_MISMATCH" -> "电脑证书指纹不匹配，请在电脑上重新核对。"
    "TLS_UNAVAILABLE" -> "当前 Android 系统不支持所需的 TLS 1.3。"
    "DESKTOP_TLS" -> "无法建立 TLS 1.3 连接，请核对电脑服务和证书。"
    "DESKTOP_UNREACHABLE" -> "无法连接电脑，请检查网络和接收服务。"
    "DESKTOP_TIMEOUT" -> "连接或等待电脑响应超时。"
    "DESKTOP_RESPONSE" -> "电脑响应无法核验。"
    "RETRY_WINDOW_EXPIRED" -> "已超过 24 小时重试期限。"
    "MESSAGE_ID_CONFLICT", "MESSAGE_SEQUENCE_CONFLICT" -> "电脑已保存同一消息 ID 或序号的其他内容。"
    "PAYLOAD_TOO_LARGE", "INVALID_REQUEST" -> "电脑拒绝了消息大小或格式。"
    "RATE_LIMITED" -> "发送过于频繁，请稍后重试。"
    "INTERNAL_ERROR" -> "电脑接收服务暂时无法保存消息。"
    "PROJECT_NAME" -> "请输入 1 至 64 个字符的项目名称。"
    "MESSAGE_BODY" -> "请输入消息，最多 12,000 个字符。"
    else -> "电脑拒绝了请求。"
}
