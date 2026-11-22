package com.example.pluginhost

import org.json.JSONObject
import java.util.Date
import java.util.GregorianCalendar
import java.util.Locale
import java.util.TimeZone

/** Dialogue v1 wire validation and submission preconditions. */
object DialogueProtocol {
    private val scope = setOf("deviceId", "projectId", "sessionId")
    private val spec = setOf("specVersion", "specSha256")
    private val link = setOf("inReplyTo")
    private val fields = mapOf(
        "desktop.status" to setOf("state"),
        "desktop.question" to spec + setOf("expiresAt", "question"),
        "desktop.draft" to spec + "specJson",
        "desktop.review" to spec + setOf("expiresAt", "specJson", "summary"),
        "desktop.error" to setOf("code"),
        "phone.answer" to link + spec + "text",
        "phone.cancel" to link,
        "phone.revise" to link + spec + "text",
        "phone.confirm" to link + spec,
        "phone.reject" to link + spec,
        "dialogue.pull" to setOf("after", "limit"),
        "dialogue.page" to setOf("after", "next", "hasMore", "events"),
        "dialogue.ack" to setOf("through"),
        "dialogue.accepted" to setOf("eventId", "sequence", "duplicate"),
        "dialogue.error" to setOf("code"),
    )
    private val transitions = mapOf(
        "connected" to setOf("queued", "cancelled", "failed"),
        "queued" to setOf("running", "cancelled", "failed"),
        "running" to setOf("waiting_for_user", "result_ready", "cancelled", "failed"),
        "waiting_for_user" to setOf("queued", "cancelled", "failed"),
        "result_ready" to setOf("queued", "confirmed", "cancelled", "failed"),
        "failed" to setOf("queued", "cancelled"),
        "confirmed" to emptySet(),
        "cancelled" to emptySet(),
    )
    private val errors = setOf(
        "INVALID_REQUEST", "UNSUPPORTED_SCHEMA_VERSION", "PAYLOAD_TOO_LARGE", "AUTH_REQUIRED",
        "DEVICE_REVOKED", "SESSION_NOT_FOUND", "CURSOR_EXPIRED", "INVALID_CURSOR",
        "EVENT_ID_CONFLICT", "EVENT_SEQUENCE_CONFLICT", "STALE_EVENT", "EXPIRED_EVENT",
        "SPEC_MISMATCH", "INVALID_STATE", "RATE_LIMITED", "INTERNAL_ERROR",
        "MODEL_TIMEOUT", "MODEL_CANCELLED", "MODEL_UNAVAILABLE", "MODEL_AUTH_FAILED",
        "MODEL_RATE_LIMITED", "MODEL_INVALID_OUTPUT", "MODEL_OUTPUT_TOO_LARGE",
        "MODEL_BUDGET_EXHAUSTED", "MODEL_ROUND_LIMIT", "MODEL_CONFIG_INVALID", "MODEL_BUSY",
        "MODEL_USAGE_UNCERTAIN", "MODEL_INPUT_TOO_LARGE", "MODEL_REQUEST_CONFLICT",
    )
    private val slug = Regex("[a-z][a-z0-9-]{2,63}")
    private val uuid = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
    private val digest = Regex("[a-f0-9]{64}")
    private val utc = Regex("([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\\.([0-9]{1,6}))?Z")
    private const val day = 86_400_000_000L

    private fun checkWire(condition: Boolean, code: String = "INVALID_REQUEST") {
        if (!condition) throw MessageProtocol.ProtocolException(code, "dialogue")
    }

    private fun text(value: JSONObject, key: String, limit: Int = Int.MAX_VALUE): String {
        val result = value.opt(key) as? String ?: throw MessageProtocol.ProtocolException("INVALID_REQUEST", key)
        checkWire(result.isNotEmpty() && result.codePointCount(0, result.length) <= limit)
        return result
    }

    private fun number(value: JSONObject, key: String, min: Int = 1, max: Int = Int.MAX_VALUE): Int {
        val result = value.opt(key) as? Int ?: throw MessageProtocol.ProtocolException("INVALID_REQUEST", key)
        checkWire(result in min..max)
        return result
    }

