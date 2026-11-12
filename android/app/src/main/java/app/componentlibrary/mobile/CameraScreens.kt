package app.componentlibrary.mobile

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.util.Size
import android.view.MotionEvent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.Camera
import androidx.camera.core.UseCase
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.view.PreviewView
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

private fun cameraPreviewView(context: Context) = PreviewView(context).apply {
    implementationMode = PreviewView.ImplementationMode.COMPATIBLE
    scaleType = PreviewView.ScaleType.FIT_CENTER
}

@Composable
private fun CameraPreview(view: PreviewView, preview: Preview?) {
    val ratio = preview?.resolutionInfo?.let { info ->
        val crop = info.cropRect
        val width = crop.width().toFloat()
        val height = crop.height().toFloat()
        if (width <= 0 || height <= 0) null
        else if (info.rotationDegrees % 180 == 0) width / height else height / width
    } ?: (4f / 3f)
    val heightLimit = minOf(440.dp, LocalConfiguration.current.screenHeightDp.dp * 0.55f)
    BoxWithConstraints(Modifier.fillMaxWidth()) {
        // 取景画面有独立的实测布局高度，后面的文字和按钮按普通内容顺序排列。
        val previewHeight = minOf(maxWidth / ratio, heightLimit)
        AndroidView({ view }, Modifier.fillMaxWidth().height(previewHeight).clipToBounds())
    }
}

@Composable
fun CameraCaptureScreen(
    onCaptured: (File) -> Unit, onCancel: () -> Unit,
    modifier: Modifier = Modifier.fillMaxSize(), enabled: Boolean = true,
) {
    val context = LocalContext.current
    val lifecycle = LocalLifecycleOwner.current
    val previewView = remember { cameraPreviewView(context) }
    val imageCapture = remember {
        ImageCapture.Builder()
            .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
            .setJpegQuality(85)
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setResolutionStrategy(
                        ResolutionStrategy(
                            Size(1920, 1080),
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER,
                        ),
                    )
                    .build(),
            )
            .build()
    }
    val executor = remember { Executors.newSingleThreadExecutor() }
    val mainExecutor = remember { ContextCompat.getMainExecutor(context) }
    var error by remember { mutableStateOf<String?>(null) }
    var capturing by remember { mutableStateOf(false) }
    var provider by remember { mutableStateOf<ProcessCameraProvider?>(null) }
    var previewUseCase by remember { mutableStateOf<Preview?>(null) }

    LaunchedEffect(Unit) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            error = "尚未授予摄像头权限，请在系统设置中允许后重试"
            return@LaunchedEffect
        }
        runCatching {
            val cameraProvider = ProcessCameraProvider.getInstance(context).get()
            val preview = Preview.Builder().build().also { it.surfaceProvider = previewView.surfaceProvider }
            cameraProvider.bindToLifecycle(lifecycle, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture)
            provider = cameraProvider
            previewUseCase = preview
        }.onFailure { error = "无法打开摄像头：${it.message}" }
    }
    DisposableEffect(Unit) {
        onDispose {
            previewUseCase?.let { provider?.unbind(it, imageCapture) }
            executor.shutdown()
        }
    }
    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        error?.let { Text(it, Modifier.padding(12.dp)) }
        CameraPreview(previewView, previewUseCase)
        Button(
            onClick = {
                capturing = true
                error = null
                val file = File(context.cacheDir, "capture-${System.currentTimeMillis()}.jpg")
                DiagnosticLog.record("PHOTO_CAPTURE_START")
                imageCapture.takePicture(
                    ImageCapture.OutputFileOptions.Builder(file).build(), executor,
                    object : ImageCapture.OnImageSavedCallback {
                        override fun onImageSaved(result: ImageCapture.OutputFileResults) {
                            DiagnosticLog.record("PHOTO_CAPTURED", "bytes=${file.length()}")
                            mainExecutor.execute {
                                capturing = false
                                onCaptured(file)
                            }
                        }

                        override fun onError(exception: ImageCaptureException) {
                            DiagnosticLog.recordFailure("PHOTO_CAPTURE_FAILURE", exception)
                            mainExecutor.execute {
                                capturing = false
                                error = exception.message
                            }
                        }
                    },
                )
            }, enabled = enabled && !capturing && provider != null, modifier = Modifier.padding(horizontal = 12.dp),
        ) { Text(if (capturing) "正在拍摄…" else "拍摄并上传") }
        Button(onClick = onCancel, modifier = Modifier.padding(12.dp)) { Text("关闭摄像头") }
    }
}

