package com.example.pluginhost

import org.json.JSONObject

/** Stage D.1 candidate delivery v1. The caller supplies authenticated pairing context. */
internal object DeliveryProtocol {
    private const val MAX_SEGMENT_BYTES = 256L * 1024
    private const val MAX_PAGE = 20L
    private const val MAX_BUILD_ID = 9_007_199_254_740_991L
    private const val MAX_OFFER_AGE = 86_400_000_000L
    private val common = setOf("deliveryVersion", "type")
    private val target = setOf("sourceFingerprint", "deviceId", "credentialId", "projectId")
    private val build = setOf("taskId", "buildId", "packageSha256")
    private val checks = setOf("static", "packaging", "isolatedBuild", "isolatedTest", "interactivePreview")
    private val errors = setOf(
        "INVALID_REQUEST", "UNSUPPORTED_SCHEMA_VERSION", "PAYLOAD_TOO_LARGE", "AUTH_REQUIRED",
        "DEVICE_REVOKED", "DELIVERY_NOT_FOUND", "DELIVERY_EXPIRED", "DELIVERY_REVOKED",
        "DELIVERY_BINDING_MISMATCH", "DELIVERY_RANGE_INVALID", "REQUEST_ID_CONFLICT",
        "BUILD_NOT_AVAILABLE", "RATE_LIMITED", "INTERNAL_ERROR",
    )
    private val fields = mapOf(
        "delivery.receipt" to target + build + setOf(
            "deliveryId", "specVersion", "specSha256", "pluginId", "pluginName", "pluginVersion",
            "hostApi", "checks", "manifestSha256", "packageFile", "sizeBytes", "offeredAt", "expiresAt",
        ),
        "delivery.publish" to target + build + "requestId",
        "delivery.published" to setOf("requestId", "receipt"),
        "delivery.query" to setOf("requestId", "projectId", "after", "limit"),
        "delivery.page" to setOf("requestId", "projectId", "after", "hasMore", "receipts"),
        "delivery.lookup" to setOf("requestId", "projectId", "deliveryId"),
        "delivery.found" to setOf("requestId", "receipt"),
        "delivery.download" to setOf("requestId", "projectId", "deliveryId", "packageSha256", "offset", "length"),
        "delivery.revoke" to setOf("requestId", "deliveryId"),
        "delivery.revoked" to setOf("requestId", "deliveryId"),
        "delivery.error" to setOf("requestId", "code"),
    )
    private val uuid = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
    private val slug = Regex("[a-z][a-z0-9-]{2,63}")
    private val digest = Regex("[a-f0-9]{64}")
    private val stableVersion = Regex("(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)")
    private val patterns = mapOf(
        "requestId" to uuid, "deliveryId" to uuid, "credentialId" to uuid, "deviceId" to slug,
        "projectId" to slug, "taskId" to Regex("task-[0-9a-f]{32}"),
        "sourceFingerprint" to digest, "specSha256" to digest, "manifestSha256" to digest, "packageSha256" to digest,
    )

    private fun fail(code: String = "INVALID_REQUEST"): Nothing =
        throw MessageProtocol.ProtocolException(code, "candidate-delivery")

    private fun requireWire(condition: Boolean, code: String = "INVALID_REQUEST") {
        if (!condition) fail(code)
    }

    private fun text(value: JSONObject, key: String): String = value.opt(key) as? String ?: fail()

    private fun number(value: JSONObject, key: String, min: Long, max: Long): Long {
        val result = when (val raw = value.opt(key)) {
            is Int -> raw.toLong()
            is Long -> raw
            else -> fail()
        }
        requireWire(result in min..max)
        return result
    }

    private fun version(value: JSONObject, key: String): String = text(value, key).also {
        requireWire(it.length <= 32 && stableVersion.matches(it))
    }

    fun parse(raw: String): JSONObject = MessageProtocol.parseObject(raw, strict = true).also(::validate)

