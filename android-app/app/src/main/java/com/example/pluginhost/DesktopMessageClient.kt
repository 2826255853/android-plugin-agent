package com.example.pluginhost

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.EOFException
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.UUID
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLException
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

internal data class DesktopEndpoint(val host: String, val port: Int, val fingerprint: String) {
    val address get() = "https://$host:$port"

    companion object {
        fun parse(address: String, fingerprint: String): DesktopEndpoint {
            val match = Regex("https://((?:[0-9]{1,3}\\.){3}[0-9]{1,3}):([0-9]{1,5})")
                .matchEntire(address.trim()) ?: throw DesktopFailure("DESKTOP_ADDRESS")
            val host = match.groupValues[1]
            val octets = host.split('.').map(String::toInt)
            val port = match.groupValues[2].toInt()
            val privateAddress = octets[0] == 10 || octets[0] == 172 && octets[1] in 16..31 ||
                octets[0] == 192 && octets[1] == 168 || octets[0] == 169 && octets[1] == 254
            if (octets.any { it > 255 } || host != octets.joinToString(".") ||
                (!privateAddress && host != "127.0.0.1") || port !in 1..65535
            ) throw DesktopFailure("DESKTOP_ADDRESS")
            val pin = fingerprint.trim().lowercase(Locale.ROOT)
            if (!pin.matches(Regex("[0-9a-f]{64}"))) throw DesktopFailure("DESKTOP_FINGERPRINT")
            return DesktopEndpoint(host, port, pin)
        }
    }
}

internal data class DesktopDevice(val id: String, val fingerprint: String)

internal data class DesktopLink(val endpoint: DesktopEndpoint, val blockedReason: String? = null)

// Keep these values out of UI models and generated toString() methods.
internal class DesktopCredential(val id: String, val token: String, val expiresAt: String)

internal class DesktopPairing(val link: DesktopLink, val credential: DesktopCredential?)

internal data class DesktopReceipt(val id: String, val receivedAt: String)

internal class DesktopFailure(
    val code: String,
    val retryable: Boolean = false,
    val retryAfterSeconds: Int = 0,
) : Exception(code)

internal fun messageTimestamp(time: Long = System.currentTimeMillis()): String =
    SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.ROOT).apply {
        timeZone = TimeZone.getTimeZone("UTC")
    }.format(Date(time))

internal const val MESSAGE_RETRY_WINDOW_MS = 24 * 60 * 60 * 1000L

internal class DesktopMessageClient {
    fun pair(endpoint: DesktopEndpoint, device: DesktopDevice, code: String): DesktopCredential {
        val now = System.currentTimeMillis()
        val request = envelope("pair.request", messageTimestamp(now))
            .put("deviceId", device.id)
            .put("deviceName", "Android Plugin Host")
            .put("clientKeyFingerprint", device.fingerprint)
            .put("pairingCode", code)
            .put("expiresAt", messageTimestamp(now + 10 * 60 * 1000))
        val (status, response) = post(endpoint, "/v1/pair", request, responseTimeout = 610_000)
        val value = response.json
        if (status != 200 || response.operation != "pair.confirm" || value.getString("deviceId") != device.id) {
            throw DesktopFailure("DESKTOP_RESPONSE", retryable = true)
        }
        if (!value.getBoolean("approved")) throw DesktopFailure(value.getString("errorCode"))
        if (value.getString("serverCertificateFingerprint") != endpoint.fingerprint) {
            throw DesktopFailure("SERVER_IDENTITY_MISMATCH")
        }
        return DesktopCredential(
            value.getString("credentialId"), value.getString("credential"), value.getString("credentialExpiresAt"),
        )
    }

