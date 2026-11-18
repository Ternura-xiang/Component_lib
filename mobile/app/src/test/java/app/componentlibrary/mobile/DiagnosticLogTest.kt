package app.componentlibrary.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DiagnosticLogTest {
    @Test
    fun formatsTimestampSeverityAndEvent() {
        val line = DiagnosticLog.formatLine(
            "2026-11-12 08:01:02,003", DiagnosticLog.Level.ERROR, "WEBSOCKET_FAILURE",
            "thread=main\nconnection closed",
        )
        assertEquals(
            "[2026-11-12 08:01:02,003][ERROR][WEBSOCKET_FAILURE] thread=main connection closed",
            line,
        )
        assertEquals(line, DiagnosticLog.normalizeLine(line))
    }

    @Test
    fun normalizesExistingDiagnosticLinesForCopy() {
        val line = DiagnosticLog.normalizeLine("2026-11-12T08:01:02.003Z APP_START thread=main")
        assertTrue(line.orEmpty().endsWith("[INFO][APP_START] thread=main"))
        assertNull(DiagnosticLog.normalizeLine("--- older log truncated ---"))
    }
}