    private fun validate(value: JSONObject) {
        requireWire(number(value, "deliveryVersion", 0, Int.MAX_VALUE.toLong()) == 1L, "UNSUPPORTED_SCHEMA_VERSION")
        val kind = text(value, "type")
        val keys = fields[kind] ?: fail()
        requireWire(value.keys().asSequence().toSet() == common + keys)
        for (key in keys.intersect(patterns.keys)) {
            requireWire(patterns.getValue(key).matches(text(value, key)))
        }
        if ("buildId" in keys) number(value, "buildId", 1, MAX_BUILD_ID)
        if ("after" in keys) {
            val after = text(value, "after")
            requireWire(after.isEmpty() || uuid.matches(after))
        }
        when (kind) {
            "delivery.receipt" -> validateReceipt(value)
            "delivery.query" -> number(value, "limit", 1, MAX_PAGE)
            "delivery.download" -> {
                val offset = number(value, "offset", 0, LocalPluginStore.MAX_ZIP_BYTES - 1)
                val length = number(value, "length", 1, MAX_SEGMENT_BYTES)
                requireWire(offset + length <= LocalPluginStore.MAX_ZIP_BYTES, "DELIVERY_RANGE_INVALID")
            }
            "delivery.published", "delivery.found" -> receipt(value.optJSONObject("receipt"))
            "delivery.page" -> validatePage(value)
            "delivery.error" -> requireWire(text(value, "code") in errors)
        }
    }

    private fun receipt(value: JSONObject?) {
        if (value == null || value.opt("type") != "delivery.receipt") fail()
        validate(value)
    }

    private fun validateReceipt(value: JSONObject) {
        number(value, "specVersion", 1, Int.MAX_VALUE.toLong())
        number(value, "sizeBytes", 1, LocalPluginStore.MAX_ZIP_BYTES)
        val pluginId = text(value, "pluginId")
        requireWire(LocalPluginStore.isPluginId(pluginId))
        val name = text(value, "pluginName")
        requireWire(name.length in 1..64 && name.all { it.code >= 32 && it.code != 127 })
        val pluginVersion = version(value, "pluginVersion")
        requireWire(text(value, "packageFile") == "$pluginId-$pluginVersion.zip")
        val api = value.optJSONObject("hostApi") ?: fail()
        requireWire(api.keys().asSequence().toSet() == setOf("min", "max"))
        val low = version(api, "min")
        val high = version(api, "max")
        requireWire(low.substringBefore('.') == high.substringBefore('.') && LocalPluginStore.compareVersions(low, high) <= 0)
        val results = value.optJSONObject("checks") ?: fail()
        requireWire(results.keys().asSequence().toSet() == checks && checks.all { results.opt(it) == true })
        val age = DialogueProtocol.micros(text(value, "expiresAt")) - DialogueProtocol.micros(text(value, "offeredAt"))
        requireWire(age in 1..MAX_OFFER_AGE)
    }

    private fun validatePage(page: JSONObject) {
        val receipts = page.optJSONArray("receipts") ?: fail()
        val hasMore = page.opt("hasMore") as? Boolean ?: fail()
        requireWire(receipts.length() <= MAX_PAGE && (receipts.length() > 0 || !hasMore))
        var previous = text(page, "after")
        for (index in 0 until receipts.length()) {
            val item = receipts.optJSONObject(index) ?: fail()
            receipt(item)
            val id = text(item, "deliveryId")
            requireWire(item.opt("projectId") == page.opt("projectId") && id > previous)
            previous = id
        }
    }

    data class Scope(val sourceFingerprint: String, val deviceId: String, val credentialId: String, val projectId: String)

    fun checkActiveReceipt(receipt: JSONObject, scope: Scope, nowMicros: Long) {
        requireWire(receipt.opt("type") == "delivery.receipt")
        requireWire(
            receipt.opt("sourceFingerprint") == scope.sourceFingerprint && receipt.opt("deviceId") == scope.deviceId &&
                receipt.opt("credentialId") == scope.credentialId && receipt.opt("projectId") == scope.projectId,
            "DELIVERY_BINDING_MISMATCH",
        )
        requireWire(nowMicros < DialogueProtocol.micros(text(receipt, "expiresAt")), "DELIVERY_EXPIRED")
    }

