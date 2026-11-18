package app.componentlibrary.mobile

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import android.util.Log
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * 保存移动端可由用户直接复制的诊断日志。
 *
 * 日志只记录协议阶段、相机帧元数据和错误，不保存二维码原文、设备令牌或配对载荷。
 */
object DiagnosticLog {
    enum class Level(val priority: Int) {
        DEBUG(Log.DEBUG), INFO(Log.INFO), WARNING(Log.WARN), ERROR(Log.ERROR),
    }

    private const val TAG = "ComponentLibrary"
    private const val MAX_FILE_BYTES = 256 * 1024L
    private const val COPY_CHARS = 64 * 1024
    private val timeFormat = DateTimeFormatter.ofPattern("uuuu-MM-dd HH:mm:ss,SSS", Locale.ROOT)
        .withZone(ZoneId.systemDefault())
    private val bracketPrefix = Regex("^\\[[^]]+]\\[[^]]+]\\[[^]]+] ")
    private val legacyLine = Regex("^(\\S+) (\\S+) (.*)$")
    private val lock = Any()
    private var file: File? = null

    fun initialize(context: Context) {
        synchronized(lock) {
            if (file != null) return
            val directory = File(context.filesDir, "diagnostics").apply { mkdirs() }
            file = File(directory, "mobile.log")
            record(
                "APP_START",
                "sdk=${Build.VERSION.SDK_INT} manufacturer=${safe(Build.MANUFACTURER)} " +
                    "model=${safe(Build.MODEL)} device=${safe(Build.DEVICE)}",
            )
        }
    }

    fun record(event: String, details: String = "", level: Level = Level.INFO) {
        val message = listOf("thread=${Thread.currentThread().name}", details)
            .filter(String::isNotBlank).joinToString(" ")
        val line = formatLine(timeFormat.format(Instant.now()), level, event, message)
        Log.println(level.priority, TAG, line)
        synchronized(lock) {
            val target = file ?: return
            runCatching {
                if (target.exists() && target.length() > MAX_FILE_BYTES) {
                    val tail = recentText(target.readText())
                    val marker = formatLine(
                        timeFormat.format(Instant.now()), Level.WARNING, "LOG_TRUNCATED", "older log truncated",
                    )
                    target.writeText("$marker\n$tail")
                }
                target.appendText("$line\n")
            }.onFailure {
                Log.e(TAG, formatLine(
                    timeFormat.format(Instant.now()), Level.ERROR, "LOG_WRITE_FAILURE",
                    "Unable to persist diagnostics: ${safe(it.message)}",
                ))
            }
        }
    }

    fun recordFailure(event: String, failure: Throwable) {
        record(event, "type=${failure.javaClass.name} message=${safe(failure.message)}", Level.ERROR)
    }

    fun copyToClipboard(context: Context): Int {
        val text = synchronized(lock) {
            runCatching {
                recentText(file?.takeIf(File::exists)?.readText().orEmpty())
                    .lineSequence().mapNotNull(::normalizeLine).joinToString("\n")
            }.getOrElse {
                formatLine(
                    timeFormat.format(Instant.now()), Level.ERROR, "LOG_READ_FAILURE",
                    "${it.javaClass.name}: ${safe(it.message)}",
                )
            }
        }
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("Component Library 诊断日志", text))
        record("DIAGNOSTICS_COPIED", "characters=${text.length}")
        return text.length
    }

    internal fun formatLine(timestamp: String, level: Level, event: String, message: String): String =
        "[$timestamp][${level.name}][$event] ${message.replace('\n', ' ').replace('\r', ' ')}"

    internal fun normalizeLine(line: String): String? {
        if (bracketPrefix.containsMatchIn(line)) return line
        val legacy = legacyLine.matchEntire(line) ?: return null
        val (timestamp, event, details) = legacy.destructured
        return runCatching { formatLine(timeFormat.format(Instant.parse(timestamp)), Level.INFO, event, details) }
            .getOrNull()
    }

    private fun recentText(text: String): String =
        if (text.length <= COPY_CHARS) text
        else text.takeLast(COPY_CHARS).substringAfter('\n', "")

    /** 只生成不可逆短摘要，便于确认连续帧识别到的是同一内容。 */
    fun valueFingerprint(value: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(value.toByteArray())
        return digest.take(6).joinToString("") { "%02x".format(it) }
    }

    private fun safe(value: String?): String = value.orEmpty()
        .replace(Regex("[^a-zA-Z0-9._ -]"), "_")
        .take(160)
}
