package com.example.pluginhost

import org.json.JSONObject

internal fun verifyDialogueProtocolFixtures(raw: String) {
    val matrix = JSONObject(raw)
    val samples = matrix.getJSONObject("samples")
    fun sample(name: String) = DialogueProtocol.parse(samples.getJSONObject(name).toString())
    fun outcome(action: () -> String): String = try {
        action()
    } catch (failure: MessageProtocol.ProtocolException) {
        failure.code
    }

    samples.keys().forEach { sample(it) }
    val invalid = matrix.getJSONObject("invalid")
    invalid.keys().forEach { name ->
        val fixture = invalid.getJSONObject(name)
        val result = outcome {
            DialogueProtocol.parse(if (fixture.has("raw")) fixture.getString("raw") else fixture.getJSONObject("value").toString())
            "accepted"
        }
        check(result == fixture.optString("code", "INVALID_REQUEST")) { "$name returned $result" }
    }
    val submissions = matrix.getJSONObject("submission")
    submissions.keys().forEach { name ->
        val fixture = submissions.getJSONObject(name)
        val event = sample(fixture.getString("action"))
        fixture.optJSONObject("patch")?.let { patch ->
            patch.keys().forEach { event.put(it, patch.get(it)) }
        }
        val result = outcome {
            DialogueProtocol.checkSubmission(
                DialogueProtocol.parse(event.toString()),
                sample(fixture.getString("head")),
                if (fixture.has("prior")) sample(fixture.getString("prior")) else null,
                fixture.optInt("nextSequence", 1),
                DialogueProtocol.micros(fixture.optString("now", matrix.getString("now"))),
            )
        }
        check(result == fixture.getString("expected")) { "$name returned $result" }
    }
    val transitions = matrix.getJSONArray("transitions")
    for (index in 0 until transitions.length()) {
        val fixture = transitions.getJSONArray(index)
        val result = outcome {
            DialogueProtocol.checkTransition(fixture.getString(0), fixture.getString(1))
            "accepted"
        }
        check(result == if (fixture.getBoolean(2)) "accepted" else "INVALID_STATE")
    }
}
