package com.example.pluginhost

import org.json.JSONException
import org.json.JSONObject
import java.nio.charset.StandardCharsets
import java.security.MessageDigest

/** Strict v1 parser for the phone-to-desktop pairing and message contract. */
object MessageProtocol {
    const val SCHEMA_VERSION = 1
    const val MAX_REQUEST_BYTES = 128 * 1024
    const val MAX_BODY_CHARS = 12_000

    private val uuid = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
    private val timestamp = Regex("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,6})?Z")
    private val deviceId = Regex("[a-z][a-z0-9-]{2,63}")
    private val projectId = Regex("[a-z][a-z0-9-]{2,63}")
    private val sha256 = Regex("[a-f0-9]{64}")
    private val pairingCode = Regex("[0-9]{6}")
    private val credential = Regex("[A-Za-z0-9_-]{43,512}")
    private val errorCodes = setOf(
        "INVALID_REQUEST", "UNSUPPORTED_SCHEMA_VERSION", "AUTH_REQUIRED", "PAIRING_REQUIRED",
        "PAIRING_EXPIRED", "PAIRING_REJECTED", "SERVER_IDENTITY_MISMATCH", "PAIRING_RATE_LIMITED",
        "DEVICE_REVOKED", "MESSAGE_ID_CONFLICT", "MESSAGE_SEQUENCE_CONFLICT", "MESSAGE_NOT_FOUND",
        "PAYLOAD_TOO_LARGE", "RATE_LIMITED", "RETRY_WINDOW_EXPIRED", "INTERNAL_ERROR",
    )

    data class ParsedEnvelope(val operation: String, val json: JSONObject)

    class ProtocolException(val code: String, val reason: String) : IllegalArgumentException(reason)

    fun parse(raw: String): ParsedEnvelope {
        if (raw.toByteArray(StandardCharsets.UTF_8).size > MAX_REQUEST_BYTES) fail("PAYLOAD_TOO_LARGE", "request-bytes")
        if (raw.startsWith('\uFEFF')) fail("INVALID_REQUEST", "bom")
        try {
            JsonKeyScanner(raw).parseDocument()
        } catch (error: ProtocolException) {
            throw error
        } catch (_: RuntimeException) {
            fail("INVALID_REQUEST", "json")
        }
        val root = try {
            JSONObject(raw)
        } catch (_: JSONException) {
            fail("INVALID_REQUEST", "json")
        }
        if (!root.has("schemaVersion")) fail("INVALID_REQUEST", "missing-schema-version")
        if (int(root, "schemaVersion") != SCHEMA_VERSION) fail("UNSUPPORTED_SCHEMA_VERSION", "schema-version")
        val operation = string(root, "operation")
        when (operation) {
            "pair.request" -> pairRequest(root)
            "pair.confirm" -> pairConfirm(root)
            "pair.revoke" -> pairRevoke(root)
            "pair.revoked" -> pairRevoked(root)
            "message.send" -> messageSend(root)
            "message.receipt" -> messageReceipt(root)
            "status.query" -> statusQuery(root)
            "status.response" -> statusResponse(root)
            "error" -> errorResponse(root)
            else -> fail("INVALID_REQUEST", "operation")
        }
        return ParsedEnvelope(operation, root)
    }

