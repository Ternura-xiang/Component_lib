package app.componentlibrary.mobile

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.ConnectException

class ProtocolCompatibilityTest {
    @Test
    fun pairingRejectsWebLinksAndUnrelatedJsonWithClearMessage() {
        for (value in listOf("https://example.org/parts/123", """{"url":"https://example.org"}""")) {
            val error = assertThrows(IllegalArgumentException::class.java) {
                ApiClient.parsePairingPayload(value)
            }
            assertTrue(error.message.orEmpty().contains("这不是元件库配对二维码"))
        }
    }

    @Test
    fun pairingAcceptsDesktopPayloadAndRejectsMissingFields() {
        val payload = JSONObject(
            """{"scheme":"component-library-pairing","protocol":2,"addresses":["192.0.2.1"],
                "server_id":"desktop","service_name":"desktop.local","port":17636,"pairing_code":"123456",
                "ca_sha256":"${"a".repeat(64)}","server_cert_sha256":"${"b".repeat(64)}"}""",
        )
        assertEquals(2, ApiClient.parsePairingPayload(payload.toString()).getInt("protocol"))
        payload.remove("server_cert_sha256")
        val error = assertThrows(IllegalArgumentException::class.java) {
            ApiClient.parsePairingPayload(payload.toString())
        }
        assertTrue(error.message.orEmpty().contains("信息不完整"))
    }

    @Test
    fun optionalUnknownFieldsDoNotChangeCanonicalFields() {
        val response = JSONObject(
            """{"session_id":"s1","revision":4,"state":"PICKING","snapshot":{"kind":"BOM_PICK"},"future_optional":{"value":1}}""",
        )
        assertEquals("s1", response.getString("session_id"))
        assertEquals(4, response.getInt("revision"))
        assertEquals("BOM_PICK", response.getJSONObject("snapshot").getString("kind"))
        assertFalse(response.isNull("future_optional"))
    }

    @Test
    fun pairingSkipsBenchmarkAddressesAndFallsBackAfterConnectionFailure() {
        val attempts = mutableListOf<String>()
        val host = ApiClient.findPairingHost(
            listOf("198.18.0.1", "198.19.0.1", "10.1.1.1", "10.1.1.1", "192.168.1.10"),
        ) { candidate ->
            attempts.add(candidate)
            if (candidate == "10.1.1.1") throw ConnectException("unreachable")
        }
        assertEquals(listOf("10.1.1.1", "192.168.1.10"), attempts)
        assertEquals("192.168.1.10", host)
    }
}
