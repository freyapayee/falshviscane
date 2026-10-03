package com.viscane.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.Menu
import android.view.MenuItem
import android.view.KeyEvent
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.ValueCallback
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.WebResourceResponse
import android.webkit.WebResourceError
import android.graphics.Bitmap
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.activity.OnBackPressedCallback
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import java.io.ByteArrayInputStream
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.preference.PreferenceManager
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var navigation: FarmerNavigation
    private var activeBaseUrl = ""
    private var loadFailed = false

    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private var pendingCameraImageUri: Uri? = null
    private var pendingFileChooserParams: WebChromeClient.FileChooserParams? = null
    private var pendingWebPermissionRequest: PermissionRequest? = null

    private val fileChooserLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val callback = filePathCallback ?: return@registerForActivityResult

            val uris: Array<Uri>? =
                if (result.resultCode == RESULT_OK) {
                    val data = result.data
                    when {
                        data?.clipData != null -> {
                            val clipData = data.clipData!!
                            Array(clipData.itemCount) { index -> clipData.getItemAt(index).uri }
                        }
                        data?.data != null -> arrayOf(data.data!!)
                        pendingCameraImageUri != null -> arrayOf(pendingCameraImageUri!!)
                        else -> null
                    }
                } else {
                    null
                }

            callback.onReceiveValue(uris)
            filePathCallback = null
            pendingCameraImageUri = null
        }

    private val cameraPermissionForChooserLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val params = pendingFileChooserParams
            pendingFileChooserParams = null
            if (params != null) {
                launchFileChooser(params, allowCamera = granted)
                return@registerForActivityResult
            }

            val request = pendingWebPermissionRequest
            pendingWebPermissionRequest = null
            if (request == null) {
                return@registerForActivityResult
            }

            if (granted && navigation.sameOrigin(request.origin.toString()) && navigation.allows(webView.url.orEmpty())) {
                request.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
            } else {
                request.deny()
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        supportActionBar?.hide()

        swipeRefresh = findViewById(R.id.swipeRefresh)
        webView = findViewById(R.id.webView)
        activeBaseUrl = getBaseUrl()
        navigation = FarmerNavigation(activeBaseUrl)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        ViewCompat.setOnApplyWindowInsetsListener(swipeRefresh) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val keyboard = insets.getInsets(WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, keyboard.bottom))
            insets
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                webView.evaluateJavascript("""
                    (() => {
                        const overlay = document.getElementById('camera-overlay');
                        if (overlay?.classList.contains('is-active')) {
                            document.getElementById('close-camera-btn')?.click();
                            return true;
                        }
                        return false;
                    })()
                """.trimIndent()) { closedDialog ->
                    if (closedDialog != "true") {
                        if (webView.canGoBack()) webView.goBack() else finish()
                    }
                }
            }
        })

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.setSupportZoom(true)
        webView.settings.builtInZoomControls = true
        webView.settings.displayZoomControls = false
        webView.settings.allowFileAccess = false
        webView.settings.allowContentAccess = true
        webView.settings.mediaPlaybackRequiresUserGesture = false
        webView.settings.userAgentString += " ViscaneFarmer/1.0"
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false)

        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>,
                fileChooserParams: FileChooserParams
            ): Boolean {
                if (!navigation.allows(this@MainActivity.webView.url.orEmpty())) return false
                this@MainActivity.filePathCallback?.onReceiveValue(null)
                this@MainActivity.filePathCallback = filePathCallback
                pendingCameraImageUri = null

                val wantsCamera = shouldOfferCamera(fileChooserParams)
                if (wantsCamera && !hasCameraPermission()) {
                    pendingFileChooserParams = fileChooserParams
                    cameraPermissionForChooserLauncher.launch(Manifest.permission.CAMERA)
                    return true
                }

                launchFileChooser(fileChooserParams, allowCamera = wantsCamera)
                return true
            }

            override fun onPermissionRequest(request: PermissionRequest) {
                val wantsVideo = request.resources.contains(PermissionRequest.RESOURCE_VIDEO_CAPTURE)
                if (!wantsVideo || !navigation.sameOrigin(request.origin.toString()) || !navigation.allows(webView.url.orEmpty())) {
                    request.deny()
                    return
                }

                if (hasCameraPermission()) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
                    return
                }

                pendingWebPermissionRequest?.deny()
                pendingWebPermissionRequest = request
                cameraPermissionForChooserLauncher.launch(Manifest.permission.CAMERA)
            }

            override fun onPermissionRequestCanceled(request: PermissionRequest) {
                if (pendingWebPermissionRequest == request) pendingWebPermissionRequest = null
            }
        }
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (navigation.allows(request.url.toString())) return false
                if (request.isForMainFrame) Toast.makeText(this@MainActivity, "This app provides farmer services only.", Toast.LENGTH_SHORT).show()
                return true
            }

            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val url = request.url.toString()
                if ((request.isForMainFrame || navigation.sameOrigin(url)) && !navigation.allows(url)) {
                    return WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden", emptyMap(),
                        ByteArrayInputStream("Farmer portal only".toByteArray()))
                }
                return null
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                loadFailed = false
                if (!navigation.allows(url)) {
                    view.stopLoading()
                    view.loadUrl(activeBaseUrl)
                }
            }

            override fun onPageFinished(view: WebView, url: String) {
                swipeRefresh.isRefreshing = false
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) showLoadError()
            }

            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                if (request.isForMainFrame) {
                    val message = if (BuildConfig.DEBUG && response.statusCode == 400) {
                        "The server rejected the request (HTTP 400). Check Django's DJANGO_ALLOWED_HOSTS includes ${request.url.host}, then restart the server."
                    } else {
                        "The server returned HTTP ${response.statusCode}. Try again or contact support."
                    }
                    showLoadError("Server error", message)
                }
            }
        }

        swipeRefresh.setOnRefreshListener { webView.reload() }
        swipeRefresh.setOnChildScrollUpCallback { _, _ -> webView.canScrollVertically(-1) }

        if (savedInstanceState == null || webView.restoreState(savedInstanceState) == null) webView.loadUrl(activeBaseUrl)
    }

    override fun onResume() {
        super.onResume()
        val baseUrl = getBaseUrl()
        if (activeBaseUrl != baseUrl) {
            CookieManager.getInstance().removeAllCookies(null)
            activeBaseUrl = baseUrl
            navigation = FarmerNavigation(baseUrl)
            webView.clearHistory()
            webView.loadUrl(baseUrl)
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        if (BuildConfig.DEBUG) menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    // Keep development server settings accessible without a title bar.
    override fun onKeyUp(keyCode: Int, event: KeyEvent): Boolean {
        if (BuildConfig.DEBUG && keyCode == KeyEvent.KEYCODE_MENU) {
            startActivity(Intent(this, SettingsActivity::class.java))
            return true
        }
        return super.onKeyUp(keyCode, event)
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_settings -> {
                startActivity(Intent(this, SettingsActivity::class.java))
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        webView.saveState(outState)
        super.onSaveInstanceState(outState)
    }

    override fun onDestroy() {
        filePathCallback?.onReceiveValue(null)
        filePathCallback = null
        pendingWebPermissionRequest?.deny()
        pendingWebPermissionRequest = null
        webView.destroy()
        super.onDestroy()
    }

    private fun showLoadError(
        title: String = "Unable to connect",
        message: String = "Check your internet connection and try again."
    ) {
        swipeRefresh.isRefreshing = false
        if (loadFailed || isFinishing) return
        loadFailed = true
        AlertDialog.Builder(this).setTitle(title)
            .setMessage(message)
            .setPositiveButton("Retry") { _, _ -> webView.reload() }
            .setNegativeButton("Close", null).show()
    }

    private fun getBaseUrl(): String {
        if (!BuildConfig.DEBUG) return BuildConfig.FARMER_BASE_URL
        val prefs = PreferenceManager.getDefaultSharedPreferences(this)
        val raw = prefs.getString(SettingsActivity.KEY_BASE_URL, SettingsActivity.DEFAULT_BASE_URL) ?: ""
        val trimmed = raw.trim()
        if (!FarmerNavigation.validOrigin(trimmed, true)) return SettingsActivity.DEFAULT_BASE_URL
        return if (trimmed.endsWith("/")) trimmed else "$trimmed/"
    }

    private fun hasCameraPermission(): Boolean {
        return ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
    }

    private fun shouldOfferCamera(params: WebChromeClient.FileChooserParams): Boolean {
        return params.isCaptureEnabled
    }

    private fun launchFileChooser(params: WebChromeClient.FileChooserParams, allowCamera: Boolean) {
        val acceptTypes = params.acceptTypes?.filter { !it.isNullOrBlank() } ?: emptyList()
        val mimeType =
            when {
                acceptTypes.any { it.startsWith("image/") } -> "image/*"
                acceptTypes.size == 1 -> acceptTypes[0]
                else -> "*/*"
            }

        val wantsMultiple = params.mode == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE

        val contentSelectionIntent = Intent(Intent.ACTION_GET_CONTENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = mimeType
            putExtra(
                Intent.EXTRA_ALLOW_MULTIPLE,
                wantsMultiple
            )
        }

        val cameraIntent =
            if (allowCamera && mimeType.startsWith("image/") && !wantsMultiple) {
                createCameraIntent()
            } else {
                null
            }

        // For an image capture request, go straight to the device camera instead of opening a file manager.
        if (params.isCaptureEnabled && cameraIntent != null) {
            fileChooserLauncher.launch(cameraIntent)
            return
        }

        val initialIntents = mutableListOf<Intent>()
        if (cameraIntent != null) {
            initialIntents.add(cameraIntent)
        }

        val chooser = Intent(Intent.ACTION_CHOOSER).apply {
            putExtra(Intent.EXTRA_INTENT, contentSelectionIntent)
            putExtra(Intent.EXTRA_TITLE, "Select image")
            if (initialIntents.isNotEmpty()) {
                putExtra(Intent.EXTRA_INITIAL_INTENTS, initialIntents.toTypedArray())
            }
        }

        fileChooserLauncher.launch(chooser)
    }

    private fun createCameraIntent(): Intent? {
        val intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
        val resolved = intent.resolveActivity(packageManager) ?: return null

        val imageFile = createTempImageFile() ?: return null
        val uri = FileProvider.getUriForFile(
            this,
            "${packageName}.fileprovider",
            imageFile
        )

        pendingCameraImageUri = uri
        return intent.apply {
            putExtra(MediaStore.EXTRA_OUTPUT, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            setPackage(resolved.packageName)
        }
    }

    private fun createTempImageFile(): File? {
        return try {
            val timeStamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
            val storageDir = getExternalFilesDir(Environment.DIRECTORY_PICTURES) ?: File(cacheDir, "camera").apply { mkdirs() }
            File.createTempFile("VISCANE_${timeStamp}_", ".jpg", storageDir)
        } catch (_: Exception) {
            null
        }
    }
}
