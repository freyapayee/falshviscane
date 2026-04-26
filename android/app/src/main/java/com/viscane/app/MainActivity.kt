package com.viscane.app

import android.app.DownloadManager
import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.Menu
import android.view.MenuItem
import android.webkit.CookieManager
import android.webkit.DownloadListener
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.ValueCallback
import android.webkit.WebView
import android.webkit.WebViewClient
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

            if (granted) {
                request.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
            } else {
                request.deny()
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        swipeRefresh = findViewById(R.id.swipeRefresh)
        webView = findViewById(R.id.webView)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.setSupportZoom(true)
        webView.settings.builtInZoomControls = true
        webView.settings.displayZoomControls = false
        webView.settings.allowFileAccess = true
        webView.settings.allowContentAccess = true
        webView.settings.mediaPlaybackRequiresUserGesture = false

        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>,
                fileChooserParams: FileChooserParams
            ): Boolean {
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
                if (!wantsVideo) {
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
        }
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                return false
            }
        }

        webView.setDownloadListener(DownloadListener { url, userAgent, contentDisposition, mimeType, _ ->
            val request = DownloadManager.Request(Uri.parse(url))
            val filename = URLUtil.guessFileName(url, contentDisposition, mimeType)
            val cookies = CookieManager.getInstance().getCookie(url)
            if (!cookies.isNullOrBlank()) {
                request.addRequestHeader("Cookie", cookies)
            }
            request.addRequestHeader("User-Agent", userAgent)
            request.setMimeType(mimeType)
            request.setTitle(filename)
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename)
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)

            val downloadManager = getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
            downloadManager.enqueue(request)
        })

        swipeRefresh.setOnRefreshListener { webView.reload() }

        webView.loadUrl(getBaseUrl())
    }

    override fun onResume() {
        super.onResume()
        val currentUrl = webView.url
        val baseUrl = getBaseUrl()
        if (currentUrl == null || !currentUrl.startsWith(baseUrl)) {
            webView.loadUrl(baseUrl)
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_settings -> {
                startActivity(Intent(this, SettingsActivity::class.java))
                true
            }
            R.id.action_open_external -> {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(getBaseUrl())))
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            @Suppress("DEPRECATION")
            super.onBackPressed()
        }
    }

    private fun getBaseUrl(): String {
        val prefs = PreferenceManager.getDefaultSharedPreferences(this)
        val raw = prefs.getString(SettingsActivity.KEY_BASE_URL, SettingsActivity.DEFAULT_BASE_URL) ?: ""
        val trimmed = raw.trim()
        return if (trimmed.endsWith("/")) trimmed else "$trimmed/"
    }

    private fun hasCameraPermission(): Boolean {
        return ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
    }

    private fun shouldOfferCamera(params: WebChromeClient.FileChooserParams): Boolean {
        if (params.isCaptureEnabled) {
            return true
        }
        val acceptTypes = params.acceptTypes?.filter { !it.isNullOrBlank() } ?: emptyList()
        if (acceptTypes.isEmpty()) {
            return false
        }
        return acceptTypes.any { it.startsWith("image/") }
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
        if ((params.isCaptureEnabled || allowCamera) && cameraIntent != null) {
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
            val storageDir = getExternalFilesDir(Environment.DIRECTORY_PICTURES) ?: cacheDir
            File.createTempFile("VISCANE_${timeStamp}_", ".jpg", storageDir)
        } catch (_: Exception) {
            null
        }
    }
}
