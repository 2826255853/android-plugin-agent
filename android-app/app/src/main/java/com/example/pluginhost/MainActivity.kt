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
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature

class MainActivity : ComponentActivity() {
    private var toastGranted by mutableStateOf(false)
    private var toastCount by mutableIntStateOf(0)
    private var bridgeSupported by mutableStateOf(true)

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
                Column(modifier = Modifier.fillMaxSize()) {
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
