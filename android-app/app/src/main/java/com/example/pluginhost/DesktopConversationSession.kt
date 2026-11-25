package com.example.pluginhost

import android.content.Context
import android.net.ConnectivityManager
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/** Process-scoped to serialize queue work across Activity recreation. */
internal class DesktopConversationSession private constructor(context: Context) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val store = DesktopConversationStore(context)
    private val pairingStore = DesktopPairingStore(context)
    private val client = DesktopMessageClient()
    private val connectivity = context.getSystemService(ConnectivityManager::class.java)
    private var started = false

    var projects by mutableStateOf<List<DesktopProject>>(emptyList())
        private set
    var project by mutableStateOf<DesktopProject?>(null)
        private set
    var messages by mutableStateOf<List<QueuedMessage>>(emptyList())
        private set
    var dialogueHeads by mutableStateOf<Map<String, JSONObject>>(emptyMap())
        private set
    var rejectedDialogues by mutableStateOf<Set<String>>(emptySet())
        private set
    var dialogueNotice by mutableStateOf<String?>(null)
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
        work {
            if (!started) {
                projects = io { store.projects() }
                device = io { pairingStore.device() }
                link = io { pairingStore.read()?.link }
                started = true
                if (drain(includeRetries = false)) return@work
            }
            val current = link ?: return@work
            if (current.blockedReason == null) verifyLink(current, current.endpoint)
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

    fun updateAddress(address: String) = work {
        val current = link ?: return@work
        if (current.blockedReason != null) return@work
        val endpoint = DesktopEndpoint.parse(address, current.endpoint.fingerprint)
        if (verifyLink(current, endpoint)) drain(includeRetries = true)
    }

    fun openProject(selected: DesktopProject) = work {
        messages = io { store.messages(selected.id) }
        dialogueHeads = io { store.dialogueHeads(selected.id) }
        rejectedDialogues = io { store.rejectedDialogues(selected.id) }
        project = selected
    }

    fun closeProject() {
        project = null
        messages = emptyList()
        dialogueHeads = emptyMap()
        rejectedDialogues = emptySet()
        dialogueNotice = null
    }

    fun createProject(name: String, firstMessage: String? = null, onSaved: () -> Unit) = work {
        val endpoint = link?.endpoint ?: return@work
        val title = name.trim()
        if (title.isEmpty() || title.length > 64) throw DesktopFailure("PROJECT_NAME")
        firstMessage?.let(::validateBody)
        val created = io { store.createProject(title, endpoint.fingerprint, firstMessage) }
        projects = listOf(created) + projects
        messages = emptyList()
        project = created
        onSaved()
        if (firstMessage != null) {
            refreshMessages()
            drain(includeRetries = false)
        }
    }

    fun enqueue(body: String, onSaved: () -> Unit) = work {
        val selected = project ?: return@work
        validateBody(body)
        io { store.enqueue(selected.id, body) }
        onSaved()
        refreshMessages()
        drain(includeRetries = false)
    }

    private fun validateBody(body: String) {
        if (body.isBlank() || body.codePointCount(0, body.length) > MessageProtocol.MAX_BODY_CHARS ||
            !Charsets.UTF_8.newEncoder().canEncode(body)
        ) throw DesktopFailure("MESSAGE_BODY")
    }

    fun retry() = work {
        if (!drain(includeRetries = true)) notice = "当前没有可重试的消息，请核对配对状态或稍后再试。"
    }

    fun refreshDialogue() = work { syncDialogue() }

    fun submitDialogue(message: QueuedMessage, head: JSONObject, type: String, text: String? = null) = work {
        val selected = project ?: return@work
        val current = link ?: return@work
        if (message.delivery != Delivery.RECEIVED || selected.id != message.projectId ||
            current.endpoint.fingerprint != selected.fingerprint || current.blockedReason != null
        ) throw DesktopFailure("INVALID_STATE")
        if (type in setOf("phone.answer", "phone.revise") && text.isNullOrBlank()) {
            throw DesktopFailure("MESSAGE_BODY")
        }
        val event = io { store.queueDialogueSubmission(message, type, head, text) }
        try {
            io {
                requireNetwork(current.endpoint)
                client.submitDialogue(current.endpoint, credential(), event)
                store.acceptDialogueSubmission(message.id, event.getString("eventId"))
            }
        } catch (failure: DesktopFailure) {
            if (!failure.retryable) {
                io { store.discardDialogueSubmission(message.id, event.getString("eventId")) }
            }
            if (failure.code in dialogueRefreshErrors) {
                syncDialogue()
                if (project == selected) dialogueNotice = desktopFailureText(failure.code)
                return@work
            }
            throw failure
        }
        syncDialogue()
    }

    private suspend fun syncDialogue() {
        val selected = project ?: return
        val identity = device ?: return
        val current = link ?: return
        if (current.endpoint.fingerprint != selected.fingerprint || current.blockedReason != null) return
        dialogueNotice = null
        try {
            val received = messages.filter { it.delivery == Delivery.RECEIVED }
            val localHeads = dialogueHeads
            if (received.isEmpty()) return
            val refreshFailure = io {
                requireNetwork(current.endpoint)
                val credential = credential()
                var failureCode: String? = null
                for (message in received) {
                    try {
                        val pending = store.pendingDialogueSubmission(message.id)
                        if (pending != null) {
                            try {
                                client.submitDialogue(current.endpoint, credential, pending)
                                store.acceptDialogueSubmission(message.id, pending.getString("eventId"))
                            } catch (failure: DesktopFailure) {
                                if (!failure.retryable) {
                                    store.discardDialogueSubmission(message.id, pending.getString("eventId"))
                                }
                                if (failure.code !in dialogueRefreshErrors) throw failure
                                failureCode = failure.code
                            }
                        }
                        var through = localHeads[message.id]?.getInt("sequence") ?: 0
                        do {
                            val page = client.pullDialogue(current.endpoint, identity, credential, selected.id, message.id, through)
                            through = store.applyDialoguePage(message, page)
                        } while (page.getBoolean("hasMore"))
                        if (through > 0) {
                            client.acknowledgeDialogue(current.endpoint, identity, credential, selected.id, message.id, through)
                        }
                    } catch (failure: DesktopFailure) {
                        if (failure.code != "SESSION_NOT_FOUND") throw failure
                        failureCode = failureCode ?: failure.code
                    }
                }
                failureCode
            }
            connection = "CONNECTED"
            if (refreshFailure != null && project == selected) dialogueNotice = desktopFailureText(refreshFailure)
        } catch (failure: DesktopFailure) {
            if (failure.pairingLost) blockPairing(current, failure.code)
            if (project == selected) dialogueNotice = desktopFailureText(failure.code)
        } finally {
            val updated = io { store.dialogueHeads(selected.id) }
            val rejected = io { store.rejectedDialogues(selected.id) }
            if (project == selected) {
                dialogueHeads = updated
                rejectedDialogues = rejected
            }
        }
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
                    val credential = credential()
                    requireNetwork(current.endpoint)
                    client.send(current.endpoint, identity, credential, message)
                }
                io { store.received(message.id, receipt) }
                connection = "CONNECTED"
            } catch (failure: DesktopFailure) {
                val pairingLost = failure.pairingLost
                val canRetry = failure.retryable || pairingLost ||
                    failure.code in setOf("SERVER_IDENTITY_MISMATCH", "TLS_UNAVAILABLE")
                if (pairingLost) blockPairing(current, failure.code)
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

    private suspend fun verifyLink(current: DesktopLink, endpoint: DesktopEndpoint): Boolean {
        val identity = device ?: return false
        val updated = DesktopLink(endpoint)
        try {
            io {
                val credential = credential()
                requireNetwork(endpoint)
                client.checkPairing(endpoint, identity, credential)
                if (updated != current) pairingStore.save(DesktopPairing(updated, credential))
            }
            link = updated
            connection = "CONNECTED"
            return true
        } catch (failure: DesktopFailure) {
            if (failure.pairingLost) blockPairing(current, failure.code)
            if (updated == current || failure.pairingLost) connection = failure.code
            else notice = desktopFailureText(failure.code)
            return false
        }
    }

    private suspend fun blockPairing(current: DesktopLink, reason: String) {
        val blocked = current.copy(blockedReason = reason)
        link = blocked
        io { pairingStore.save(DesktopPairing(blocked, null)) }
    }

    private fun credential() = pairingStore.read()?.credential ?: throw DesktopFailure("PAIRING_REQUIRED")

    private fun requireNetwork(endpoint: DesktopEndpoint) {
        if (endpoint.host != "127.0.0.1" && connectivity.activeNetwork == null) {
            throw DesktopFailure("DESKTOP_OFFLINE", retryable = true)
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

private val DesktopFailure.pairingLost: Boolean
    get() = code in setOf("DEVICE_REVOKED", "AUTH_REQUIRED", "PAIRING_REQUIRED", "PAIRING_EXPIRED")

private val dialogueRefreshErrors = setOf("STALE_EVENT", "EXPIRED_EVENT", "SPEC_MISMATCH", "INVALID_STATE")

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
    "DESKTOP_OFFLINE" -> "手机当前离线，联网后可重试。"
    "DESKTOP_UNREACHABLE" -> "无法连接电脑，请检查网络和接收服务。"
    "DESKTOP_TIMEOUT" -> "连接或等待电脑响应超时。"
    "DESKTOP_INTERRUPTED" -> "与电脑的连接中断，可重试原消息。"
    "DESKTOP_RESPONSE" -> "电脑响应无法核验。"
    "RETRY_WINDOW_EXPIRED" -> "已超过 24 小时重试期限。"
    "MESSAGE_ID_CONFLICT", "MESSAGE_SEQUENCE_CONFLICT" -> "电脑已保存同一消息 ID 或序号的其他内容。"
    "PAYLOAD_TOO_LARGE", "INVALID_REQUEST" -> "电脑拒绝了消息大小或格式。"
    "RATE_LIMITED" -> "发送过于频繁，请稍后重试。"
    "INTERNAL_ERROR" -> "电脑接收服务暂时无法保存消息。"
    "SESSION_NOT_FOUND" -> "电脑尚未提供此规格会话，请稍后刷新。"
    "STALE_EVENT", "SPEC_MISMATCH", "INVALID_STATE" -> "审阅内容或会话状态已变化，请刷新规格会话并重新审阅。"
    "EXPIRED_EVENT" -> "本次审阅或问题已过期，请刷新规格会话后再提交。"
    "DIALOGUE_PENDING" -> "上一项操作仍待提交，请刷新规格会话以重试。"
    "CURSOR_EXPIRED", "INVALID_CURSOR" -> "电脑会话记录与本机进度不一致。"
    "MODEL_USAGE_UNCERTAIN" -> "上次模型调用结果待核对。"
    "MODEL_BUDGET_EXHAUSTED" -> "项目模型预算已用完。"
    "MODEL_INPUT_TOO_LARGE" -> "需求正文超过模型处理上限。"
    "MODEL_INVALID_OUTPUT" -> "电脑收到的规格格式无效，无法进入审阅。"
    "MODEL_OUTPUT_TOO_LARGE" -> "电脑收到的规格超过处理上限。"
    "MODEL_TIMEOUT" -> "电脑等待模型响应超时。"
    "PROJECT_NAME" -> "请输入 1 至 64 个字符的项目名称。"
    "MESSAGE_BODY" -> "请输入消息，最多 12,000 个字符。"
    else -> "电脑拒绝了请求。"
}