    private fun pairRequest(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "createdAt", "operation", "deviceId", "deviceName", "clientKeyFingerprint", "pairingCode", "expiresAt"))
        request(value)
        if (string(value, "operation") != "pair.request") fail("INVALID_REQUEST", "operation")
        device(value, "deviceId")
        val name = string(value, "deviceName", 64)
        if (name.any { it.code < 0x20 || it.code == 0x7f }) fail("INVALID_REQUEST", "device-name-control")
        hash(value, "clientKeyFingerprint")
        if (!pairingCode.matches(string(value, "pairingCode"))) fail("INVALID_REQUEST", "pairing-code-format")
        timestamp(value, "expiresAt")
    }

    private fun pairConfirm(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "approved"), setOf("serverCertificateFingerprint", "credentialId", "credential", "credentialExpiresAt", "errorCode"))
        response(value)
        if (string(value, "operation") != "pair.confirm") fail("INVALID_REQUEST", "operation")
        device(value, "deviceId")
        val approved = boolean(value, "approved")
        if (approved) {
            keys(value, setOf("schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "approved", "serverCertificateFingerprint", "credentialId", "credential", "credentialExpiresAt"))
            hash(value, "serverCertificateFingerprint")
            uuid(value, "credentialId")
            if (!credential.matches(string(value, "credential"))) fail("INVALID_REQUEST", "credential-format")
            timestamp(value, "credentialExpiresAt")
        } else {
            keys(value, setOf("schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "approved", "errorCode"))
            if (string(value, "errorCode") !in setOf("PAIRING_EXPIRED", "PAIRING_REJECTED", "SERVER_IDENTITY_MISMATCH", "PAIRING_RATE_LIMITED")) {
                fail("INVALID_REQUEST", "pair-error-code")
            }
        }
    }

    private fun pairRevoke(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "createdAt", "operation", "deviceId", "credentialId", "reason"))
        request(value)
        if (string(value, "operation") != "pair.revoke") fail("INVALID_REQUEST", "operation")
        device(value, "deviceId")
        uuid(value, "credentialId")
        if (string(value, "reason") !in setOf("user_requested", "credential_compromised", "device_removed")) fail("INVALID_REQUEST", "revoke-reason")
    }

    private fun pairRevoked(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "credentialId", "revokedAt"))
        response(value)
        if (string(value, "operation") != "pair.revoked") fail("INVALID_REQUEST", "operation")
        device(value, "deviceId")
        uuid(value, "credentialId")
        timestamp(value, "revokedAt")
    }

    private fun messageSend(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "createdAt", "operation", "deviceId", "credentialId", "messageId", "projectId", "sequence", "messageType", "body", "bodySha256"), setOf("specVersion", "confirmation"))
        request(value)
        if (string(value, "operation") != "message.send") fail("INVALID_REQUEST", "operation")
        device(value, "deviceId")
        uuid(value, "credentialId")
        uuid(value, "messageId")
        if (!projectId.matches(string(value, "projectId"))) fail("INVALID_REQUEST", "project-id-format")
        integer(value, "sequence", 1, 2_147_483_647)
        if (string(value, "messageType") !in setOf("user.message", "user.answer", "user.confirmation")) fail("INVALID_REQUEST", "message-type")
        val body = string(value, "body")
        if (body.codePointCount(0, body.length) > MAX_BODY_CHARS) fail("INVALID_REQUEST", "body-length")
        hash(value, "bodySha256")
        val actual = MessageDigest.getInstance("SHA-256").digest(body.toByteArray(StandardCharsets.UTF_8)).joinToString("") { "%02x".format(it) }
        if (actual != string(value, "bodySha256")) fail("INVALID_REQUEST", "body-hash")
        if (value.has("specVersion")) integer(value, "specVersion", 1, Int.MAX_VALUE)
        if (value.has("confirmation")) {
            val confirmation = value.optJSONObject("confirmation") ?: fail("INVALID_REQUEST", "confirmation-type")
            keys(confirmation, setOf("specVersion", "decision", "confirmedAt"))
            integer(confirmation, "specVersion", 1, Int.MAX_VALUE)
            if (string(confirmation, "decision") !in setOf("confirmed", "rejected")) fail("INVALID_REQUEST", "confirmation-decision")
            timestamp(confirmation, "confirmedAt")
        }
    }

    private fun messageReceipt(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "messageId", "projectId", "sequence", "status", "receivedAt", "retryable"))
        response(value)
        if (string(value, "operation") != "message.receipt") fail("INVALID_REQUEST", "operation")
        device(value, "deviceId")
        uuid(value, "messageId")
        if (!projectId.matches(string(value, "projectId"))) fail("INVALID_REQUEST", "project-id-format")
        integer(value, "sequence", 1, 2_147_483_647)
        if (string(value, "status") !in setOf("received", "duplicate") || boolean(value, "retryable")) fail("INVALID_REQUEST", "receipt-status")
        timestamp(value, "receivedAt")
    }

    private fun statusQuery(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "createdAt", "operation", "deviceId", "credentialId", "messageId"))
        request(value)
        if (string(value, "operation") != "status.query") fail("INVALID_REQUEST", "operation")
        device(value, "deviceId")
        uuid(value, "credentialId")
        uuid(value, "messageId")
    }

    private fun statusResponse(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "messageId", "projectId", "sequence", "status", "updatedAt", "retryable"))
        response(value)
        if (string(value, "operation") != "status.response") fail("INVALID_REQUEST", "operation")
        device(value, "deviceId")
        uuid(value, "messageId")
        if (!projectId.matches(string(value, "projectId"))) fail("INVALID_REQUEST", "project-id-format")
        integer(value, "sequence", 1, 2_147_483_647)
        if (string(value, "status") !in setOf("queued", "received", "failed_retryable", "rejected", "revoked")) fail("INVALID_REQUEST", "message-status")
        timestamp(value, "updatedAt")
        boolean(value, "retryable")
    }

    private fun errorResponse(value: JSONObject) {
        keys(value, setOf("schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "error"))
        response(value)
        if (string(value, "operation") != "error") fail("INVALID_REQUEST", "operation")
        val error = value.optJSONObject("error") ?: fail("INVALID_REQUEST", "error-object")
        keys(error, setOf("code", "retryable"), setOf("retryAfterSeconds"))
        if (string(error, "code") !in errorCodes) fail("INVALID_REQUEST", "error-code")
        val retryable = boolean(error, "retryable")
        if (error.has("retryAfterSeconds")) {
            if (!retryable) fail("INVALID_REQUEST", "retry-after-without-retry")
            integer(error, "retryAfterSeconds", 1, 86400)
        }
    }

    private fun request(value: JSONObject) {
        uuid(value, "requestId")
        timestamp(value, "createdAt")
    }

    private fun response(value: JSONObject) {
        request(value)
        uuid(value, "inReplyTo")
    }

    private fun keys(value: JSONObject, required: Set<String>, optional: Set<String> = emptySet()) {
        val actual = mutableSetOf<String>()
        val iterator = value.keys()
        while (iterator.hasNext()) actual += iterator.next()
        if ((actual - required - optional).isNotEmpty() || (required - actual).isNotEmpty()) fail("INVALID_REQUEST", "fields")
    }

    private fun value(value: JSONObject, field: String): Any {
        if (!value.has(field) || value.isNull(field)) fail("INVALID_REQUEST", "$field-missing")
        return try { value.get(field) } catch (_: JSONException) { fail("INVALID_REQUEST", "$field-missing") }
    }

    private fun string(value: JSONObject, field: String, maxLength: Int? = null): String {
        val result = value(value, field) as? String ?: fail("INVALID_REQUEST", "$field-type")
        if (result.isEmpty() || (maxLength != null && result.length > maxLength)) fail("INVALID_REQUEST", "$field-length")
        return result
    }

    private fun int(value: JSONObject, field: String): Int = integer(value, field, Int.MIN_VALUE, Int.MAX_VALUE)

    private fun integer(value: JSONObject, field: String, minimum: Int, maximum: Int): Int {
        val raw = value(value, field)
        val result = when (raw) {
            is Int -> raw
            is Long -> if (raw in Int.MIN_VALUE..Int.MAX_VALUE) raw.toInt() else fail("INVALID_REQUEST", "$field-range")
            else -> fail("INVALID_REQUEST", "$field-type")
        }
        if (result < minimum || result > maximum) fail("INVALID_REQUEST", "$field-range")
        return result
    }

    private fun boolean(value: JSONObject, field: String): Boolean = value(value, field) as? Boolean ?: fail("INVALID_REQUEST", "$field-type")

    private fun uuid(value: JSONObject, field: String) {
        if (!uuid.matches(string(value, field))) fail("INVALID_REQUEST", "$field-format")
    }

    private fun timestamp(value: JSONObject, field: String) {
        if (!timestamp.matches(string(value, field))) fail("INVALID_REQUEST", "$field-format")
    }

    private fun device(value: JSONObject, field: String) {
        if (!deviceId.matches(string(value, field))) fail("INVALID_REQUEST", "$field-format")
    }

    private fun hash(value: JSONObject, field: String) {
        if (!sha256.matches(string(value, field))) fail("INVALID_REQUEST", "$field-format")
    }

    private fun fail(code: String, reason: String): Nothing = throw ProtocolException(code, reason)

    private class JsonKeyScanner(private val source: String) {
        private var index = 0

        private fun fail(code: String, reason: String): Nothing = throw ProtocolException(code, reason)

        fun parseDocument() {
            whitespace()
            value()
            whitespace()
            if (index != source.length) fail("INVALID_REQUEST", "trailing-json")
        }

        private fun value() {
            whitespace()
            if (index >= source.length) fail("INVALID_REQUEST", "json")
            when (source[index]) {
                '{' -> objectValue()
                '[' -> arrayValue()
                '"' -> stringValue()
                else -> primitive()
            }
            whitespace()
        }

        private fun objectValue() {
            index++
            whitespace()
            val names = mutableSetOf<String>()
            if (consume('}')) return
            while (true) {
                whitespace()
                if (index >= source.length || source[index] != '"') fail("INVALID_REQUEST", "object-key")
                val name = stringValue()
                if (!names.add(name)) fail("INVALID_REQUEST", "duplicate-json-key")
                whitespace()
                if (!consume(':')) fail("INVALID_REQUEST", "object-colon")
                value()
                if (consume('}')) return
                if (!consume(',')) fail("INVALID_REQUEST", "object-comma")
            }
        }

        private fun arrayValue() {
            index++
            whitespace()
            if (consume(']')) return
            while (true) {
                value()
                if (consume(']')) return
                if (!consume(',')) fail("INVALID_REQUEST", "array-comma")
            }
        }

        private fun stringValue(): String {
            if (!consume('"')) fail("INVALID_REQUEST", "string")
            val result = StringBuilder()
            while (index < source.length) {
                when (val character = source[index++]) {
                    '"' -> return result.toString()
                    '\\' -> {
                        if (index >= source.length) fail("INVALID_REQUEST", "string-escape")
                        when (val escaped = source[index++]) {
                            '"', '\\', '/' -> result.append(escaped)
                            'b' -> result.append('\b')
                            'f' -> result.append('\u000c')
                            'n' -> result.append('\n')
                            'r' -> result.append('\r')
                            't' -> result.append('\t')
                            'u' -> {
                                if (index + 4 > source.length) fail("INVALID_REQUEST", "string-unicode")
                                val hex = source.substring(index, index + 4)
                                if (!hex.matches(Regex("[0-9a-fA-F]{4}"))) fail("INVALID_REQUEST", "string-unicode")
                                result.append(hex.toInt(16).toChar())
                                index += 4
                            }
                            else -> fail("INVALID_REQUEST", "string-escape")
                        }
                    }
                    else -> {
                        if (character.code < 0x20) fail("INVALID_REQUEST", "string-control")
                        result.append(character)
                    }
                }
            }
            fail("INVALID_REQUEST", "string-end")
        }

        private fun primitive() {
            val start = index
            while (index < source.length && source[index] !in ",]}\t\r\n ") index++
            if (start == index) fail("INVALID_REQUEST", "primitive")
        }

        private fun whitespace() {
            while (index < source.length && source[index] in " \t\r\n") index++
        }

        private fun consume(expected: Char): Boolean {
            if (index < source.length && source[index] == expected) {
                index++
                whitespace()
                return true
            }
            return false
        }
    }
}
