package com.example.pluginhost

import android.net.Uri
import android.util.Log
import android.util.JsonReader
import android.util.JsonToken
import android.webkit.WebView
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONObject
import java.io.StringReader

/** Stage 0.4 uses a host-owned fixture, never an identity or grant supplied by JavaScript. */
internal class MinimalBridge(
    private val isToastGranted: () -> Boolean,
    private val showToast: (String) -> Unit,
) {
    private val declaredCapabilities = setOf("ui.toast")

    fun attach(view: WebView): Boolean {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return false
        WebViewCompat.addWebMessageListener(view, OBJECT_NAME, setOf(ORIGIN)) {
                sourceView, message, sourceOrigin, isMainFrame, reply ->
            val response = handle(sourceView, message, sourceOrigin, isMainFrame)
            reply.postMessage(response.toString())
        }
        return true
    }

    private fun handle(
        view: WebView,
        message: WebMessageCompat,
        sourceOrigin: Uri,
        isMainFrame: Boolean,
    ): JSONObject {
        var requestId: String? = null
        fun reject(code: String): JSONObject {
            Log.i("BridgeAudit", "result=$code")
            return JSONObject().put("id", requestId ?: JSONObject.NULL).put("ok", false)
                .put("error", JSONObject().put("code", code))
        }

        // Source and frame information comes from WebView, not from the request body.
        if (sourceOrigin.scheme != "https" || sourceOrigin.host != HOST ||
            sourceOrigin.port !in setOf(-1, 443) || sourceOrigin.userInfo != null || !isMainFrame
        ) return reject("SOURCE_NOT_ALLOWED")
        val page = Uri.parse(view.url ?: "")
        if (page.scheme != "https" || page.host != HOST || page.port !in setOf(-1, 443) ||
            page.userInfo != null || page.encodedPath != ENTRY_PATH
        ) return reject("SOURCE_NOT_ALLOWED")
        if (message.type != WebMessageCompat.TYPE_STRING) return reject("INVALID_REQUEST")
        val raw = message.data ?: return reject("INVALID_REQUEST")
        if (raw.toByteArray(Charsets.UTF_8).size > 4096) return reject("REQUEST_TOO_LARGE")
        val request = try {
            // JSONObject alone is lenient; reject duplicate keys, trailing data and deep nesting first.
            JsonReader(StringReader(raw)).use { reader ->
                reader.isLenient = false
                validateJson(reader, 0)
                require(reader.peek() == JsonToken.END_DOCUMENT)
            }
            JSONObject(raw)
        } catch (_: Exception) {
            return reject("INVALID_REQUEST")
        }
        val id = request.opt("id")
        if (id !is String || !id.matches(Regex("[A-Za-z0-9_-]{1,64}"))) {
            return reject("INVALID_REQUEST")
        }
        requestId = id
        if (request.keys().asSequence().toSet() != setOf("id", "apiVersion", "pluginId", "capability", "params")) {
            return reject("INVALID_REQUEST")
        }
        if (request.opt("apiVersion") != "1.0.0") return reject("UNSUPPORTED_API_VERSION")
        if (request.opt("pluginId") != PLUGIN_ID) return reject("PLUGIN_ID_MISMATCH")
        val capability = request.opt("capability")
        if (capability !is String) return reject("INVALID_REQUEST")
        if (capability !in declaredCapabilities) return reject("CAPABILITY_NOT_DECLARED")
        if (!isToastGranted()) return reject("PERMISSION_DENIED")
        val params = request.opt("params") as? JSONObject ?: return reject("INVALID_PARAMS")
        if (params.keys().asSequence().toSet() != setOf("text")) return reject("INVALID_PARAMS")
        val text = params.opt("text")
        if (text !is String || text.isBlank() || text.length > 120 || text.any { it.isISOControl() }) {
            return reject("INVALID_PARAMS")
        }
        showToast(text)
        Log.i("BridgeAudit", "capability=ui.toast result=OK")
        return JSONObject().put("id", id).put("ok", true)
            .put("result", JSONObject().put("environment", "android").put("simulated", false))
    }

    private fun validateJson(reader: JsonReader, depth: Int) {
        require(depth <= 8)
        when (reader.peek()) {
            JsonToken.BEGIN_OBJECT -> {
                reader.beginObject()
                val keys = mutableSetOf<String>()
                while (reader.hasNext()) {
                    require(keys.add(reader.nextName()))
                    validateJson(reader, depth + 1)
                }
                reader.endObject()
            }
            JsonToken.BEGIN_ARRAY -> {
                reader.beginArray()
                while (reader.hasNext()) validateJson(reader, depth + 1)
                reader.endArray()
            }
            JsonToken.STRING, JsonToken.NUMBER -> reader.nextString()
            JsonToken.BOOLEAN -> reader.nextBoolean()
            JsonToken.NULL -> reader.nextNull()
            else -> error("Invalid JSON token")
        }
    }

    companion object {
        const val OBJECT_NAME = "PluginHostBridge"
        const val HOST = "appassets.androidplatform.net"
        const val ORIGIN = "https://$HOST"
        const val ENTRY_PATH = "/assets/test/bridge/index.html"
        const val PLUGIN_ID = "org.example.bridge-demo"
    }
}
