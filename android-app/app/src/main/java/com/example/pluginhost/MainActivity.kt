package com.example.pluginhost

import android.os.Bundle
import android.net.Uri
import android.graphics.Bitmap
import android.webkit.WebViewClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebSettings
import android.webkit.MimeTypeMap
import android.webkit.WebStorage
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.setContent
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.selection.toggleable
import androidx.compose.material3.Button
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.platform.LocalContext
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import androidx.webkit.WebStorageCompat
import java.util.concurrent.Executors
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.io.File
import java.security.MessageDigest
import java.util.WeakHashMap

class MainActivity : ComponentActivity() {
    private var toastGranted by mutableStateOf(false)
    private var toastCount by mutableIntStateOf(0)
    private var bridgeSupported by mutableStateOf(true)
    private var grantedPluginCapabilities by mutableStateOf<Set<String>>(emptySet())
    private var pluginAccessSession = PluginAccessSession()
    private val pluginStore by lazy { LocalPluginStore(this) }
    private val pluginDataStore by lazy { PluginDataStore(this) }
    private val pluginBridges = WeakHashMap<WebView, InstalledPluginBridge>()
    private val io = Executors.newSingleThreadExecutor()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val webViewVersion = if (android.os.Build.VERSION.SDK_INT >= 26) {
            WebView.getCurrentWebViewPackage()?.versionName ?: "unavailable"
        } else {
            "not exposed on API < 26"
        }
        val deviceApi = android.os.Build.VERSION.SDK_INT
        bridgeSupported = WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)
        val webStorageClearSupported = WebViewFeature.isFeatureSupported(WebViewFeature.DELETE_BROWSING_DATA)
        val assetLoader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()
        val bridge = MinimalBridge(
            isToastGranted = { toastGranted },
            showToast = { text ->
                Toast.makeText(this, text, Toast.LENGTH_SHORT).show()
                toastCount += 1
            },
        )
        WebView.setWebContentsDebuggingEnabled(
            applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0,
        )

        setContent {
            MaterialTheme {
                val context = LocalContext.current
                val installed = remember { mutableStateListOf<LocalPluginStore.Installed>() }
                var packageStatus by remember { mutableStateOf("") }
                var candidate by remember { mutableStateOf<LocalPluginStore.Installed?>(null) }
                var activePlugin by remember { mutableStateOf<LocalPluginStore.Installed?>(null) }
                var removeTarget by remember { mutableStateOf<LocalPluginStore.Installed?>(null) }
                var deletePluginData by remember { mutableStateOf(false) }
                val closePlugin = {
                    pluginAccessSession.revokeAll()
                    grantedPluginCapabilities = emptySet()
                    activePlugin = null
                }
                val openPlugin: (LocalPluginStore.Installed) -> Unit = { plugin ->
                    pluginAccessSession.revokeAll()
                    pluginAccessSession = PluginAccessSession()
                    grantedPluginCapabilities = emptySet()
                    activePlugin = plugin
                }
                BackHandler(enabled = activePlugin != null) { closePlugin() }
                val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
                    if (uri != null) {
                        packageStatus = "正在校验插件包…"
                        io.execute {
                            try {
                                val checked = pluginStore.inspect(context, uri)
                                val previous = pluginStore.list().firstOrNull { it.id == checked.id }
                                runOnUiThread {
                                    candidate = checked
                                    val old = previous ?: installed.firstOrNull { it.id == checked.id }
                                    packageStatus = if (old == null) "已通过校验：${checked.name} ${checked.version}" else ""
                                    if (old == null) {
                                        io.execute {
                                            try {
                                                val result = pluginStore.install(checked)
                                                runOnUiThread {
                                                    installed.removeAll { it.id == result.id }
                                                    installed.add(result)
                                                    candidate = null
                                                    packageStatus = "安装完成：${result.name} ${result.version}"
                                                }
                                            } catch (e: LocalPluginStore.ImportFailure) {
                                                runOnUiThread { packageStatus = importError(e.code); candidate = null }
                                            }
                                        }
                                    }
                                }
                            } catch (e: LocalPluginStore.ImportFailure) {
                                runOnUiThread { packageStatus = importError(e.code) }
                            }
                        }
                    }
                }
                LaunchedEffect(Unit) {
                    io.execute {
                        val records = pluginStore.list()
                        runOnUiThread { installed.addAll(records) }
                    }
                }

                val active = activePlugin
                if (active != null) {
                    Column(modifier = Modifier.fillMaxSize()) {
                        Row(modifier = Modifier.fillMaxWidth()) {
                            Button(onClick = { closePlugin() }) { Text("返回插件列表") }
                            Text("${active.name}  ${active.version}", modifier = Modifier.padding(12.dp))
                        }
                        Row(modifier = Modifier.fillMaxWidth()) {
                            if ("data.read" in active.capabilities) {
                                val granted = "data.read" in grantedPluginCapabilities
                                Button(
                                    enabled = bridgeSupported,
                                    onClick = {
                                        pluginAccessSession.setGranted("data.read", !granted)
                                        grantedPluginCapabilities = if (granted) {
                                            grantedPluginCapabilities - "data.read"
                                        } else {
                                            grantedPluginCapabilities + "data.read"
                                        }
                                    },
                                ) { Text(if (granted) "撤销读取授权" else "授权读取") }
                            }
                            if ("data.write" in active.capabilities) {
                                val granted = "data.write" in grantedPluginCapabilities
                                Button(
                                    enabled = bridgeSupported,
                                    onClick = {
                                        pluginAccessSession.setGranted("data.write", !granted)
                                        grantedPluginCapabilities = if (granted) {
                                            grantedPluginCapabilities - "data.write"
                                        } else {
                                            grantedPluginCapabilities + "data.write"
                                        }
                                    },
                                ) { Text(if (granted) "撤销写入授权" else "授权写入") }
                            }
                        }
                        Text(
                            if (bridgeSupported) "仅授予清单已声明的能力；授权只在本次打开期间有效。返回列表会撤销全部授权。"
                            else "当前 WebView 不支持安全桥接，插件数据能力已关闭。",
                            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                        )
                        AndroidView(
                            modifier = Modifier.fillMaxSize(),
                            factory = { viewContext -> createPluginWebView(viewContext, active) },
                            onRelease = { view ->
                                pluginBridges.remove(view)?.detach(view)
                                view.stopLoading()
                                view.destroy()
                            },
                        )
                    }
                } else {
                    Column(modifier = Modifier.fillMaxSize()) {
                        Row(modifier = Modifier.fillMaxWidth()) {
                            Text("本地插件", modifier = Modifier.padding(8.dp).weight(1f))
                        Button(onClick = {
                            picker.launch(arrayOf("application/zip", "application/x-zip-compressed", "application/octet-stream"))
                        }) { Text("导入 ZIP") }
                        }
                        if (packageStatus.isNotEmpty()) {
                            Text(packageStatus, modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp))
                        }
                        if (installed.isEmpty()) Text("暂无已安装插件", modifier = Modifier.padding(horizontal = 12.dp))
                        LazyColumn(modifier = Modifier.fillMaxWidth().weight(1f)) {
                            items(installed, key = { it.id }) { plugin ->
                                Row(modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp)) {
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("${plugin.name}  ${plugin.version}")
                                        Text(plugin.id)
                                    }
                                    Button(onClick = { openPlugin(plugin) }) { Text("打开") }
                                    Button(onClick = { deletePluginData = false; removeTarget = plugin }) { Text("移除") }
                                }
                            }
                        }
                        Text("桥接实验：org.example.bridge-demo / ui.toast", modifier = Modifier.padding(8.dp))
                        Row(modifier = Modifier.fillMaxWidth()) {
                            Button(onClick = { toastGranted = !toastGranted }, enabled = bridgeSupported) {
                                Text(if (toastGranted) "撤销提示授权" else "授权提示")
                            }
                            Text("已显示提示：$toastCount", modifier = Modifier.padding(8.dp))
                        }
                        Text(
                            if (bridgeSupported) "默认拒绝；授权仅在本次 Activity 有效" else "当前 WebView 不支持安全桥接，已关闭能力",
                            modifier = Modifier.padding(8.dp),
                        )
                        AndroidView(
                            modifier = Modifier.fillMaxWidth().weight(1f),
                            factory = { viewContext ->
                                WebView(viewContext).apply {
                                    configurePluginWebSettings()
                                    webViewClient = HostWebViewClient(assetLoader)
                                    bridgeSupported = bridge.attach(this)
                                    val url = Uri.Builder()
                                        .scheme("https")
                                        .authority("appassets.androidplatform.net")
                                        .path("/assets/test/index.html")
                                        .appendQueryParameter("webview", webViewVersion)
                                        .appendQueryParameter("api", deviceApi.toString())
                                        .build()
                                    loadUrl(url.toString())
                                }
                            },
                            onRelease = { view ->
                                if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
                                    WebViewCompat.removeWebMessageListener(view, MinimalBridge.OBJECT_NAME)
                                }
                                view.stopLoading()
                                view.destroy()
                            },
                        )
                    }
                }

                candidate?.let { checked ->
                    val old = installed.firstOrNull { it.id == checked.id }
                    if (old != null) AlertDialog(
                        onDismissRequest = { pluginStore.discard(checked); candidate = null; packageStatus = "已取消更新" },
                        title = { Text("确认更新插件") },
                        text = { Text("${checked.id}\n${old.version} → ${checked.version}\n数据模型兼容性检查已通过，现有插件将在切换成功后替换。") },
                        confirmButton = { Button(onClick = {
                            candidate = null; packageStatus = "正在安装更新…"
                            io.execute {
                                try {
                                    val result = pluginStore.install(checked)
                                    runOnUiThread {
                                        installed.removeAll { it.id == result.id }
                                        installed.add(result)
                                        packageStatus = "更新完成：${result.name} ${result.version}"
                                    }
                                } catch (e: LocalPluginStore.ImportFailure) {
                                    runOnUiThread { packageStatus = importError(e.code) }
                                }
                            }
                        }) { Text("更新") } },
                        dismissButton = { Button(onClick = { pluginStore.discard(checked); candidate = null; packageStatus = "已取消更新" }) { Text("取消") } },
                    )
                }
                removeTarget?.let { plugin ->
                    AlertDialog(
                        onDismissRequest = { removeTarget = null },
                        title = { Text("移除插件？") },
                        text = {
                            Column {
                                Text("将移除 ${plugin.name}（${plugin.id}）的安装资源。宿主笔记数据和页面来源存储默认保留，可在以后重新安装同一 ID 的插件时继续使用。")
                                Row(
                                    modifier = Modifier.fillMaxWidth().toggleable(
                                        value = deletePluginData,
                                        enabled = webStorageClearSupported,
                                        role = Role.Checkbox,
                                        onValueChange = { deletePluginData = it },
                                    ),
                                ) {
                                    Checkbox(
                                        checked = deletePluginData,
                                        enabled = webStorageClearSupported,
                                        onCheckedChange = null,
                                    )
                                    Text("同时永久删除此插件的宿主数据和页面来源存储", modifier = Modifier.padding(top = 12.dp))
                                }
                                if (!webStorageClearSupported) Text("当前 WebView 不支持按来源清理页面数据，删除选项不可用。")
                                if (deletePluginData) Text("此操作不可恢复；只有在安装资源移除成功后才会删除数据。")
                            }
                        },
                        confirmButton = {
                            Button(onClick = {
                                removeTarget = null
                                packageStatus = "正在移除 ${plugin.name}…"
                                val deleteData = deletePluginData
                                io.execute {
                                    val removed = pluginStore.remove(plugin.id)
                                    val dataDeleted = removed && deleteData && runCatching {
                                        deletePluginOriginStorage(plugin.id).also { originDeleted ->
                                            if (originDeleted) pluginDataStore.deletePluginData(plugin.id)
                                        }
                                    }.getOrDefault(false)
                                    runOnUiThread {
                                        if (removed) {
                                            installed.removeAll { it.id == plugin.id }
                                            packageStatus = when {
                                                !deleteData -> "已移除安装资源，插件数据已保留：${plugin.name}"
                                                dataDeleted -> "已移除安装资源并永久删除插件数据：${plugin.name}"
                                                else -> "安装资源已移除，但页面来源数据清理未完成；宿主数据仍保留，可重新安装后重试。"
                                            }
                                        } else {
                                            packageStatus = "移除失败，插件仍保留。"
                                        }
                                    }
                                }
                            }) { Text("移除安装资源") }
                        },
                        dismissButton = { Button(onClick = { removeTarget = null; deletePluginData = false }) { Text("取消") } },
                    )
                }
            }
        }
    }

    override fun onDestroy() {
        pluginDataStore.close()
        io.shutdownNow()
        super.onDestroy()
    }

    private fun deletePluginOriginStorage(pluginId: String): Boolean {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.DELETE_BROWSING_DATA)) return false
        val completed = CountDownLatch(1)
        val succeeded = AtomicBoolean(false)
        runOnUiThread {
            runCatching {
                WebStorageCompat.deleteBrowsingDataForSite(
                    WebStorage.getInstance(), pluginOrigin(pluginId), Runnable {
                        succeeded.set(true)
                        completed.countDown()
                    },
                )
            }.onFailure { completed.countDown() }
        }
        return runCatching { completed.await(30, TimeUnit.SECONDS) && succeeded.get() }.getOrDefault(false)
    }

    private fun importError(code: String) = when (code) {
        "PACKAGE_CORRUPT" -> "插件包损坏或格式不支持。"
        "PACKAGE_LIMIT_EXCEEDED" -> "插件包超过大小或解压限制。"
        "PACKAGE_ENTRY_INVALID" -> "插件包包含不允许的路径或文件类型。"
        "PACKAGE_DUPLICATE_ENTRY" -> "插件包包含重复文件。"
        "MANIFEST_MISSING" -> "插件包缺少根目录 manifest.json。"
        "MANIFEST_INVALID" -> "插件清单无效。"
        "MANIFEST_VERSION_UNSUPPORTED" -> "插件清单版本暂不支持。"
        "INCOMPATIBLE_HOST_API" -> "插件要求的宿主接口版本不兼容。"
        "CAPABILITY_UNKNOWN" -> "插件声明了未知能力。"
        "RESOURCE_MISSING" -> "插件缺少清单声明的资源。"
        "RESOURCE_UNDECLARED" -> "插件包含未声明的资源。"
        "RESOURCE_HASH_MISMATCH" -> "插件资源摘要校验失败。"
        "RESOURCE_REFERENCE_INVALID" -> "插件页面引用了缺失或外部资源。"
        "PLUGIN_VERSION_NOT_NEWER" -> "已安装相同或更新版本，未覆盖现有插件。"
        "DATA_MODEL_INCOMPATIBLE" -> "新版本的数据模型不兼容，现有插件保持不变。"
        else -> "插件导入失败（$code）。"
    }

    private fun createPluginWebView(
        context: android.content.Context,
        plugin: LocalPluginStore.Installed,
    ): WebView = WebView(context).apply {
        configurePluginWebSettings()
        webViewClient = PluginWebViewClient(plugin)
        val bridge = InstalledPluginBridge(plugin, pluginDataStore, pluginAccessSession)
        pluginBridges[this] = bridge
        bridgeSupported = bridge.attach(this)
        val entry = plugin.entry.split('/').joinToString("/") { Uri.encode(it) }
        loadUrl("${pluginOrigin(plugin.id)}/plugin/$entry")
    }

    private fun WebView.configurePluginWebSettings() {
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.allowFileAccessFromFileURLs = false
        settings.allowUniversalAccessFromFileURLs = false
        settings.setSupportMultipleWindows(false)
        settings.mediaPlaybackRequiresUserGesture = true
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        settings.javaScriptCanOpenWindowsAutomatically = false
        settings.cacheMode = WebSettings.LOAD_NO_CACHE
    }

    private class PluginWebViewClient(
        private val plugin: LocalPluginStore.Installed,
    ) : WebViewClient() {
        private val origin = pluginOrigin(plugin.id)
        private val host = Uri.parse(origin).host.orEmpty()
        private val allowedPaths = plugin.resources.toSet()
        @Volatile private var lastGoodUrl: String? = null
        private var pendingGoodUrl: String? = null
        private val assetLoader = WebViewAssetLoader.Builder()
            .setDomain(host)
            .addPathHandler("/plugin/", WebViewAssetLoader.PathHandler(::serve))
            .build()
        private var errorPageShown = false

        override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse =
            intercept(request.url)

        @Suppress("DEPRECATION")
        override fun shouldInterceptRequest(view: WebView, url: String): WebResourceResponse =
            intercept(Uri.parse(url))

        private fun intercept(uri: Uri): WebResourceResponse {
            if (!isPluginOrigin(uri)) return response(403, "Forbidden", "Blocked")
            val path = uri.encodedPath.orEmpty().removePrefix("/plugin/")
            if (path !in allowedPaths) return response(404, "Not Found", "Plugin resource not found")
            return assetLoader.shouldInterceptRequest(uri) ?: response(404, "Not Found", "Plugin resource not found")
        }

        private fun serve(path: String): WebResourceResponse? {
            if (path !in allowedPaths) return null
            val rootPath = plugin.directory.canonicalFile.path + File.separator
            val file = File(plugin.directory, path).canonicalFile
            if (!file.path.startsWith(rootPath) || !file.isFile) return null
            val extension = file.extension.lowercase()
            val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension) ?: when (extension) {
                "js" -> "text/javascript"
                "html" -> "text/html"
                "css" -> "text/css"
                "json" -> "application/json"
                else -> "application/octet-stream"
            }
            val headers = mapOf(
                "Content-Security-Policy" to PLUGIN_CSP,
                "X-Content-Type-Options" to "nosniff",
                "Cache-Control" to "no-store",
            )
            return WebResourceResponse(mime, "UTF-8", 200, "OK", headers, file.inputStream())
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            if (isPluginOrigin(request.url)) return false
            if (request.isForMainFrame) showErrorPage(view, "已拦截离开插件来源的页面跳转")
            return true
        }

        @Suppress("DEPRECATION")
        override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
            val uri = Uri.parse(url)
            if (isPluginOrigin(uri)) return false
            showErrorPage(view, "已拦截离开插件来源的页面跳转")
            return true
        }

        private fun isPluginOrigin(uri: Uri): Boolean =
            uri.scheme == "https" && uri.host == host && uri.port in setOf(-1, 443) && uri.userInfo == null &&
                uri.encodedPath.orEmpty().startsWith("/plugin/")

        private fun isPluginResource(uri: Uri): Boolean = isPluginOrigin(uri) &&
            uri.encodedPath.orEmpty().removePrefix("/plugin/") in allowedPaths

        private fun isPluginHtml(uri: Uri): Boolean {
            if (!isPluginResource(uri)) return false
            val path = uri.encodedPath.orEmpty().removePrefix("/plugin/")
            if (!path.endsWith(".html")) return false
            val rootPath = plugin.directory.canonicalFile.path + File.separator
            val file = File(plugin.directory, path).canonicalFile
            return file.path.startsWith(rootPath) && file.isFile
        }

        override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
            val uri = Uri.parse(url)
            if (isPluginHtml(uri)) {
                pendingGoodUrl = url
                errorPageShown = false
            }
            super.onPageStarted(view, url, favicon)
        }

        override fun onPageFinished(view: WebView, url: String) {
            if (pendingGoodUrl == url && !errorPageShown) lastGoodUrl = url
            super.onPageFinished(view, url)
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame) showErrorPage(view, error.description?.toString())
            super.onReceivedError(view, request, error)
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, error: WebResourceResponse) {
            if (request.isForMainFrame) showErrorPage(view, "资源加载失败（HTTP ${error.statusCode}）")
            super.onReceivedHttpError(view, request, error)
        }

        private fun showErrorPage(view: WebView, description: String?) {
            if (errorPageShown) return
            errorPageShown = true
            val message = (description ?: "未知加载错误")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")
            val entry = plugin.entry.split('/').joinToString("/") { Uri.encode(it) }
            val recoveryUrl = (lastGoodUrl ?: "$origin/plugin/$entry")
                .replace("&", "&amp;").replace("\"", "&quot;")
            view.loadDataWithBaseURL(
                "$origin/",
                ERROR_PAGE_TEMPLATE.replace("{{recovery}}", recoveryUrl).replace("{{message}}", message),
                "text/html",
                "UTF-8",
                null,
            )
        }

        private fun response(status: Int, reason: String, body: String) = WebResourceResponse(
            "text/plain", "UTF-8", status, reason,
            mapOf("Cache-Control" to "no-store", "X-Content-Type-Options" to "nosniff"),
            body.byteInputStream(),
        )

        companion object {
            private const val PLUGIN_CSP = "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; media-src 'self'; connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
            private const val ERROR_PAGE_TEMPLATE = """
                <!doctype html>
                <html lang="zh-CN"><meta charset="utf-8">
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <title>插件页面加载失败</title>
                <style>body{font:16px system-ui,sans-serif;margin:0;padding:2rem;color:#17212b;background:#fff4f2}main{max-width:42rem;margin:auto;padding:1.5rem;background:white;border:1px solid #e5aaa0;border-radius:8px}h1{font-size:1.4rem;margin-top:0;color:#a6291e}code{overflow-wrap:anywhere}</style>
                <main><h1>页面加载失败</h1><p>宿主未能加载插件资源，请检查包内容后重试。</p><p>错误信息：<code>{{message}}</code></p><a id="error-recovery" href="{{recovery}}">返回上一页</a></main>
                </html>
            """
        }
    }

    private companion object {
        fun pluginOrigin(id: String): String {
            require(LocalPluginStore.isPluginId(id))
            val digest = MessageDigest.getInstance("SHA-256").digest(id.toByteArray(Charsets.UTF_8))
            val token = digest.joinToString("") { "%02x".format(it) }
            return "https://p-$token.test"
        }
    }

    @Suppress("DEPRECATION")
    private class HostWebViewClient(
        private val assetLoader: WebViewAssetLoader,
    ) : WebViewClient() {
        @Volatile private var lastGoodUrl: String? = null
        private var pendingGoodUrl: String? = null
        private var errorPageShown = false

        override fun shouldInterceptRequest(
            view: WebView,
            request: WebResourceRequest,
        ) = intercept(request.url)

        override fun shouldInterceptRequest(view: WebView, url: String) =
            intercept(Uri.parse(url))

        private fun intercept(uri: Uri): WebResourceResponse? {
            if (!isAppAssetUrl(uri)) {
                return WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden", emptyMap(), "Blocked".byteInputStream())
            }
            val response = assetLoader.shouldInterceptRequest(uri)
            if (uri.host == ASSET_HOST && (!uri.path.orEmpty().startsWith(ASSET_PATH) || response == null)) {
                return WebResourceResponse(
                    "text/plain",
                    "UTF-8",
                    404,
                    "Not Found",
                    emptyMap(),
                    "Requested app resource was not found".byteInputStream(),
                )
            }
            if (response != null) {
                val scriptPolicy = if (uri.path.orEmpty().startsWith("/assets/test/bridge/")) "'self'" else "'self' 'unsafe-inline'"
                response.responseHeaders = mapOf(
                    "Content-Security-Policy" to "default-src 'none'; script-src $scriptPolicy; style-src $scriptPolicy; frame-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'",
                    "X-Content-Type-Options" to "nosniff",
                    "Cache-Control" to "no-store",
                )
            }
            return response
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            if (isAppAssetUrl(request.url)) return false
            showErrorPage(view, "已拦截非应用内页面跳转")
            return true
        }

        override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
            val uri = Uri.parse(url)
            if (isAppAssetUrl(uri)) return false
            showErrorPage(view, "已拦截非应用内页面跳转")
            return true
        }

        private fun isAppAssetUrl(uri: Uri) =
            uri.scheme == "https" && uri.host == ASSET_HOST && uri.port in setOf(-1, 443) &&
                uri.userInfo == null && uri.encodedPath in ALLOWED_RESOURCES

        override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
            val uri = Uri.parse(url)
            if (uri.encodedPath.orEmpty().endsWith(".html") && isAppAssetUrl(uri) && assetExists(view, uri)) {
                pendingGoodUrl = url
                errorPageShown = false
            }
            super.onPageStarted(view, url, favicon)
        }

        override fun onPageFinished(view: WebView, url: String) {
            if (pendingGoodUrl == url && !errorPageShown) lastGoodUrl = url
            super.onPageFinished(view, url)
        }

        private fun assetExists(view: WebView, uri: Uri): Boolean = runCatching {
            view.context.assets.open(uri.encodedPath.orEmpty().removePrefix(ASSET_PATH)).use { }
            true
        }.getOrDefault(false)

        override fun onReceivedError(
            view: WebView,
            request: WebResourceRequest,
            error: WebResourceError,
        ) {
            if (request.isForMainFrame) showErrorPage(view, error.description?.toString())
            super.onReceivedError(view, request, error)
        }

        override fun onReceivedHttpError(
            view: WebView,
            request: WebResourceRequest,
            errorResponse: WebResourceResponse,
        ) {
            if (request.isForMainFrame) {
                showErrorPage(view, "资源加载失败（HTTP ${errorResponse.statusCode}）")
            }
            super.onReceivedHttpError(view, request, errorResponse)
        }

        private fun showErrorPage(view: WebView, description: String?) {
            if (errorPageShown) return
            errorPageShown = true
            val message = (description ?: "未知加载错误")
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\"", "&quot;")
            val recoveryUrl = (lastGoodUrl ?: "https://$ASSET_HOST$ASSET_PATH/test/index.html")
                .replace("&", "&amp;")
                .replace("\"", "&quot;")
            view.loadDataWithBaseURL(
                "https://appassets.androidplatform.net/",
                ERROR_PAGE_TEMPLATE.replace("{{recovery}}", recoveryUrl).replace("{{message}}", message),
                "text/html",
                "UTF-8",
                null,
            )
        }

        companion object {
            private const val ASSET_HOST = "appassets.androidplatform.net"
            private const val ASSET_PATH = "/assets/"
            private val ALLOWED_RESOURCES = setOf(
                "/assets/test/index.html", "/assets/test/missing.html",
                MinimalBridge.ENTRY_PATH, "/assets/test/bridge/adapters.js",
                "/assets/test/bridge/experiment.js", "/assets/test/bridge/app.css",
                "/assets/test/bridge/frame.html", "/assets/test/bridge/frame.js",
            )

            private const val ERROR_PAGE_TEMPLATE = """
                <!doctype html>
                <html lang="zh-CN"><meta charset="utf-8">
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <title>插件页面加载失败</title>
                <style>body{font:16px system-ui,sans-serif;margin:0;padding:2rem;color:#17212b;background:#fff4f2}main{max-width:42rem;margin:auto;padding:1.5rem;background:white;border:1px solid #e5aaa0;border-radius:8px}h1{font-size:1.4rem;margin-top:0;color:#a6291e}code{overflow-wrap:anywhere}</style>
                <main><h1>页面加载失败</h1><p>宿主未能加载插件资源，请检查包内容后重试。</p><p>错误信息：<code>{{message}}</code></p><a id="error-recovery" href="{{recovery}}">返回上一页</a></main>
                </html>
            """
        }
    }
}
