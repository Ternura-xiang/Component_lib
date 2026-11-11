package app.componentlibrary.mobile

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import android.util.Log
import java.io.File
import java.security.MessageDigest
import java.time.Instant

/**
 * 保存移动端可由用户直接复制的诊断日志。
 *
 * 日志只记录协议阶段、相机帧元数据和错误，不保存二维码原文、设备令牌或配对载荷。
 */
object DiagnosticLog {
    private const val TAG = "ComponentLibrary"
    private const val MAX_FILE_BYTES = 256 * 1024L
    private const val COPY_BYTES = 64 * 1024
    private val lock = Any()
    private var file: File? = null

    fun initialize(context: Context) {
        synchronized(lock) {
            val directory = File(context.filesDir, "diagnostics").apply { mkdirs() }
            file = File(directory, "mobile.log")
        }
        record(
            "APP_START",
            "sdk=${Build.VERSION.SDK_INT} manufacturer=${safe(Build.MANUFACTURER)} " +
                "model=${safe(Build.MODEL)} device=${safe(Build.DEVICE)}",
        )
    }

    fun record(event: String, details: String = "") {
        val line = buildString {
            append(Instant.now())
            append(' ')
            append(event)
            append(" thread=")
            append(Thread.currentThread().name)
            if (details.isNotBlank()) {
                append(' ')
                append(details.replace('\n', ' ').replace('\r', ' '))
            }
        }
        Log.i(TAG, line)
        synchronized(lock) {
            val target = file ?: return
            runCatching {
                if (target.exists() && target.length() > MAX_FILE_BYTES) {
                    val tail = target.readText().takeLast(COPY_BYTES)
                    target.writeText("--- older log truncated ---\n$tail")
                }
                target.appendText("$line\n")
            }.onFailure { Log.e(TAG, "Unable to persist diagnostics", it) }
        }
    }

    fun recordFailure(event: String, failure: Throwable) {
        record(event, "type=${failure.javaClass.name} message=${safe(failure.message)}")
    }

    fun copyToClipboard(context: Context): Int {
        val text = synchronized(lock) {
            val body = runCatching { file?.takeIf(File::exists)?.readText().orEmpty() }
                .getOrElse { "读取诊断日志失败：${it.javaClass.name}: ${safe(it.message)}" }
                .takeLast(COPY_BYTES)
            "Component Library Android diagnostics\n$body"
        }
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("Component Library 诊断日志", text))
        record("DIAGNOSTICS_COPIED", "characters=${text.length}")
        return text.length
    }

    /** 只生成不可逆短摘要，便于确认连续帧识别到的是同一内容。 */
    fun valueFingerprint(value: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(value.toByteArray())
        return digest.take(6).joinToString("") { "%02x".format(it) }
    }

    private fun safe(value: String?): String = value.orEmpty()
        .replace(Regex("[^a-zA-Z0-9._ -]"), "_")
        .take(160)
}
