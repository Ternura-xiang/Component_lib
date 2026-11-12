package app.componentlibrary.mobile

import android.Manifest
import android.app.Application
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Build
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.WebSocket
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

data class MobileState(
    val connected: Boolean = false,
    val busy: Boolean = false,
    val message: String = "",
    val sessions: List<JSONObject> = emptyList(),
    val current: JSONObject? = null,
)

class MobileViewModel(application: Application) : AndroidViewModel(application) {
    private val store = CredentialStore(application)
    private val discovery = NsdDiscovery(application)
    private var credential = store.load()
    private var api = credential?.let(::ApiClient)
    private var socket: WebSocket? = null
    private var socketGeneration = 0
    private val mutableState = MutableStateFlow(MobileState())
    val state = mutableState.asStateFlow()

    init {
        credential?.let(::startDiscovery)
        refresh()
        startDeviceMonitor()
    }

    private fun startDiscovery(paired: ServerCredential) {
        discovery.start(paired.serverId) { host, port ->
            viewModelScope.launch {
                val current = credential ?: return@launch
                if (current.deviceId != paired.deviceId || (current.host == host && current.port == port)) return@launch
                val candidate = current.copy(host = host, port = port)
                val candidateApi = ApiClient(candidate)
                try {
                    withContext(Dispatchers.IO) { candidateApi.heartbeat() }
                } catch (failure: CancellationException) {
                    throw failure
                } catch (failure: Exception) {
                    DiagnosticLog.recordFailure("DISCOVERY_CONNECT_FAILURE", failure)
                    return@launch
                }
                if (credential === current) {
                    credential = candidate.also(store::save)
                    api = candidateApi
                    refresh()
                }
            }
        }
    }

    private fun work(block: suspend () -> Unit) {
        viewModelScope.launch {
            if (mutableState.value.busy) return@launch
            mutableState.value = mutableState.value.copy(busy = true, message = "")
            runCatching { block() }
                .onFailure { mutableState.value = mutableState.value.copy(message = it.message ?: "操作失败") }
            mutableState.value = mutableState.value.copy(busy = false)
        }
    }

    fun pair(payload: String) = work {
        val name = Settings.Global.getString(getApplication<Application>().contentResolver, "device_name")
            ?: android.os.Build.MODEL
        val paired = withContext(Dispatchers.IO) {
            runCatching { ApiClient.pair(payload, name) }
                .onFailure { DiagnosticLog.recordFailure("PAIRING_FAILURE", it) }
                .getOrThrow()
        }
        store.save(paired)
        credential = paired
        api = ApiClient(paired)
        startDiscovery(paired)
        refreshConnection()
    }

    fun forget() {
        socketGeneration += 1
        socket?.cancel()
        socket = null
        discovery.stop()
        store.clear()
        credential = null
        api = null
        mutableState.value = MobileState(message = "已移除本机连接信息")
    }

    fun refresh() = work { refreshConnection() }

    private suspend fun refreshConnection() {
        val active = api ?: return
        val pair = withContext(Dispatchers.IO) {
            active.heartbeat()
            active.sessions()
        }
        mutableState.value = mutableState.value.copy(
            connected = true,
            sessions = (0 until pair.length()).map { pair.getJSONObject(it) },
        )
    }

    fun create(kind: String) = work {
        val created = withContext(Dispatchers.IO) { requireNotNull(api).createSession(kind) }
        open(created)
    }

    fun open(session: JSONObject) {
        mutableState.value = mutableState.value.copy(current = session)
        connectEvents(session.getString("session_id"))
    }

    private fun acceptSnapshot(snapshot: JSONObject) {
        val current = mutableState.value.current ?: return
        if (current.getString("session_id") != snapshot.getString("session_id") ||
            current.getInt("revision") > snapshot.getInt("revision")) return
        mutableState.value = mutableState.value.copy(current = snapshot, connected = true)
    }

