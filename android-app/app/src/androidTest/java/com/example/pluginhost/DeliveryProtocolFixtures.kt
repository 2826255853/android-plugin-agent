package com.example.pluginhost

import org.json.JSONObject

internal fun verifyDeliveryProtocolFixtures(raw: String) {
    val fixture = JSONObject(raw)
    val samples = fixture.getJSONObject("samples")
    val receipt = DeliveryProtocol.parse(samples.getJSONObject("receipt").toString())
    val query = DeliveryProtocol.parse(samples.getJSONObject("query").toString())

    fun verifyCases(group: String, verify: (JSONObject) -> String) {
        val cases = fixture.getJSONArray(group)
        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            val actual = try {
                verify(case)
            } catch (failure: MessageProtocol.ProtocolException) {
                failure.code
            }
            check(actual == case.getString("expected")) { "${case.getString("name")} returned $actual" }
        }
    }

    fun parseCase(case: JSONObject): JSONObject {
        val sample = JSONObject(samples.getJSONObject(case.getString("sample")).toString())
        case.optJSONObject("set")?.let { patches ->
            for (key in patches.keys()) sample.put(key, patches.get(key))
        }
        if (case.has("remove")) check(sample.remove(case.getString("remove")) != null)
        var encoded = case.optString("raw", sample.toString())
        case.optJSONArray("replace")?.let { replacement ->
            check(replacement.getString(0) in encoded)
            encoded = encoded.replaceFirst(replacement.getString(0), replacement.getString(1))
        }
        return DeliveryProtocol.parse(encoded)
    }

    fun event(value: JSONObject) = DeliveryProtocol.StateEvent(
        value.getString("eventId"), value.getString("actor"), value.getString("action"),
        value.opt("reason") as? String,
    )

    verifyCases("parseCases") { case ->
        parseCase(case)
        "OK"
    }
    verifyCases("contexts") { case ->
        val value = parseCase(case)
        val defaults = fixture.getJSONObject("scope")
        val overrides = case.optJSONObject("scope")
        fun field(key: String) = overrides?.optString(key, defaults.getString(key)) ?: defaults.getString(key)
        val scope = DeliveryProtocol.Scope(field("sourceFingerprint"), field("deviceId"), field("credentialId"), field("projectId"))
        val now = DialogueProtocol.micros(case.optString("now", fixture.getString("now")))
        when (case.getString("sample")) {
            "page" -> DeliveryProtocol.checkPage(value, query, scope, now)
            "published", "found" -> {
                val request = if (case.getString("sample") == "published") "publish" else "lookup"
                DeliveryProtocol.checkSingleReceipt(value, DeliveryProtocol.parse(samples.getJSONObject(request).toString()), scope, now)
            }
            "segment" -> {
                DeliveryProtocol.checkActiveReceipt(receipt, scope, now)
                DeliveryProtocol.checkSegment(value, receipt)
            }
            else -> DeliveryProtocol.checkActiveReceipt(value, scope, now)
        }
        "OK"
    }
    verifyCases("states") { case ->
        DeliveryProtocol.nextState(
            case.getString("current"), event(case.getJSONObject("event")), case.optJSONObject("prior")?.let(::event),
        )
    }
}