    internal fun micros(value: String): Long {
        val match = utc.matchEntire(value) ?: throw MessageProtocol.ProtocolException("INVALID_REQUEST", "timestamp")
        val parts = match.groupValues.drop(1).take(6).map(String::toInt)
        checkWire(parts[0] > 0)
        val calendar = GregorianCalendar(TimeZone.getTimeZone("UTC"), Locale.ROOT).apply {
            gregorianChange = Date(Long.MIN_VALUE)
            isLenient = false
            clear()
            set(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5])
        }
        return try {
            calendar.timeInMillis * 1000 + match.groupValues[7].padEnd(6, '0').toLong()
        } catch (_: IllegalArgumentException) {
            throw MessageProtocol.ProtocolException("INVALID_REQUEST", "timestamp")
        }
    }

    fun parse(raw: String): JSONObject = MessageProtocol.parseObject(raw, strict = true).also(::validate)

    private fun validate(value: JSONObject) {
        checkWire(number(value, "dialogueVersion", Int.MIN_VALUE) == 1, "UNSUPPORTED_SCHEMA_VERSION")
        val kind = text(value, "type")
        val typeFields = fields[kind] ?: throw MessageProtocol.ProtocolException("INVALID_REQUEST", "type")
        val isEvent = !kind.startsWith("dialogue.")
        var required = scope + typeFields + setOf("dialogueVersion", "type")
        if (isEvent) required = required + setOf("eventId", "sequence", "createdAt", "messageId")
        if (kind == "desktop.status") {
            val state = text(value, "state")
            checkWire(state in setOf("connected", "queued", "running", "cancelled", "confirmed"))
            if (state == "confirmed") required = required + spec + link + "savedSpecSha256"
        }
        checkWire(value.keys().asSequence().toSet() == required)
        for (key in setOf("deviceId", "projectId")) checkWire(slug.matches(text(value, key)))
        for (key in required intersect setOf("sessionId", "eventId", "messageId", "inReplyTo")) {
            checkWire(uuid.matches(text(value, key)))
        }
        for (key in required intersect setOf("sequence", "specVersion", "through")) number(value, key)
        for (key in required intersect setOf("specSha256", "savedSpecSha256")) checkWire(digest.matches(text(value, key)))
        for (key in required intersect setOf("text", "question", "summary")) {
            text(value, key, if (key == "text") 12_000 else 2_000)
        }
        if (isEvent) {
            val created = micros(text(value, "createdAt"))
            if ("expiresAt" in required) checkWire(micros(text(value, "expiresAt")) - created in 1..day)
        }
        if ("specJson" in required) {
            val raw = text(value, "specJson")
            val bytes = raw.toByteArray(Charsets.UTF_8)
            checkWire(bytes.size <= 48 * 1024, "PAYLOAD_TOO_LARGE")
            checkWire(MessageProtocol.sha256Hex(bytes) == text(value, "specSha256"))
            val draft = MessageProtocol.parseObject(raw, strict = true)
            checkWire(draft.opt("status") == "draft" && draft.opt("projectId") == value.opt("projectId"))
            checkWire(number(draft, "schemaVersion") == 1 && number(draft, "version") == number(value, "specVersion"))
        }
        if ("code" in required) checkWire(text(value, "code") in errors)
        if ("duplicate" in required) checkWire(value.opt("duplicate") is Boolean)
        if ("after" in required) number(value, "after", 0)
        if (kind == "dialogue.pull") number(value, "limit", 1, 20)
        if (kind == "dialogue.page") validatePage(value)
    }

    private fun validatePage(page: JSONObject) {
        val events = page.optJSONArray("events") ?: throw MessageProtocol.ProtocolException("INVALID_REQUEST", "events")
        val hasMore = page.opt("hasMore")
        val after = number(page, "after", 0).toLong()
        checkWire(events.length() <= 20 && hasMore is Boolean && (events.length() > 0 || hasMore == false))
        val ids = mutableSetOf<String>()
        for (index in 0 until events.length()) {
            val event = events.optJSONObject(index) ?: throw MessageProtocol.ProtocolException("INVALID_REQUEST", "event")
            checkWire(text(event, "type").startsWith("desktop."))
            validate(event)
            checkWire(scope.all { event.opt(it) == page.opt(it) } && number(event, "sequence").toLong() == after + index + 1)
            checkWire(ids.add(text(event, "eventId")))
        }
        checkWire(number(page, "next", 0).toLong() == after + events.length())
    }

    fun checkTransition(previous: String, following: String) {
        checkWire(following in transitions[previous].orEmpty(), "INVALID_STATE")
    }

    fun checkSubmission(event: JSONObject, head: JSONObject, prior: JSONObject?, nextSequence: Int, nowMicros: Long): String {
        val kind = text(event, "type")
        checkWire(kind.startsWith("phone."))
        checkWire(scope.all { event.opt(it) == head.opt(it) }, "SESSION_NOT_FOUND")
        if (prior != null) {
            val keys = event.keys().asSequence().toSet()
            checkWire(keys == prior.keys().asSequence().toSet() && keys.all { event.opt(it) == prior.opt(it) }, "EVENT_ID_CONFLICT")
            return "duplicate"
        }
        checkWire(number(event, "sequence") == nextSequence, "EVENT_SEQUENCE_CONFLICT")
        val created = micros(text(event, "createdAt"))
        checkWire(created > nowMicros - day && created <= nowMicros + 300_000_000L, "EXPIRED_EVENT")
        checkWire(event.opt("inReplyTo") == head.opt("eventId") && event.opt("messageId") == head.opt("messageId"), "STALE_EVENT")
        val state = when (head.opt("type")) {
            "desktop.question" -> "waiting_for_user"
            "desktop.review" -> "result_ready"
            "desktop.error" -> "failed"
            "desktop.draft" -> "running"
            else -> text(head, "state")
        }
        if (kind == "phone.cancel") {
            checkTransition(state, "cancelled")
        } else {
            checkWire(state == if (kind == "phone.answer") "waiting_for_user" else "result_ready", "INVALID_STATE")
            checkWire(nowMicros < micros(text(head, "expiresAt")), "EXPIRED_EVENT")
            checkWire(spec.all { event.opt(it) == head.opt(it) }, "SPEC_MISMATCH")
        }
        return "accepted"
    }
}