    fun send(
        endpoint: DesktopEndpoint,
        device: DesktopDevice,
        credential: DesktopCredential,
        message: QueuedMessage,
    ): DesktopReceipt {
        val request = envelope("message.send", messageTimestamp(message.createdAt))
            .put("deviceId", device.id)
            .put("credentialId", credential.id)
            .put("messageId", message.id)
            .put("projectId", message.projectId)
            .put("sequence", message.sequence)
            .put("messageType", "user.message")
            .put("body", message.body)
            .put("bodySha256", MessageProtocol.sha256Hex(message.body.toByteArray(Charsets.UTF_8)))
        val (status, response) = post(endpoint, "/v1/messages", request, credential.token)
        val value = response.json
        if (status != 200 || response.operation != "message.receipt" ||
            value.getString("deviceId") != device.id || value.getString("messageId") != message.id ||
            value.getString("projectId") != message.projectId || value.getInt("sequence") != message.sequence
        ) throw DesktopFailure("DESKTOP_RESPONSE", retryable = true)
        return DesktopReceipt(value.getString("requestId"), value.getString("receivedAt"))
    }

    fun checkPairing(endpoint: DesktopEndpoint, device: DesktopDevice, credential: DesktopCredential) {
        val messageId = UUID.randomUUID().toString()
        val request = envelope("status.query", messageTimestamp())
            .put("deviceId", device.id)
            .put("credentialId", credential.id)
            .put("messageId", messageId)
        // A query for an unknown message only returns MESSAGE_NOT_FOUND after authentication.
        val (status, response) = try {
            post(endpoint, "/v1/status", request, credential.token)
        } catch (failure: DesktopFailure) {
            if (failure.code == "MESSAGE_NOT_FOUND") return
            throw failure
        }
        if (status != 200 || response.operation != "status.response" ||
            response.json.getString("deviceId") != device.id || response.json.getString("messageId") != messageId
        ) invalidResponse()
    }

    fun pullDialogue(
        endpoint: DesktopEndpoint,
        device: DesktopDevice,
        credential: DesktopCredential,
        projectId: String,
        sessionId: String,
        after: Int,
    ): JSONObject {
        val request = dialogueEnvelope("dialogue.pull", device, projectId, sessionId)
            .put("after", after)
            .put("limit", 20)
        val response = postDialogue(endpoint, "/v1/dialogue/pull", request, credential)
        if (response.optString("type") != "dialogue.page" ||
            response.optString("deviceId") != device.id || response.optString("projectId") != projectId ||
            response.optString("sessionId") != sessionId || response.optInt("after", -1) != after
        ) invalidResponse()
        return response
    }

    fun acknowledgeDialogue(
        endpoint: DesktopEndpoint,
        device: DesktopDevice,
        credential: DesktopCredential,
        projectId: String,
        sessionId: String,
        through: Int,
    ) {
        val request = dialogueEnvelope("dialogue.ack", device, projectId, sessionId).put("through", through)
        val response = postDialogue(endpoint, "/v1/dialogue/ack", request, credential)
        if (response.optString("type") != "dialogue.ack" ||
            response.optString("deviceId") != device.id || response.optString("projectId") != projectId ||
            response.optString("sessionId") != sessionId || response.optInt("through", -1) < through
        ) invalidResponse()
    }

    fun submitDialogue(
        endpoint: DesktopEndpoint,
        credential: DesktopCredential,
        event: JSONObject,
    ) {
        val response = postDialogue(endpoint, "/v1/dialogue/submit", event, credential)
        if (response.optString("type") != "dialogue.accepted" ||
            response.optString("eventId") != event.optString("eventId") ||
            response.optInt("sequence", -1) != event.optInt("sequence")
        ) invalidResponse()
    }

    private fun dialogueEnvelope(type: String, device: DesktopDevice, projectId: String, sessionId: String) =
        JSONObject().put("dialogueVersion", 1).put("deviceId", device.id).put("projectId", projectId)
            .put("sessionId", sessionId).put("type", type)

