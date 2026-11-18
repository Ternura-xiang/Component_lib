package app.componentlibrary.mobile

import android.util.Base64
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.UUID
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

class ApiException(message: String, val status: Int = 0) : Exception(message)

class ApiClient(private var credential: ServerCredential) {
    private val jsonType = "application/json; charset=utf-8".toMediaType()
    private val client: OkHttpClient = secureClient(credential.caPem)
    val baseUrl: String get() = "https://${credential.host}:${credential.port}"

    private fun secureClient(caPem: String): OkHttpClient {
        val certificate = CertificateFactory.getInstance("X.509").generateCertificate(
            ByteArrayInputStream(Base64.decode(caPem, Base64.DEFAULT)),
        ) as X509Certificate
        val store = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null)
            setCertificateEntry("paired-local-ca", certificate)
        }
        val factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply {
            init(store)
        }
        val trustManager = factory.trustManagers.filterIsInstance<X509TrustManager>().single()
        val context = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trustManager), null) }
        return OkHttpClient.Builder()
            .sslSocketFactory(context.socketFactory, trustManager)
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(90, TimeUnit.SECONDS)
            .pingInterval(15, TimeUnit.SECONDS)
            .build()
    }

    private fun execute(request: Request): JSONObject {
        client.newCall(request).execute().use { response ->
            val text = response.body.string()
            if (!response.isSuccessful) throw ApiException(errorMessage(text), response.code)
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }

    private fun authenticated(path: String, method: String = "GET", body: JSONObject? = null): Request {
        val builder = Request.Builder()
            .url(baseUrl + path)
            .header("Authorization", "Bearer ${credential.token}")
        if (method != "GET") builder.method(method, (body ?: JSONObject()).toString().toRequestBody(jsonType))
        return builder.build()
    }

    fun heartbeat(sessionId: String = ""): JSONObject = execute(
        authenticated("/api/v1/devices/heartbeat", "POST")
            .newBuilder().header("X-Client-Version", "1.0.10")
            .header("X-Session-Id", sessionId).build(),
    ).also {
        val major = it.getJSONObject("capabilities").getInt("api_major")
        if (major != 1) throw ApiException("桌面端 API 主版本 $major 不兼容，请升级移动端")
    }

    fun sessions(): JSONArray {
        client.newCall(authenticated("/api/v1/sessions")).execute().use { response ->
            val text = response.body.string()
            if (!response.isSuccessful) throw ApiException(errorMessage(text), response.code)
            return JSONArray(text)
        }
    }

    fun session(id: String): JSONObject = execute(authenticated("/api/v1/sessions/$id"))

    fun createSession(kind: String): JSONObject = execute(
        authenticated(
            "/api/v1/sessions",
            "POST",
            JSONObject().put("kind", kind).put("payload", JSONObject().put("camera_stream", true)),
        ).newBuilder().header("Idempotency-Key", UUID.randomUUID().toString()).build(),
    )

    fun command(id: String, revision: Int, command: String, payload: JSONObject = JSONObject()): JSONObject =
        execute(
            authenticated(
                "/api/v1/sessions/$id/commands",
                "POST",
                JSONObject()
                    .put("command_id", UUID.randomUUID().toString())
                    .put("expected_revision", revision)
                    .put("command", command)
                    .put("payload", payload),
            ),
        )

    fun uploadImage(id: String, file: File): JSONObject {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("file", file.name, file.asRequestBody("image/jpeg".toMediaType()))
            .build()
        return execute(
            Request.Builder().url("$baseUrl/api/v1/sessions/$id/image")
                .header("Authorization", "Bearer ${credential.token}")
                .post(body).build(),
        )
    }

    fun events(id: String, onSnapshot: (JSONObject) -> Unit, onFailure: (Throwable) -> Unit): WebSocket {
        val request = Request.Builder()
            .url("${baseUrl.replace("https://", "wss://")}/api/v1/sessions/$id/events")
            .header("Authorization", "Bearer ${credential.token}")
            .build()
        return client.newWebSocket(request, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) = onSnapshot(JSONObject(text))
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                DiagnosticLog.recordFailure("WEBSOCKET_FAILURE", t)
                onFailure(t)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onFailure(ApiException("连接已关闭 ($code)"))
            }
        })
    }

    companion object {
        internal fun parsePairingPayload(text: String): JSONObject {
            val wrongCode = "这不是元件库配对二维码，请扫描电脑「设置 → 连接信息」中的配对码"
            val payload = try {
                JSONObject(text.trim())
            } catch (_: JSONException) {
                throw IllegalArgumentException(wrongCode)
            }
            val scheme = payload.optString("scheme")
            val protocol = payload.optInt("protocol", -1)
            require(scheme == "component-library-pairing" || (scheme.isEmpty() && protocol == 1)) {
                wrongCode
            }
            require(protocol == 1 || protocol == 2) { "不支持的配对协议版本 $protocol" }
            require(payload.optJSONArray("addresses")?.optString(0)?.isNotBlank() == true) {
                "配对信息中没有局域网地址"
            }
            val fields = listOf("server_id", "service_name", "pairing_code") +
                if (protocol == 1) listOf("ca_pem") else listOf("ca_sha256", "server_cert_sha256")
            require(payload.optInt("port") in 1..65535 && fields.all { payload.optString(it).isNotBlank() }) {
                "配对二维码信息不完整，请在电脑上重新生成"
            }
            return payload
        }

        fun pair(payloadText: String, deviceName: String): ServerCredential {
            val payload = parsePairingPayload(payloadText)
            val protocol = payload.getInt("protocol")
            val addresses = payload.getJSONArray("addresses")
            val port = payload.getInt("port")
            val payloadCa = payload.optString("ca_pem")
            val pairingClient = if (protocol == 1) {
                require(payloadCa.isNotBlank()) { "旧版配对载荷缺少 CA 证书" }
                secureClientForCa(payloadCa)
            } else {
                pinnedClient(payload.getString("server_cert_sha256"))
            }
            val host = findPairingHost((0 until addresses.length()).map(addresses::getString)) { candidate ->
                DiagnosticLog.record("PAIRING_CONNECT", "protocol=$protocol host=$candidate port=$port")
                val capabilities = runCatching {
                    execute(pairingClient, Request.Builder().url("https://$candidate:$port/api/v1/capabilities").build())
                }.onFailure { DiagnosticLog.recordFailure("PAIRING_CONNECT_FAILURE", it) }.getOrThrow()
                require(capabilities.getInt("api_major") == 1) { "桌面端 API 主版本不兼容" }
            }
            val jsonType = "application/json; charset=utf-8".toMediaType()
            val response = execute(
                pairingClient,
                Request.Builder().url("https://$host:$port/api/v1/pairing").post(
                    JSONObject()
                        .put("pairing_code", payload.getString("pairing_code"))
                        .put("device_name", deviceName)
                        .put("device_type", "ANDROID_PHONE")
                        .put("client_version", "1.0.10")
                        .toString().toRequestBody(jsonType),
                ).build(),
            )
            val caPem = if (protocol == 1) payloadCa else response.getString("ca_pem")
            if (protocol == 2) {
                require(certificateFingerprint(caPem) == payload.getString("ca_sha256")) {
                    "桌面端返回的 CA 证书与配对二维码不一致"
                }
            }
            DiagnosticLog.record("PAIRING_SUCCESS", "protocol=$protocol")
            return ServerCredential(
                payload.getString("server_id"),
                payload.getString("service_name"),
                host,
                port,
                caPem,
                response.getString("device_id"),
                response.getString("device_token"),
            )
        }

        internal fun findPairingHost(addresses: List<String>, probe: (String) -> Unit): String {
            val candidates = addresses.map(String::trim).filter { address ->
                address.isNotEmpty() && !address.startsWith("198.18.") && !address.startsWith("198.19.")
            }.distinct()
            require(candidates.isNotEmpty()) { "配对码中没有可用的局域网地址，请在电脑上重新生成配对码" }
            for (host in candidates) {
                try {
                    probe(host)
                    return host
                } catch (_: IOException) {
                    // 探测不消耗一次性配对码，可继续尝试其他网卡地址。
                }
            }
            throw ApiException("无法连接电脑（${candidates.joinToString()}），请检查电脑防火墙是否允许元件库的 HTTPS 端口")
        }

        private fun execute(client: OkHttpClient, request: Request): JSONObject {
            client.newCall(request).execute().use { response ->
                val text = response.body.string()
                if (!response.isSuccessful) throw ApiException(errorMessage(text), response.code)
                return if (text.isBlank()) JSONObject() else JSONObject(text)
            }
        }

        private fun secureClientForCa(caPem: String): OkHttpClient {
            val certificate = decodeCertificate(caPem)
            val store = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
                load(null)
                setCertificateEntry("paired-local-ca", certificate)
            }
            val factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply {
                init(store)
            }
            val trustManager = factory.trustManagers.filterIsInstance<X509TrustManager>().single()
            val context = SSLContext.getInstance("TLS").apply {
                init(null, arrayOf(trustManager), null)
            }
            return baseClient(context, trustManager)
        }

        private fun pinnedClient(expectedFingerprint: String): OkHttpClient {
            val normalized = expectedFingerprint.lowercase()
            val trustManager = object : X509TrustManager {
                override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

                override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) = Unit

                override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                    val certificate = chain?.firstOrNull()
                        ?: throw CertificateException("桌面端没有提供服务器证书")
                    val actual = certificate.encoded.sha256Hex()
                    if (actual != normalized) {
                        throw CertificateException("服务器证书指纹与配对二维码不一致")
                    }
                }
            }
            val context = SSLContext.getInstance("TLS").apply {
                init(null, arrayOf(trustManager), null)
            }
            return baseClient(context, trustManager)
        }

        private fun baseClient(context: SSLContext, trustManager: X509TrustManager): OkHttpClient =
            OkHttpClient.Builder()
                .sslSocketFactory(context.socketFactory, trustManager)
                .connectTimeout(5, TimeUnit.SECONDS)
                .callTimeout(8, TimeUnit.SECONDS)
                .retryOnConnectionFailure(false)
                .followRedirects(false)
                .build()

        private fun decodeCertificate(caPem: String): X509Certificate =
            CertificateFactory.getInstance("X.509").generateCertificate(
                ByteArrayInputStream(Base64.decode(caPem, Base64.DEFAULT)),
            ) as X509Certificate

        private fun certificateFingerprint(caPem: String): String =
            decodeCertificate(caPem).encoded.sha256Hex()

        private fun ByteArray.sha256Hex(): String =
            MessageDigest.getInstance("SHA-256").digest(this).joinToString("") { "%02x".format(it) }

        private fun errorMessage(text: String): String = runCatching {
            val json = JSONObject(text)
            val error = json.optJSONObject("error") ?: json.optJSONObject("detail")?.optJSONObject("error")
            error?.optString("message") ?: json.optString("detail", text)
        }.getOrDefault(text)
    }
}
