package app.componentlibrary.mobile

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class ServerCredential(
    val serverId: String,
    val serviceName: String,
    val host: String,
    val port: Int,
    val caPem: String,
    val deviceId: String,
    val token: String,
)

class CredentialStore(context: Context) {
    private val preferences = context.getSharedPreferences("secure_connection", Context.MODE_PRIVATE)
    private val alias = "component-library-device-token"

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                    alias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build(),
            )
            generateKey()
        }
    }

    fun save(value: ServerCredential) {
        val plain = JSONObject()
            .put("server_id", value.serverId)
            .put("service_name", value.serviceName)
            .put("host", value.host)
            .put("port", value.port)
            .put("ca_pem", value.caPem)
            .put("device_id", value.deviceId)
            .put("token", value.token)
            .toString()
            .toByteArray()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        preferences.edit()
            .putString("payload", Base64.encodeToString(cipher.doFinal(plain), Base64.NO_WRAP))
            .putString("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .apply()
    }

    fun load(): ServerCredential? = runCatching {
        val payload = Base64.decode(preferences.getString("payload", null), Base64.NO_WRAP)
        val iv = Base64.decode(preferences.getString("iv", null), Base64.NO_WRAP)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
        val json = JSONObject(String(cipher.doFinal(payload)))
        ServerCredential(
            json.getString("server_id"),
            json.getString("service_name"),
            json.getString("host"),
            json.getInt("port"),
            json.getString("ca_pem"),
            json.getString("device_id"),
            json.getString("token"),
        )
    }.getOrNull()

    fun clear() = preferences.edit().clear().apply()
}