    private fun connectEvents(sessionId: String) {
        val generation = ++socketGeneration
        socket?.cancel()
        DiagnosticLog.record("WEBSOCKET_OPEN", "session=${sessionId.take(8)} generation=$generation")
        socket = api?.events(
            sessionId,
            { snapshot ->
                viewModelScope.launch {
                    if (generation == socketGeneration) acceptSnapshot(snapshot)
                }
            },
            { error ->
                viewModelScope.launch {
                    if (generation == socketGeneration) {
                        socket = null
                        DiagnosticLog.recordFailure("SESSION_EVENTS_RECONNECT", error)
                    }
                }
            },
        )
    }

    fun closeSession() {
        val current = mutableState.value.current
        val kind = current?.optJSONObject("snapshot")?.optString("kind")
        if (current != null && kind in listOf("ISSUE", "RECEIVE") && current.optString("state") != "CANCELLED") {
            command("CANCEL", after = { detachSession() })
            return
        }
        detachSession()
    }

    private fun detachSession() {
        socketGeneration += 1
        socket?.cancel()
        socket = null
        mutableState.value = mutableState.value.copy(current = null)
    }

    private fun startDeviceMonitor() {
        viewModelScope.launch {
            while (isActive) {
                delay(3_000)
                val active = api ?: continue
                val sessionId = mutableState.value.current?.optString("session_id").orEmpty()
                val pollSnapshot = socket == null
                runCatching {
                    withContext(Dispatchers.IO) {
                        active.heartbeat(sessionId)
                        active.sessions() to if (pollSnapshot && sessionId.isNotEmpty()) active.session(sessionId) else null
                    }
                }.onSuccess { (sessions, snapshot) ->
                    if (snapshot != null) {
                        acceptSnapshot(snapshot)
                        if (socket == null && mutableState.value.current?.optString("session_id") == sessionId &&
                            snapshot.optString("state") != "CANCELLED") connectEvents(sessionId)
                    }
                    val values = (0 until sessions.length()).map { sessions.getJSONObject(it) }
                    mutableState.value = mutableState.value.copy(connected = true, sessions = values)
                    val deviceId = credential?.deviceId
                    val requested = values.firstOrNull { session ->
                        val form = session.getJSONObject("snapshot").optJSONObject("form")
                        form?.optBoolean("camera_only") == true &&
                            form.optString("target_device_id") == deviceId
                    }
                    if (!mutableState.value.busy && mutableState.value.current == null && requested != null) {
                        DiagnosticLog.record(
                            "REMOTE_CAMERA_REQUEST",
                            "session=${requested.getString("session_id").take(8)}",
                        )
                        open(requested)
                    }
                }.onFailure { failure ->
                    DiagnosticLog.recordFailure("DEVICE_MONITOR_FAILURE", failure)
                    mutableState.value = mutableState.value.copy(connected = false)
                }
            }
        }
    }

    fun command(name: String, payload: JSONObject = JSONObject(), after: ((JSONObject) -> Unit)? = null) = work {
        val current = requireNotNull(mutableState.value.current)
        val sessionId = current.getString("session_id")
        val revision = current.getInt("revision")
        DiagnosticLog.record(
            "SESSION_COMMAND_START",
            "command=$name session=${sessionId.take(8)} revision=$revision",
        )
        val result = try {
            withContext(Dispatchers.IO) {
                requireNotNull(api).command(sessionId, revision, name, payload)
            }
        } catch (failure: Exception) {
            DiagnosticLog.recordFailure("SESSION_COMMAND_FAILURE", failure)
            if (failure is ApiException && failure.status == 409) {
                acceptSnapshot(withContext(Dispatchers.IO) { requireNotNull(api).session(sessionId) })
            }
            throw failure
        }
        DiagnosticLog.record(
            "SESSION_COMMAND_SUCCESS",
            "command=$name revision=${result.optInt("revision", -1)} state=${result.optString("state")}",
        )
        acceptSnapshot(result)
        after?.invoke(result)
    }

    fun updateAndConfirm(form: JSONObject) = command("UPDATE_AND_CONFIRM", form)