    fun checkPage(page: JSONObject, query: JSONObject, scope: Scope, nowMicros: Long) {
        requireWire(page.opt("type") == "delivery.page" && query.opt("type") == "delivery.query")
        requireWire(
            page.opt("projectId") == scope.projectId &&
                listOf("requestId", "projectId", "after").all { page.opt(it) == query.opt(it) },
            "DELIVERY_BINDING_MISMATCH",
        )
        val receipts = page.getJSONArray("receipts")
        val limit = query.getInt("limit")
        requireWire(receipts.length() <= limit && (!page.getBoolean("hasMore") || receipts.length() == limit))
        for (index in 0 until receipts.length()) checkActiveReceipt(receipts.getJSONObject(index), scope, nowMicros)
    }

    fun checkSingleReceipt(response: JSONObject, request: JSONObject, scope: Scope, nowMicros: Long) {
        val keys = when (request.opt("type")) {
            "delivery.publish" -> {
                requireWire(response.opt("type") == "delivery.published")
                target + build
            }
            "delivery.lookup" -> {
                requireWire(response.opt("type") == "delivery.found")
                setOf("deliveryId", "projectId")
            }
            else -> fail()
        }
        val receipt = response.getJSONObject("receipt")
        requireWire(
            response.opt("requestId") == request.opt("requestId") && keys.all { receipt.opt(it) == request.opt(it) },
            "DELIVERY_BINDING_MISMATCH",
        )
        checkActiveReceipt(receipt, scope, nowMicros)
    }

    fun checkSegment(request: JSONObject, receipt: JSONObject) {
        requireWire(request.opt("type") == "delivery.download" && receipt.opt("type") == "delivery.receipt")
        requireWire(
            listOf("deliveryId", "projectId", "packageSha256").all { request.opt(it) == receipt.opt(it) },
            "DELIVERY_BINDING_MISMATCH",
        )
        requireWire(request.getLong("offset") + request.getLong("length") <= receipt.getLong("sizeBytes"), "DELIVERY_RANGE_INVALID")
    }

    data class StateEvent(val eventId: String, val actor: String, val action: String, val reason: String? = null)

    private data class Transition(val actor: String, val from: Set<String>, val to: String)

    private val pending = setOf("offered", "interrupted", "transferred", "verified")
    private val transitions = mapOf(
        "publish" to Transition("desktop", setOf("built"), "offered"),
        "observe" to Transition("android", setOf("unseen"), "offered"),
        "disconnect" to Transition("android", setOf("offered"), "interrupted"),
        "resume" to Transition("android", setOf("interrupted"), "offered"),
        "receive_all" to Transition("android", setOf("offered"), "transferred"),
        "verify" to Transition("android", setOf("transferred"), "verified"),
        "commit_import" to Transition("android", setOf("verified"), "imported"),
        "reject" to Transition("android", pending, "rejected"),
        "cancel" to Transition("android", pending, "cancelled"),
        "expire" to Transition("either", pending, "expired"),
        "revoke" to Transition("either", pending, "revoked"),
    )
    private val rejections = mapOf(
        "package_hash_mismatch" to "transferred", "plugin_identity_mismatch" to "transferred",
        "incompatible_host_api" to "transferred", "invalid_package" to "transferred",
        "user_declined" to "verified", "import_failed" to "verified",
    )

    /** Validate a local event after the caller has established the action's evidence. */
    fun nextState(current: String, event: StateEvent, prior: StateEvent? = null): String {
        val transition = transitions[event.action] ?: fail("INVALID_STATE")
        requireWire(uuid.matches(event.eventId), "INVALID_STATE")
        requireWire(
            event.actor in setOf("desktop", "android") && (transition.actor == "either" || event.actor == transition.actor),
            "INVALID_STATE",
        )
        requireWire(if (event.action == "reject") event.reason in rejections else event.reason == null, "INVALID_STATE")
        if (prior != null) {
            requireWire(event == prior, "EVENT_ID_CONFLICT")
            return current
        }
        requireWire(current in transition.from, "INVALID_STATE")
        if (transition.actor == "either" && event.actor == "desktop") requireWire(current == "offered", "INVALID_STATE")
        if (event.action == "reject") requireWire(current == rejections[event.reason], "INVALID_STATE")
        return transition.to
    }
}
