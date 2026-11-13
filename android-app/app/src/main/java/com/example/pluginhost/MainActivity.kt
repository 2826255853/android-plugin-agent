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
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.AlertDialog
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.platform.LocalContext
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import java.util.concurrent.Executors

class MainActivity : ComponentActivity() {
    private var toastGranted by mutableStateOf(false)
    private var toastCount by mutableIntStateOf(0)
    private var bridgeSupported by mutableStateOf(true)
    private val pluginStore by lazy { LocalPluginStore(this) }
    private val io = Executors.newSingleThreadExecutor()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val webViewVersion = if (android.os.Build.VERSION.SDK_INT >= 26) {
            WebView.getCurrentWebViewPackage()?.versionName ?: "unavailable"
        } else {
            "not exposed on API < 26"
        }
        val deviceApi = android.os.Build.VERSION.SDK_INT
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
                val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
                    if (uri != null) {
                        packageStatus = "正在校验插件包…"
                        io.execute {
                            try {
                                val checked = pluginStore.inspect(context, uri)
                                runOnUiThread {
                                    candidate = checked
                                    val old = installed.firstOrNull { it.id == checked.id }
                                    packageStatus = if (old == null) "已通过校验：${checked.name} ${checked.version}" else ""
                                    if (old == null) {
                                        io.execute {
                                            try {
                                                val result = pluginStore.install(checked)
                                                runOnUiThread { installed.removeAll { it.id == result.id }; installed.add(result); candidate = null; packageStatus = "安装完成：${result.name} ${result.version}" }
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
                LaunchedEffect(Unit) { installed.addAll(pluginStore.list()) }
                Column(modifier = Modifier.fillMaxSize()) {
                    Row(modifier = Modifier.fillMaxWidth()) {
                        Text("本地插件", modifier = Modifier.padding(8.dp).weight(1f))
                        Button(onClick = { picker.launch(arrayOf("application/zip", "application/octet-stream")) }) { Text("导入 ZIP") }
                    }
                    installed.forEach { plugin ->
                        Text("${plugin.name}  ${plugin.version}\n${plugin.id}", modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp))
                    }
                    if (installed.isEmpty()) Text("暂无已安装插件", modifier = Modifier.padding(horizontal = 12.dp))
                    if (packageStatus.isNotEmpty()) Text(packageStatus, modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp))
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
                                        runOnUiThread { installed.removeAll { it.id == result.id }; installed.add(result); packageStatus = "更新完成：${result.name} ${result.version}" }
                                    } catch (e: LocalPluginStore.ImportFailure) { runOnUiThread { packageStatus = importError(e.code) } }
                                }
                            }) { Text("更新") } },
                            dismissButton = { Button(onClick = { pluginStore.discard(checked); candidate = null; packageStatus = "已取消更新" }) { Text("取消") } },
                        )
                    }
                    Text("桥接实验：org.example.bridge-demo / ui.toast", modifier = Modifier.padding(8.dp))
                    Row(modifier = Modifier.fillMaxWidth()) {
                        Button(
                            onClick = { toastGranted = !toastGranted },
                            enabled = bridgeSupported,
                        ) {
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
                        factory = { context ->
                            WebView(context).apply {
                                settings.javaScriptEnabled = true
                                settings.domStorageEnabled = true
                                settings.allowFileAccess = false
                                settings.allowContentAccess = false
                                settings.setSupportMultipleWindows(false)
                                settings.mediaPlaybackRequiresUserGesture = true
                                settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
                                settings.javaScriptCanOpenWindowsAutomatically = false

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
        }
    }

    override fun onDestroy() {
        io.shutdownNow()
        super.onDestroy()
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

    @Suppress("DEPRECATION")
    private class HostWebViewClient(
        private val assetLoader: WebViewAssetLoader,
    ) : WebViewClient() {
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
            errorPageShown = false
            super.onPageStarted(view, url, favicon)
        }

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
            view.loadDataWithBaseURL(
                "https://appassets.androidplatform.net/",
                ERROR_PAGE_TEMPLATE.replace("{{message}}", message),
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
                <main><h1>页面加载失败</h1><p>宿主未能加载插件资源，请检查包内容后重试。</p><p>错误信息：<code>{{message}}</code></p><button onclick="history.back()">返回上一页</button></main>
                </html>
            """
        }
    }
}