    fun upload(file: File) = work {
        val current = requireNotNull(mutableState.value.current)
        val startedAt = System.nanoTime()
        DiagnosticLog.record("IMAGE_UPLOAD_START", "bytes=${file.length()}")
        val result = try {
            withContext(Dispatchers.IO) {
                requireNotNull(api).uploadImage(current.getString("session_id"), file)
            }
        } finally {
            file.delete()
        }
        val elapsedMs = (System.nanoTime() - startedAt) / 1_000_000
        DiagnosticLog.record("IMAGE_UPLOAD_OCR_COMPLETE", "duration_ms=$elapsedMs")
        acceptSnapshot(result)
    }

    override fun onCleared() {
        socket?.cancel()
        discovery.stop()
        super.onCleared()
    }
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        DiagnosticLog.initialize(applicationContext)
        setContent {
            MaterialTheme { MobileApp() }
        }
    }
}

@Composable
fun MobileApp(model: MobileViewModel = viewModel()) {
    val state by model.state.collectAsState()
    var scannerMode by remember { mutableStateOf<String?>(null) }
    var cameraMode by remember { mutableStateOf(false) }
    var pendingScan by remember { mutableStateOf<String?>(null) }
    val context = LocalContext.current
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {}
    LaunchedEffect(Unit) {
        val required = buildList {
            if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) add(Manifest.permission.CAMERA)
            if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(context, Manifest.permission.NEARBY_WIFI_DEVICES) != PackageManager.PERMISSION_GRANTED) add(Manifest.permission.NEARBY_WIFI_DEVICES)
        }
        if (required.isNotEmpty()) permission.launch(required.toTypedArray())
    }
    val currentSession = state.current
    val snapshot = currentSession?.optJSONObject("snapshot")
    val kind = snapshot?.optString("kind")
    val inventory = kind == "ISSUE" || kind == "RECEIVE"
    BackHandler(currentSession != null && !state.busy) { model.closeSession() }
    LaunchedEffect(currentSession?.optString("session_id"), currentSession?.optString("state")) {
        if (currentSession?.optString("state") == "CANCELLED") {
            DiagnosticLog.record("SESSION_CANCELLED_BY_DESKTOP")
            scannerMode = null
            cameraMode = false
            pendingScan = null
            model.closeSession()
        }
    }
    LaunchedEffect(currentSession?.optString("session_id")) {
        if (currentSession == null) {
            if (scannerMode == "SESSION") scannerMode = null
            cameraMode = false
            pendingScan = null
        }
        if (currentSession?.optString("state") == "CANCELLED") return@LaunchedEffect
        val snapshot = currentSession?.optJSONObject("snapshot") ?: return@LaunchedEffect
        val form = snapshot.optJSONObject("form") ?: return@LaunchedEffect
        if (form.optBoolean("camera_only") || form.optBoolean("camera_stream")) {
            when (snapshot.optString("kind")) {
                "RECEIVE" -> cameraMode = true
                "ISSUE" -> scannerMode = "SESSION"
            }
        }
    }
    if (scannerMode == "PAIR" || (scannerMode == "SESSION" && kind == "BOM_PICK")) {
        BarcodeScannerScreen(
            validateCode = if (scannerMode == "PAIR") {
                { value ->
                    runCatching { ApiClient.parsePairingPayload(value) }.exceptionOrNull()?.message
                }
            } else null,
            onCode = { value ->
                val mode = scannerMode
                scannerMode = null
                if (mode == "PAIR") model.pair(value) else pendingScan = value
            },
            onCancel = { scannerMode = null },
        )
        return
    }
    Scaffold { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(12.dp)
                .then(if (inventory) Modifier.verticalScroll(rememberScrollState()) else Modifier),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text("元件库移动端", style = MaterialTheme.typography.headlineSmall)
            if (!state.connected && currentSession != null) Text("正在重新连接电脑…")
            if (state.message.isNotBlank()) Text(state.message, color = MaterialTheme.colorScheme.error)
            when {
                state.current != null -> SessionScreen(
                    state.current!!,
                    state.busy || !state.connected,
                    pendingScan,
                    onScanConsumed = { pendingScan = null },
                    onBack = model::closeSession,
                    onCamera = { cameraMode = true },
                    onScanner = { scannerMode = "SESSION" },
                    onCommand = model::command,
                    onConfirm = model::updateAndConfirm,
                    camera = {
                        if (cameraMode && kind == "RECEIVE") {
                            CameraCaptureScreen(model::upload, model::closeSession,
                                Modifier.fillMaxWidth(), state.connected && !state.busy)
                        } else if (scannerMode == "SESSION" && kind == "ISSUE") {
                            BarcodeScannerScreen(
                                onCode = { model.command("SCAN_CODE", JSONObject().put("raw_text", it)) },
                                onCancel = null,
                                modifier = Modifier.fillMaxWidth(),
                                continuous = true,
                                enabled = state.connected && !state.busy,
                            )
                        }
                    },
                )
                !state.connected -> PairScreen(
                    busy = state.busy,
                    onScan = { scannerMode = "PAIR" },
                    onPair = model::pair,
                )
                else -> HomeScreen(state, model::create, model::open, model::refresh, model::forget)
            }
        }
    }
}

