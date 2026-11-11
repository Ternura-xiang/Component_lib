package app.componentlibrary.mobile

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ProtocolCompatibilityTest {
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
}