@Composable
fun BarcodeScannerScreen(
    onCode: (String) -> Unit, onCancel: (() -> Unit)?,
    modifier: Modifier = Modifier.fillMaxSize(), continuous: Boolean = false, enabled: Boolean = true,
    validateCode: ((String) -> String?)? = null,
) {
    val submitCode by rememberUpdatedState(onCode)
    val scanEnabled by rememberUpdatedState(enabled)
    val validate by rememberUpdatedState(validateCode)
    val context = LocalContext.current
    val lifecycle = LocalLifecycleOwner.current
    val previewView = remember { cameraPreviewView(context) }
    val executor = remember { Executors.newSingleThreadExecutor() }
    val mainExecutor = remember { ContextCompat.getMainExecutor(context) }
    val delivered = remember { AtomicBoolean(false) }
    var lastCode by remember { mutableStateOf<String?>(null) }
    var lastRejectedCode by remember { mutableStateOf<String?>(null) }
    var clearFrames by remember { mutableStateOf(0) }
    val processing = remember { AtomicBoolean(false) }
    val receivedFrame = remember { AtomicBoolean(false) }
    val frameCount = remember { AtomicLong(0) }
    val analysisCount = remember { AtomicLong(0) }
    val emptyResultCount = remember { AtomicLong(0) }
    var error by remember { mutableStateOf<String?>(null) }
    var status by remember { mutableStateOf("正在持续识别二维码，请将二维码完整放入画面") }
    var provider by remember { mutableStateOf<ProcessCameraProvider?>(null) }
    var analysisUseCase by remember { mutableStateOf<UseCase?>(null) }
    var previewUseCase by remember { mutableStateOf<Preview?>(null) }
    var boundCamera by remember { mutableStateOf<Camera?>(null) }
    var linearZoom by remember { mutableStateOf(0f) }
    var cameraGranted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED,
        )
    }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        cameraGranted = granted
        DiagnosticLog.record("CAMERA_PERMISSION_RESULT", "granted=$granted")
        if (!granted) error = "尚未授予摄像头权限，请在系统设置中允许后重试"
    }
    val scanner = remember {
        BarcodeScanning.getClient(
            BarcodeScannerOptions.Builder()
                .setBarcodeFormats(
                    Barcode.FORMAT_QR_CODE,
                    Barcode.FORMAT_DATA_MATRIX,
                    Barcode.FORMAT_CODE_128,
                )
                .build(),
        )
    }
    LaunchedEffect(cameraGranted, lifecycle) {
        DiagnosticLog.record("SCANNER_OPEN", "permission=$cameraGranted")
        if (!cameraGranted) {
            DiagnosticLog.record("CAMERA_PERMISSION_REQUEST")
            permissionLauncher.launch(Manifest.permission.CAMERA)
            return@LaunchedEffect
        }
        runCatching {
            DiagnosticLog.record("CAMERA_PROVIDER_WAIT")
            val cameraProvider = ProcessCameraProvider.getInstance(context).get()
            DiagnosticLog.record("CAMERA_PROVIDER_READY", "hasBackCamera=${cameraProvider.hasCamera(CameraSelector.DEFAULT_BACK_CAMERA)}")
            val preview = Preview.Builder().build().also {
                it.surfaceProvider = previewView.surfaceProvider
            }
            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
                .setResolutionSelector(
                    ResolutionSelector.Builder()
                        .setResolutionStrategy(
                            ResolutionStrategy(
                                Size(1280, 720),
                                ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                            ),
                        )
                        .build(),
                )
                .build()
            analysis.setAnalyzer(executor) { proxy ->
                val currentFrame = frameCount.incrementAndGet()
                if (receivedFrame.compareAndSet(false, true)) {
                    DiagnosticLog.record(
                        "ANALYZER_FIRST_FRAME",
                        "size=${proxy.width}x${proxy.height} format=${proxy.format} " +
                            "rotation=${proxy.imageInfo.rotationDegrees}",
                    )
                    mainExecutor.execute {
                        status = "相机分析已启动（${proxy.width}×${proxy.height}），正在识别二维码……"
                    }
                } else if (currentFrame % 30L == 0L) {
                    DiagnosticLog.record(
                        "ANALYZER_FRAME",
                        "count=$currentFrame size=${proxy.width}x${proxy.height} " +
                            "format=${proxy.format} rotation=${proxy.imageInfo.rotationDegrees}",
                    )
                }
                val mediaImage = proxy.image
                if (mediaImage == null || !processing.compareAndSet(false, true)) {
                    if (mediaImage == null) {
                        DiagnosticLog.record("ANALYZER_NULL_IMAGE", "frame=$currentFrame")
                    }
                    proxy.close()
                    return@setAnalyzer
                }
                val currentAnalysis = analysisCount.incrementAndGet()
                if (currentAnalysis <= 3L || currentAnalysis % 30L == 0L) {
                    DiagnosticLog.record("MLKIT_PROCESS_START", "analysis=$currentAnalysis frame=$currentFrame")
                }
                try {
                    scanner.process(
                        InputImage.fromMediaImage(mediaImage, proxy.imageInfo.rotationDegrees),
                    )
                        .addOnSuccessListener(mainExecutor) { codes ->
                            if (codes.isEmpty()) {
                                val emptyCount = emptyResultCount.incrementAndGet()
                                if (emptyCount <= 3L || emptyCount % 30L == 0L) {
                                    DiagnosticLog.record(
                                        "MLKIT_NO_CODE",
                                        "empty=$emptyCount analysis=$currentAnalysis",
                                    )
                                }
                            } else if (!continuous) {
                                DiagnosticLog.record(
                                    "MLKIT_CODES",
                                    "count=${codes.size} formats=${codes.joinToString(",") { it.format.toString() }}",
                                )
                            }
                            val value = codes.asSequence()
                                .mapNotNull { it.rawValue ?: it.displayValue }
                                .filter { it.isNotBlank() }
                                .firstOrNull { candidate ->
                                    // 配对码通过校验后才占用单次提交机会，扫错时继续取景。
                                    if (validate == null) {
                                        true
                                    } else if (!scanEnabled || delivered.get() || candidate == lastRejectedCode) {
                                        false
                                    } else {
                                        val rejection = validate?.invoke(candidate)
                                        if (rejection != null) {
                                            lastRejectedCode = candidate
                                            status = rejection
                                            DiagnosticLog.record("BARCODE_REJECTED", "reason=invalid_pairing_code")
                                        }
                                        rejection == null
                                    }
                                }
                            if (codes.isEmpty() && ++clearFrames >= 5) lastCode = null
                            if (codes.isNotEmpty()) clearFrames = 0
                            if (value != null && scanEnabled && (
                                    if (continuous) value != lastCode else delivered.compareAndSet(false, true)
                                )
                            ) {
                                lastCode = value
                                DiagnosticLog.record(
                                    "BARCODE_DELIVERED",
                                    "length=${value.length} fingerprint=${DiagnosticLog.valueFingerprint(value)}",
                                )
                                status = if (continuous) "已识别，可继续扫描" else "已识别，正在处理……"
                                submitCode(value)
                            } else if (codes.isNotEmpty() && codes.all {
                                    (it.rawValue ?: it.displayValue).isNullOrBlank()
                                }) {
                                DiagnosticLog.record("BARCODE_WITHOUT_TEXT", "count=${codes.size}")
                            }
                        }
                        .addOnFailureListener(mainExecutor) { failure ->
                            DiagnosticLog.recordFailure("MLKIT_FAILURE", failure)
                            error = "二维码识别失败：${failure.message ?: failure.javaClass.simpleName}"
                        }
                        .addOnCompleteListener {
                            processing.set(false)
                            proxy.close()
                        }
                } catch (failure: Exception) {
                    DiagnosticLog.recordFailure("MLKIT_PROCESS_EXCEPTION", failure)
                    processing.set(false)
                    proxy.close()
                    mainExecutor.execute {
                        error = "二维码识别失败：${failure.message ?: failure.javaClass.simpleName}"
                    }
                }
            }
            cameraProvider.unbindAll()
            val camera = cameraProvider.bindToLifecycle(
                lifecycle,
                CameraSelector.DEFAULT_BACK_CAMERA,
                preview,
                analysis,
            )
            previewView.setOnTouchListener { _, event ->
                if (event.action == MotionEvent.ACTION_UP) {
                    val point = previewView.meteringPointFactory.createPoint(event.x, event.y)
                    camera.cameraControl.startFocusAndMetering(
                        FocusMeteringAction.Builder(point).build(),
                    )
                    DiagnosticLog.record(
                        "CAMERA_FOCUS_REQUEST",
                        "x=${event.x.toInt()} y=${event.y.toInt()}",
                    )
                }
                true
            }
            previewView.post {
                if (previewView.width > 0 && previewView.height > 0) {
                    val center = previewView.meteringPointFactory.createPoint(
                        previewView.width / 2f,
                        previewView.height / 2f,
                    )
                    camera.cameraControl.startFocusAndMetering(
                        FocusMeteringAction.Builder(center).build(),
                    )
                    DiagnosticLog.record("CAMERA_CENTER_FOCUS_REQUEST")
                }
            }
            provider = cameraProvider
            analysisUseCase = analysis
            previewUseCase = preview
            boundCamera = camera
            error = null
            DiagnosticLog.record("CAMERA_BOUND", "useCases=preview,imageAnalysis")
        }.onFailure { failure ->
            DiagnosticLog.recordFailure("CAMERA_BIND_FAILURE", failure)
            error = "摄像头分析器启动失败：${failure.message ?: failure.javaClass.simpleName}"
        }
    }
    DisposableEffect(scanner, executor) {
        onDispose {
            DiagnosticLog.record(
                "SCANNER_DISPOSE",
                "frames=${frameCount.get()} analyses=${analysisCount.get()} delivered=${delivered.get()}",
            )
            provider?.unbind(*listOfNotNull(previewUseCase, analysisUseCase).toTypedArray())
            previewView.setOnTouchListener(null)
            scanner.close()
            executor.shutdown()
        }
    }
    val screen = if (continuous) modifier else modifier.safeDrawingPadding().verticalScroll(rememberScrollState())
    Column(screen, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        error?.let { Text(it, Modifier.padding(12.dp)) }
        Text(status, Modifier.padding(12.dp))
        CameraPreview(previewView, previewUseCase)
        if (!continuous) {
            Row(
                Modifier.padding(horizontal = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Button(onClick = {
                    linearZoom = (linearZoom - 0.15f).coerceIn(0f, 1f)
                    boundCamera?.cameraControl?.setLinearZoom(linearZoom)
                }) { Text("缩小") }
                Text("缩放 ${(linearZoom * 100).toInt()}%", Modifier.padding(top = 12.dp))
                Button(onClick = {
                    linearZoom = (linearZoom + 0.15f).coerceIn(0f, 1f)
                    boundCamera?.cameraControl?.setLinearZoom(linearZoom)
                }) { Text("放大") }
            }
            Text("若画面模糊，可点击二维码位置对焦或使用缩放按钮", Modifier.padding(horizontal = 12.dp))
            var copied by remember { mutableStateOf<Int?>(null) }
            Button(
                onClick = { copied = DiagnosticLog.copyToClipboard(context) },
                modifier = Modifier.padding(horizontal = 12.dp),
            ) { Text("复制诊断日志") }
            copied?.let { Text("已复制 $it 个字符，可直接粘贴发送", Modifier.padding(horizontal = 12.dp)) }
        }
        if (onCancel != null) {
            Button(onClick = onCancel, modifier = Modifier.padding(12.dp)) { Text("取消") }
        }
    }
}