@Composable
private fun PairScreen(busy: Boolean, onScan: () -> Unit, onPair: (String) -> Unit) {
    var payload by remember { mutableStateOf("") }
    Text("请在电脑的 设置 → 连接信息 生成配对二维码。")
    Button(onClick = onScan, enabled = !busy) { Text("扫描电脑配对二维码") }
    OutlinedTextField(
        value = payload,
        onValueChange = { payload = it },
        label = { Text("或粘贴完整配对载荷") },
        modifier = Modifier.fillMaxWidth(),
    )
    Button(onClick = { onPair(payload) }, enabled = payload.isNotBlank() && !busy) { Text("配对") }
    DiagnosticCopyButton()
}

@Composable
private fun HomeScreen(
    state: MobileState,
    create: (String) -> Unit,
    open: (JSONObject) -> Unit,
    refresh: () -> Unit,
    forget: () -> Unit,
) {
    Text("已通过局域网 HTTPS 连接；库存数据只保存在电脑。")
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = { create("RECEIVE") }) { Text("摄像头入库") }
        Button(onClick = { create("ISSUE") }) { Text("扫码出库") }
    }
    DiagnosticCopyButton()
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = refresh) { Text("刷新任务") }
        Button(onClick = forget) { Text("移除连接") }
    }
    Text("当前任务", style = MaterialTheme.typography.titleMedium)
    LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        items(state.sessions, key = { it.getString("session_id") }) { session ->
            Card(onClick = { open(session) }, modifier = Modifier.fillMaxWidth()) {
                val snapshot = session.getJSONObject("snapshot")
                Text(
                    "${snapshot.optString("name", snapshot.optString("kind"))} · ${session.getString("state")}",
                    Modifier.padding(12.dp),
                )
            }
        }
    }
}

@Composable
private fun SessionScreen(
    session: JSONObject,
    busy: Boolean,
    pendingScan: String?,
    onScanConsumed: () -> Unit,
    onBack: () -> Unit,
    onCamera: () -> Unit,
    onScanner: () -> Unit,
    onCommand: (String, JSONObject, ((JSONObject) -> Unit)?) -> Unit,
    onConfirm: (JSONObject) -> Unit,
    camera: @Composable () -> Unit,
) {
    val snapshot = session.getJSONObject("snapshot")
    val kind = snapshot.getString("kind")
    val form = snapshot.optJSONObject("form")
    val cameraOnly = form?.optBoolean("camera_only") == true && !form.optBoolean("camera_stream")
    val completed = session.getString("state") == "COMPLETED"
    var scanQuantity by remember(session.getString("session_id")) { mutableStateOf("1") }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = onBack, enabled = !busy) { Text("返回") }
        Text(when (kind) {
            "ISSUE" -> "扫码出库"
            "RECEIVE" -> "扫描 / 图片识别入库"
            else -> "BOM 取货"
        }, style = MaterialTheme.typography.titleLarge)
    }
    pendingScan?.let { code ->
        LaunchedEffect(code, session.getInt("revision")) {
            DiagnosticLog.record(
                "SESSION_SCAN_SUBMIT",
                "kind=$kind length=${code.length} fingerprint=${DiagnosticLog.valueFingerprint(code)}",
            )
            val payload = JSONObject().put("raw_text", code)
            if (kind == "BOM_PICK") payload.put("quantity", scanQuantity.toIntOrNull() ?: 1)
            onCommand("SCAN_CODE", payload, if (cameraOnly) { { onBack() } } else null)
            onScanConsumed()
        }
    }
    if (completed && kind != "BOM_PICK") {
        val batch = snapshot.optJSONObject("batch")
        Text(if (kind == "ISSUE") "出库成功 · 批次剩余 ${batch?.optInt("remaining_quantity") ?: 0}"
             else "入库成功", color = MaterialTheme.colorScheme.primary)
    }
    camera()
    when (kind) {
        "RECEIVE", "ISSUE" -> {
            val receive = kind == "RECEIVE"
            if (!form.orEmptyStream()) {
                Button(onClick = if (receive) onCamera else onScanner, enabled = !busy) {
                    Text("打开摄像头")
                }
            }
            if (!cameraOnly && !completed) {
                InventorySession(snapshot, receive, busy, onCommand, onConfirm)
            }
            if (!receive && (form.orEmptyStream() || cameraOnly)) {
                Button(onClick = onBack, enabled = !busy) { Text("关闭摄像头") }
            }
        }
        "BOM_PICK" -> PickSession(
            snapshot,
            session.getString("state"),
            busy,
            scanQuantity,
            { scanQuantity = it.filter(Char::isDigit) },
            onScanner,
            onCommand,
        )
    }
}