    private fun postDialogue(
        endpoint: DesktopEndpoint,
        path: String,
        request: JSONObject,
        credential: DesktopCredential,
    ): JSONObject {
        val json = request.toString()
        DialogueProtocol.parse(json)
        val (status, raw) = try {
            postRaw(endpoint, path, json, credential.token, credential.id)
        } catch (failure: DesktopFailure) {
            if (failure.code != "DESKTOP_INTERRUPTED") throw failure
            // A pull or acknowledgement can safely be repeated if the response was lost.
            postRaw(endpoint, path, json, credential.token, credential.id)
        }
        val response = try {
            DialogueProtocol.parse(raw)
        } catch (_: MessageProtocol.ProtocolException) {
            throw DesktopFailure("DESKTOP_RESPONSE", retryable = true)
        }
        if (listOf("deviceId", "projectId", "sessionId").any { response.getString(it) != request.getString(it) }) {
            invalidResponse()
        }
        if (status in 400..599 && response.optString("type") == "dialogue.error") {
            val code = response.getString("code")
            val retryable = code in setOf("RATE_LIMITED", "INTERNAL_ERROR", "MODEL_TIMEOUT", "MODEL_UNAVAILABLE")
            throw DesktopFailure(code, retryable)
        }
        if (status != 200 || response.optString("type") == "dialogue.error") invalidResponse()
        return response
    }

    private fun envelope(operation: String, createdAt: String): JSONObject = JSONObject()
        .put("schemaVersion", MessageProtocol.SCHEMA_VERSION)
        .put("requestId", UUID.randomUUID().toString())
        .put("operation", operation)
        .put("createdAt", createdAt)

    private fun post(
        endpoint: DesktopEndpoint,
        path: String,
        request: JSONObject,
        token: String? = null,
        responseTimeout: Int = 15_000,
    ): Pair<Int, MessageProtocol.ParsedEnvelope> {
        val json = request.toString()
        MessageProtocol.parse(json)
        val (status, raw) = postRaw(endpoint, path, json, token, null, responseTimeout)
        val response = try {
            MessageProtocol.parse(raw)
        } catch (_: MessageProtocol.ProtocolException) {
            throw DesktopFailure("DESKTOP_RESPONSE", retryable = true)
        }
        val error = response.json.optJSONObject("error")
        // The receiver rate-limits pairing before it reads the request ID.
        if (path == "/v1/pair" && status == 429 && error?.getString("code") == "PAIRING_RATE_LIMITED") {
            throw DesktopFailure("PAIRING_RATE_LIMITED")
        }
        if (response.json.optString("inReplyTo") != request.getString("requestId")) {
            throw DesktopFailure("DESKTOP_RESPONSE", retryable = true)
        }
        if (status == 403 && response.operation == "pair.revoked" && token != null &&
            response.json.getString("deviceId") == request.getString("deviceId") &&
            response.json.getString("credentialId") == request.getString("credentialId")
        ) throw DesktopFailure("DEVICE_REVOKED")
        if (status in 400..599 && response.operation == "error" && error != null) {
            throw DesktopFailure(error.getString("code"), error.getBoolean("retryable"), error.optInt("retryAfterSeconds"))
        }
        return status to response
    }

