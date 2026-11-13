package com.example.pluginhost

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.JsonReader
import android.util.JsonToken
import android.util.Log
import android.webkit.WebView
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONArray
import org.json.JSONObject
import java.io.StringReader
import java.math.BigDecimal
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** Session-scoped data API for a single verified installation and its dedicated WebView origin. */
internal class InstalledPluginBridge(
    private val plugin: LocalPluginStore.Installed,
    private val dataStore: PluginDataStore,
    private val accessSession: PluginAccessSession,
) {
    private val executor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())
    private val sessionOpen = AtomicBoolean(true)
    private val collections by lazy {
        val declared = plugin.dataModel.optJSONArray("collections") ?: JSONArray()
        (0 until declared.length()).associate { index ->
            val collection = declared.getJSONObject(index)
            collection.getString("id") to collection
        }
    }
    private val origin = pluginOrigin(plugin.id)
    private val host = Uri.parse(origin).host.orEmpty()

    fun attach(view: WebView): Boolean {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return false
        WebViewCompat.addWebMessageListener(view, OBJECT_NAME, setOf(origin)) {
                sourceView, message, sourceOrigin, isMainFrame, reply ->
            if (!sessionOpen.get()) return@addWebMessageListener
            val currentUrl = sourceView.url
            val messageType = message.type
            val raw = message.data
            runCatching {
                executor.execute {
                    val response = handle(currentUrl, messageType, raw, sourceOrigin, isMainFrame)
                    mainHandler.post {
                        runCatching { if (sessionOpen.get()) reply.postMessage(response.toString()) }
                    }
                }
            }
        }
        return true
    }

    fun detach(view: WebView) {
        if (!sessionOpen.compareAndSet(true, false)) return
        accessSession.revokeAll()
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            runCatching { WebViewCompat.removeWebMessageListener(view, OBJECT_NAME) }
        }
        executor.shutdownNow()
    }

    private fun handle(
        currentUrl: String?,
        messageType: Int,
        raw: String?,
        sourceOrigin: Uri,
        isMainFrame: Boolean,
    ): JSONObject {
        var requestId: String? = null
        fun reject(code: String): JSONObject {
            // Audit codes only; never log plugin content, record values, IDs, or parameters.
            Log.i(TAG, "result=$code")
            return JSONObject().put("id", requestId ?: JSONObject.NULL).put("ok", false)
                .put("error", JSONObject().put("code", code))
        }

        if (!sessionOpen.get() || !isAllowedOrigin(sourceOrigin) || !isMainFrame ||
            !isAllowedPage(currentUrl)
        ) return reject("SOURCE_NOT_ALLOWED")
        if (messageType != WebMessageCompat.TYPE_STRING) return reject("INVALID_REQUEST")
        raw ?: return reject("INVALID_REQUEST")
        if (raw.toByteArray(Charsets.UTF_8).size > MAX_REQUEST_BYTES) return reject("REQUEST_TOO_LARGE")
        val request = try {
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
        if (id !is String || !id.matches(Regex("[A-Za-z0-9_-]{1,64}"))) return reject("INVALID_REQUEST")
        requestId = id
        if (request.keys().asSequence().toSet() != REQUEST_KEYS) return reject("INVALID_REQUEST")
        if (request.opt("apiVersion") != API_VERSION) return reject("UNSUPPORTED_API_VERSION")
        if (request.opt("pluginId") != plugin.id) return reject("PLUGIN_ID_MISMATCH")
        val capability = request.opt("capability") as? String ?: return reject("INVALID_REQUEST")
        if (capability !in plugin.capabilities) return reject("CAPABILITY_NOT_DECLARED")
        if (capability !in SUPPORTED_CAPABILITIES) return reject("CAPABILITY_UNAVAILABLE")
        if (!accessSession.isGranted(capability)) return reject("PERMISSION_DENIED")
        val params = request.opt("params") as? JSONObject ?: return reject("INVALID_PARAMS")
        val collectionId = params.opt("collection") as? String ?: return reject("INVALID_PARAMS")
        val collection = collections[collectionId] ?: return reject("COLLECTION_NOT_DECLARED")

        return try {
            when (capability) {
                "data.write" -> {
                    if (params.keys().asSequence().toSet() != setOf("collection", "record")) return reject("INVALID_PARAMS")
                    val supplied = params.opt("record") as? JSONObject ?: return reject("INVALID_PARAMS")
                    val normalized = validateRecord(collection, supplied) ?: return reject("INVALID_PARAMS")
                    val recordId = accessSession.withGrant(capability) {
                        dataStore.insert(plugin.id, collectionId, normalized)
                    } ?: return reject("PERMISSION_DENIED")
                    Log.i(TAG, "capability=data.write result=OK")
                    JSONObject().put("id", id).put("ok", true)
                        .put("result", JSONObject().put("recordId", recordId))
                }
                "data.read" -> {
                    val keys = params.keys().asSequence().toSet()
                    if (!keys.containsAll(setOf("collection")) || keys.any { it !in setOf("collection", "limit", "after") }) {
                        return reject("INVALID_PARAMS")
                    }
                    val limitValue = if (params.has("limit")) params.opt("limit") else DEFAULT_READ_PAGE_SIZE
                    if (limitValue !is Number) return reject("INVALID_PARAMS")
                    val limit = runCatching { BigDecimal(limitValue.toString()).intValueExact() }.getOrNull()
                        ?: return reject("INVALID_PARAMS")
                    if (limit !in 1..PluginDataStore.MAX_READ_PAGE_SIZE) return reject("INVALID_PARAMS")
                    val afterValue = if (params.has("after")) params.opt("after") else "0"
                    if (afterValue !is String || !afterValue.matches(Regex("0|[1-9][0-9]{0,18}"))) return reject("INVALID_PARAMS")
                    val after = afterValue.toLongOrNull()?.takeIf { it >= 0 } ?: return reject("INVALID_PARAMS")
                    val page = accessSession.withGrant(capability) {
                        dataStore.read(plugin.id, collectionId, after, limit)
                    } ?: return reject("PERMISSION_DENIED")
                    Log.i(TAG, "capability=data.read result=OK")
                    JSONObject().put("id", id).put("ok", true)
                        .put("result", JSONObject().put("records", page.records)
                            .put("nextCursor", page.nextCursor ?: JSONObject.NULL))
                }
                else -> reject("CAPABILITY_UNAVAILABLE")
            }
        } catch (failure: PluginDataStore.DataFailure) {
            reject(failure.code)
        } catch (_: Exception) {
            reject("STORAGE_UNAVAILABLE")
        }
    }

    private fun validateRecord(collection: JSONObject, supplied: JSONObject): JSONObject? {
        val fields = collection.optJSONArray("fields") ?: return null
        val definitions = (0 until fields.length()).map { fields.getJSONObject(it) }.associateBy { it.getString("id") }
        if (supplied.keys().asSequence().any { it !in definitions }) return null

        val result = JSONObject()
        for ((fieldId, definition) in definitions) {
            if (!supplied.has(fieldId)) {
                if (definition.optBoolean("required")) return null
                if (definition.has("default")) result.put(fieldId, definition.get("default"))
                continue
            }
            val value = supplied.opt(fieldId)
            if (value == null || value === JSONObject.NULL) return null
            val valid = when (definition.optString("type")) {
                "string" -> {
                    val string = value as? String ?: return null
                    val maxLength = if (definition.has("maxLength")) definition.optInt("maxLength") else Int.MAX_VALUE
                    string.codePointCount(0, string.length) <= maxLength
                }
                "integer" -> {
                    val number = value as? Number ?: return null
                    val integer = runCatching { BigDecimal(number.toString()).toBigIntegerExact() }.getOrNull()
                        ?: return null
                    inNumericBounds(integer.toBigDecimal(), definition)
                }
                "number" -> {
                    val number = value as? Number ?: return null
                    val decimal = runCatching { BigDecimal(number.toString()) }.getOrNull() ?: return null
                    inNumericBounds(decimal, definition)
                }
                "boolean" -> value is Boolean
                else -> false
            }
            if (!valid) return null
            result.put(fieldId, value)
        }
        if (result.toString().toByteArray(Charsets.UTF_8).size > PluginDataStore.MAX_RECORD_BYTES) return null
        return result
    }

    private fun inNumericBounds(value: BigDecimal, definition: JSONObject): Boolean {
        if (definition.has("minimum") && value < BigDecimal(definition.get("minimum").toString())) return false
        if (definition.has("maximum") && value > BigDecimal(definition.get("maximum").toString())) return false
        return true
    }

    private fun isAllowedOrigin(uri: Uri): Boolean =
        uri.scheme == "https" && uri.host == host && uri.port in setOf(-1, 443) && uri.userInfo == null

    private fun isAllowedPage(rawUrl: String?): Boolean {
        val uri = Uri.parse(rawUrl ?: return false)
        if (!isAllowedOrigin(uri)) return false
        val path = uri.encodedPath.orEmpty().removePrefix("/plugin/")
        return path != uri.encodedPath && path in plugin.resources && path.endsWith(".html")
    }

    private fun validateJson(reader: JsonReader, depth: Int) {
        require(depth <= MAX_JSON_DEPTH)
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
        const val API_VERSION = "1.0.0"
        const val MAX_REQUEST_BYTES = 32 * 1024
        private const val MAX_JSON_DEPTH = 8
        private const val DEFAULT_READ_PAGE_SIZE = 100
        private const val TAG = "PluginDataBridge"
        private val REQUEST_KEYS = setOf("id", "apiVersion", "pluginId", "capability", "params")
        private val SUPPORTED_CAPABILITIES = setOf("data.read", "data.write")

        private fun pluginOrigin(id: String): String {
            require(LocalPluginStore.isPluginId(id))
            val digest = java.security.MessageDigest.getInstance("SHA-256").digest(id.toByteArray(Charsets.UTF_8))
            val token = digest.joinToString("") { "%02x".format(it) }
            return "https://p-$token.test"
        }
    }
}

/** Serializes authorization changes with data operations so a completed revocation is immediate. */
internal class PluginAccessSession {
    private val lock = Any()
    private val grants = mutableSetOf<String>()

    fun isGranted(capability: String): Boolean = synchronized(lock) { capability in grants }

    fun setGranted(capability: String, granted: Boolean) = synchronized(lock) {
        if (granted) grants.add(capability) else grants.remove(capability)
        Unit
    }

    fun revokeAll() = synchronized(lock) { grants.clear() }

    fun <T> withGrant(capability: String, operation: () -> T): T? = synchronized(lock) {
        if (capability !in grants) null else operation()
    }
}