private fun JSONObject?.orEmptyStream(): Boolean = this?.optBoolean("camera_stream") == true

@Composable
private fun DiagnosticCopyButton() {
    val context = LocalContext.current
    var copied by remember { mutableStateOf<Int?>(null) }
    Button(onClick = { copied = DiagnosticLog.copyToClipboard(context) }) {
        Text("复制诊断日志")
    }
    copied?.let { Text("已复制 $it 个字符，可直接粘贴发送") }
}

@Composable
private fun InventorySession(
    snapshot: JSONObject,
    receive: Boolean,
    busy: Boolean,
    onCommand: (String, JSONObject, ((JSONObject) -> Unit)?) -> Unit,
    onConfirm: (JSONObject) -> Unit,
) {
    val form = snapshot.optJSONObject("form") ?: JSONObject()
    val formKey = JSONObject(form.toString()).apply { remove("location_id") }.toString() +
        snapshot.optJSONObject("draft")?.toString().orEmpty()
    var componentId by remember(formKey) { mutableStateOf(form.text("component_id")) }
    var locationId by remember(formKey, form.text("location_id")) { mutableStateOf(form.text("location_id")) }
    var quantity by remember(formKey) { mutableStateOf(form.optInt("quantity", 1).toString()) }
    var notes by remember(formKey) { mutableStateOf(form.text("notes")) }
    val batch = snapshot.optJSONObject("batch")
    val stock = batch?.optJSONArray("locations").objects().filter { it.optInt("remaining_quantity") > 0 }
    val available = if (receive) Int.MAX_VALUE else if (locationId.isEmpty()) batch?.optInt("remaining_quantity") ?: 0
        else stock.firstOrNull { it.text("location_id") == locationId }?.optInt("remaining_quantity") ?: 0
    val count = quantity.toIntOrNull() ?: 0
    fun update(source: String) = JSONObject().put("location_id", locationId.ifBlank { null } ?: JSONObject.NULL)
        .put("quantity", count).put("notes", notes).put("source", source)

    val component = form.optJSONObject("component") ?: JSONObject()
    val componentFields = mapOf(
        "name" to "元件名称", "lcsc_part_number" to "立创编号",
        "manufacturer_part_number" to "制造商料号", "brand" to "品牌",
        "package" to "封装", "category" to "元件类别",
    )
    val receiveFields = mapOf("batch_code" to "自定义批次码", "order_number" to "立创订单号",
        "unit_price" to "购买单价", "purchase_date" to "购买日期")
    var componentValues by remember(formKey) {
        mutableStateOf(componentFields.mapValues { component.text(it.key) })
    }
    var receiveValues by remember(formKey) {
        mutableStateOf(receiveFields.mapValues { form.text(it.key) })
    }
    if (receive) {
        Text("元件信息", style = MaterialTheme.typography.titleMedium)
        val candidates = snapshot.optJSONArray("component_candidates").objects()
        if (candidates.isNotEmpty()) {
            InventoryChoice("匹配元件", componentId,
                listOf("" to "新建元件") + candidates.map { it.text("id") to it.text("name") },
                !busy) { componentId = it }
        }
        for ((field, label) in componentFields) {
            if (componentId.isBlank()) {
                InventoryText(componentValues.getValue(field), label, !busy) {
                    componentValues = componentValues + (field to it)
                }
            }
        }
        for ((field, label) in receiveFields) {
            InventoryText(receiveValues.getValue(field), label, !busy) {
                receiveValues = receiveValues + (field to it)
            }
        }
    } else {
        Row(
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                quantity, { quantity = it.filter(Char::isDigit) }, label = { Text("出库数量") },
                enabled = !busy && batch != null, singleLine = true, modifier = Modifier.weight(1f),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            )
            Button(
                enabled = !busy && count in 1..available,
                onClick = { onConfirm(update("扫码出库")) },
            ) { Text("确认出库") }
        }
        if (batch == null) return
        val part = snapshot.optJSONObject("component") ?: JSONObject()
        Text(batch.text("component_name"), style = MaterialTheme.typography.titleMedium)
        Text(listOf(part.text("manufacturer_part_number"), part.text("brand"), part.text("package"))
            .filter(String::isNotBlank).joinToString(" · "))
        val label = batch.text("custom_code").ifBlank {
            "订单 ${batch.text("order_number")} / ${batch.text("lcsc_part_number")}"
        }
        val source = if (batch.text("source_type").equals("JLC", true)) "立创" else "自定义"
        Text("批次 $label · $source · 原始 ${batch.optInt("initial_quantity")} · 剩余 ${batch.optInt("remaining_quantity")}")
    }
    val storageRows = if (receive) snapshot.optJSONArray("locations").objects() else stock
    val locations = if (receive) storageRows.map {
        it.text("id") to "${it.text("warehouse_name")} / ${it.text("name")}"
    } else listOf("" to "全部仓库（${batch?.optInt("remaining_quantity")}）") + stock.map {
        it.text("location_id") to "${it.text("warehouse_name")} / ${it.text("location_name")}（${it.optInt("remaining_quantity")}）"
    }
    val storageColors = storageRows.associate {
        it.text(if (receive) "id" else "location_id") to it.text("color")
    }
    Text(if (receive) "位置 / 仓库与数量" else "位置 / 仓库", style = MaterialTheme.typography.titleMedium)
    InventoryChoice("位置 / 仓库", locationId, locations, !busy, storageColors) {
        locationId = it
        if (!receive && form.orEmptyStream()) {
            onCommand("SELECT_ISSUE_LOCATION",
                JSONObject().put("location_id", it.ifBlank { null } ?: JSONObject.NULL), null)
        }
    }
    if (receive) InventoryText(quantity, "数量", !busy) { quantity = it.filter(Char::isDigit) }
    InventoryText(notes, "用途 / 备注", !busy) { notes = it }
    if (!receive) return
    Button(
        enabled = !busy && count in 1..available && locationId.isNotBlank() &&
            (componentId.isNotBlank() || componentValues.getValue("name").isNotBlank()),
        onClick = {
            val update = update("扫码入库")
            update.put("component_id", componentId.ifBlank { null } ?: JSONObject.NULL)
            if (componentId.isBlank()) update.put("component", JSONObject(componentValues))
            for ((field, value) in receiveValues) {
                update.put(field, value.ifBlank { null } ?: JSONObject.NULL)
            }
            onConfirm(update)
        },
    ) { Text("确认入库") }
}