    private fun postRaw(
        endpoint: DesktopEndpoint,
        path: String,
        json: String,
        token: String? = null,
        credentialId: String? = null,
        responseTimeout: Int = 15_000,
    ): Pair<Int, String> {
        val body = json.toByteArray(Charsets.UTF_8)
        val pin = PinnedCertificate(endpoint.fingerprint)
        val ssl = SSLContext.getInstance("TLS").apply { init(null, arrayOf(pin), null) }
        var handshakeComplete = false
        val (status, response) = try {
            Socket(Proxy.NO_PROXY).use { tcp ->
                val ip = InetAddress.getByAddress(endpoint.host.split('.').map { it.toInt().toByte() }.toByteArray())
                tcp.connect(InetSocketAddress(ip, endpoint.port), 5_000)
                (ssl.socketFactory.createSocket(tcp, endpoint.host, endpoint.port, true) as SSLSocket).use { tls ->
                    if ("TLSv1.3" !in tls.supportedProtocols) throw DesktopFailure("TLS_UNAVAILABLE")
                    tls.enabledProtocols = arrayOf("TLSv1.3")
                    tls.soTimeout = 5_000
                    tls.startHandshake()
                    handshakeComplete = true
                    val headers = buildString {
                        append("POST $path HTTP/1.1\r\nHost: ${endpoint.host}:${endpoint.port}\r\n")
                        append("Content-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n")
                        if (token != null) append("Authorization: Bearer $token\r\n")
                        if (credentialId != null) append("X-Credential-Id: $credentialId\r\n")
                        append("\r\n")
                    }
                    val output = BufferedOutputStream(tls.outputStream)
                    output.write(headers.toByteArray(Charsets.US_ASCII))
                    output.write(body)
                    output.flush()
                    tls.soTimeout = responseTimeout
                    val input = DataInputStream(BufferedInputStream(tls.inputStream))
                    val first = input.readUnsignedByte()
                    tls.soTimeout = 5_000
                    readResponse(input, first)
                }
            }
        } catch (_: SocketTimeoutException) {
            throw DesktopFailure("DESKTOP_TIMEOUT", retryable = true)
        } catch (_: EOFException) {
            throw DesktopFailure("DESKTOP_INTERRUPTED", retryable = true)
        } catch (_: SSLException) {
            if (pin.rejected) throw DesktopFailure("SERVER_IDENTITY_MISMATCH")
            throw DesktopFailure(if (handshakeComplete) "DESKTOP_INTERRUPTED" else "DESKTOP_TLS", retryable = true)
        } catch (_: CharacterCodingException) {
            throw DesktopFailure("DESKTOP_RESPONSE", retryable = true)
        } catch (_: SocketException) {
            throw DesktopFailure(if (handshakeComplete) "DESKTOP_INTERRUPTED" else "DESKTOP_UNREACHABLE", retryable = true)
        } catch (_: IOException) {
            throw DesktopFailure("DESKTOP_UNREACHABLE", retryable = true)
        }
        return status to response
    }

    private fun readResponse(input: DataInputStream, first: Int): Pair<Int, String> {
        val statusLine = readLine(input, first)
        val status = Regex("HTTP/1\\.[01] ([1-5][0-9]{2})(?: .*)?")
            .matchEntire(statusLine)?.groupValues?.get(1)?.toInt() ?: invalidResponse()
        val headers = mutableMapOf<String, String>()
        repeat(24) {
            val line = readLine(input)
            if (line.isEmpty()) {
                val length = headers["content-length"]?.toIntOrNull() ?: invalidResponse()
                if (length !in 1..MessageProtocol.MAX_REQUEST_BYTES || "transfer-encoding" in headers ||
                    "content-encoding" in headers ||
                    headers["content-type"]?.substringBefore(';')?.trim()?.lowercase(Locale.ROOT) != "application/json"
                ) invalidResponse()
                val bytes = ByteArray(length)
                input.readFully(bytes)
                val json = Charsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes)).toString()
                return status to json
            }
            val name = line.substringBefore(':').lowercase(Locale.ROOT)
            if (':' !in line || headers.put(name, line.substringAfter(':').trim()) != null) invalidResponse()
        }
        invalidResponse()
    }

    private fun readLine(input: DataInputStream, first: Int = input.readUnsignedByte()): String {
        val line = ByteArrayOutputStream()
        var byte = first
        while (byte != '\r'.code) {
            if (line.size() == 4096 || byte !in 32..126) invalidResponse()
            line.write(byte)
            byte = input.readUnsignedByte()
        }
        if (input.readUnsignedByte() != '\n'.code) invalidResponse()
        return line.toString("US-ASCII")
    }

    private fun invalidResponse(): Nothing = throw DesktopFailure("DESKTOP_RESPONSE", retryable = true)

    // The desktop certificate is self-signed. The user-verified DER digest is its trust anchor.
    private class PinnedCertificate(private val fingerprint: String) : X509TrustManager {
        var rejected = false
            private set

        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            val leaf = chain.firstOrNull()
            if (leaf == null || MessageProtocol.sha256Hex(leaf.encoded) != fingerprint) {
                rejected = true
                throw CertificateException("Server certificate mismatch")
            }
            leaf.checkValidity()
        }

        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {
            throw CertificateException("Client certificate authentication is unsupported")
        }

        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }
}