private fun JSONObject.text(key: String): String = if (isNull(key)) "" else optString(key)

private fun JSONArray?.objects(): List<JSONObject> =
    if (this == null) emptyList() else (0 until length()).map(::getJSONObject)

@Composable
private fun InventoryText(value: String, label: String, enabled: Boolean, change: (String) -> Unit) {
    OutlinedTextField(value, change, label = { Text(label) }, enabled = enabled,
        modifier = Modifier.fillMaxWidth(), singleLine = true)
}

@Composable
private fun InventoryChoice(label: String, value: String, options: List<Pair<String, String>>,
                            enabled: Boolean, colors: Map<String, String> = emptyMap(),
                            change: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        Button(onClick = { expanded = true }, enabled = enabled && options.isNotEmpty()) {
            StorageLabel(options.firstOrNull { it.first == value }?.second ?: "选择$label", colors[value])
        }
        DropdownMenu(expanded, onDismissRequest = { expanded = false }) {
            for ((id, name) in options) {
                DropdownMenuItem(text = { StorageLabel(name, colors[id]) },
                    onClick = { change(id); expanded = false })
            }
        }
    }
}

@Composable
private fun StorageLabel(name: String, hex: String?) {
    val rgb = hex?.takeIf { it.startsWith("#") && it.length == 7 }
        ?.drop(1)?.toLongOrNull(16)
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        if (rgb != null) Box(Modifier.size(12.dp).background(Color((0xff000000L or rgb).toInt())))
        Text(name)
    }
}

@Composable
private fun PickSession(
    snapshot: JSONObject,
    state: String,
    busy: Boolean,
    scanQuantity: String,
    onScanQuantityChange: (String) -> Unit,
    onScanner: () -> Unit,
    command: (String, JSONObject, ((JSONObject) -> Unit)?) -> Unit,
) {
    Text(snapshot.optString("name", "BOM 取货"), style = MaterialTheme.typography.titleLarge)
    OutlinedTextField(
        scanQuantity,
        onScanQuantityChange,
        label = { Text("本次扫码取货数量") },
        modifier = Modifier.fillMaxWidth(),
    )
    Button(onClick = onScanner, enabled = !busy && state != "COMPLETED") { Text("扫描取货二维码") }
    snapshot.optJSONObject("pending_substitution")?.let { pending ->
        Text("扫描物料不在直接需求中，请人工确认替代目标：")
        val candidates = pending.optJSONArray("candidates") ?: JSONArray()
        for (index in 0 until candidates.length()) {
            val candidate = candidates.getJSONObject(index)
            Button(
                onClick = {
                    command(
                        "CONFIRM_SUBSTITUTE",
                        JSONObject()
                            .put("item_id", candidate.getString("item_id"))
                            .put("quantity", pending.getInt("quantity"))
                            .put("reason", candidate.getString("reason"))
                            .put("remember", false),
                        null,
                    )
                },
            ) { Text("${candidate.getJSONObject("item").getString("component_name")} · ${candidate.getString("reason")}") }
        }
    }
    val groups = snapshot.optJSONArray("groups") ?: JSONArray()
    LazyColumn(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        for (groupIndex in 0 until groups.length()) {
            val group = groups.getJSONObject(groupIndex)
            item {
                StorageLabel("${group.getString("warehouse_name")} / ${group.getString("location_name")}",
                    group.text("color"))
            }
            val entries = group.getJSONArray("items")
            items((0 until entries.length()).map(entries::getJSONObject), key = { it.getString("id") }) { item ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(item.getString("component_name"))
                        Text("需求 ${item.getInt("required_quantity")} · 已取 ${item.getInt("picked_quantity")} · 剩余 ${item.getInt("remaining")}")
                        if (item.getInt("remaining") > 0) {
                            Button(onClick = { command("CHECK_ITEM", JSONObject().put("item_id", item.getString("id")), null) }) { Text("整项打钩") }
                        }
                        val picked = item.optJSONArray("entries") ?: JSONArray()
                        if (picked.length() > 0) {
                            Button(onClick = { command("UNDO", JSONObject().put("entry_id", picked.getJSONObject(picked.length() - 1).getString("id")), null) }) { Text("撤销末次") }
                        }
                    }
                }
            }
        }
    }
    Button(onClick = { command("COMMIT", JSONObject(), null) }, enabled = state == "READY" && !busy) {
        Text("全部确认并原子出库")
    }
}
