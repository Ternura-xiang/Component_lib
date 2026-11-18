"""日志留存、重启恢复、过滤及跨线程归档回归。"""

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import component_library.debug_log as debug_log
from component_library.debug_log import DebugLogHandler


def record(level: int, message: str, created: float) -> logging.LogRecord:
    value = logging.LogRecord("component_library.test", level, __file__, 0, message, (), None)
    value.created = created
    return value


def test_debug_expires_but_info_survives_filters_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    path = tmp_path / "app.log"
    handler = DebugLogHandler(path)
    try:
        handler.handle(record(logging.DEBUG, "即将过期的排错细节", now - 599))
        handler.handle(record(logging.INFO, "库存入库完成", now - 86400))
        handler.handle(record(logging.WARNING, "库存不足", now - 86400))
        handler.handle(record(logging.ERROR, "备份写入失败", now - 86400))
        handler.display_level = logging.ERROR
        assert "即将过期" not in handler.snapshot()[1]
        handler.display_level = logging.DEBUG
        assert "即将过期" in handler.snapshot()[1]
        monkeypatch.setattr(debug_log.time, "time", lambda: now + 2)
        assert "即将过期" not in handler.snapshot()[1]
        handler.prune()
        with sqlite3.connect(handler.archive_path) as archive:
            assert archive.execute("SELECT count(*) FROM events WHERE level=10").fetchone()[0] == 0
        handler.doRollover()
        handler.handle(record(logging.INFO, "已关闭程序", now + 2))
    finally:
        handler.close()
    restarted = DebugLogHandler(path)
    try:
        history = restarted.snapshot()[1]
        assert all(text in history for text in ("库存入库完成", "库存不足", "备份写入失败", "已关闭程序"))
        export = tmp_path / "export.log"
        restarted.export(export)
        assert export.read_text(encoding="utf-8").count("库存入库完成") == 1
        assert "即将过期" not in export.read_text(encoding="utf-8")
    finally:
        restarted.close()


def test_concurrent_logging_exports_complete_history_and_redacts_secrets(tmp_path: Path) -> None:
    handler = DebugLogHandler(tmp_path / "app.log")
    logger = logging.Logger("component_library.parallel", logging.DEBUG)
    logger.addHandler(handler)
    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda index: logger.debug("工作线程事件 %d", index), range(450)))
        logger.info("设备连接完成 Authorization: Bearer TOPSECRET device_token=PRIVATE")
        logger.error("下载失败 https://example.com/file.pdf?token=SIGNED&Signature=SIGNATURE\n已停止下载")
        export = tmp_path / "export.log"
        handler.export(export)
        text = export.read_text(encoding="utf-8")
        assert text.count("工作线程事件") == 450
        assert handler.snapshot()[1].count("工作线程事件") < 450
        assert all(secret not in text for secret in ("TOPSECRET", "PRIVATE", "SIGNED", "SIGNATURE"))
        assert "https://example.com/file.pdf" in text
        assert "[ERROR][component_library.parallel] 已停止下载" in text
        assert "工作线程事件" not in Path(handler.baseFilename).read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            handler.export(Path(str(handler.archive_path) + "-wal"))
    finally:
        handler.close()


def test_legacy_debug_removed_and_warning_imported_once(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text(
        "[2020-01-01 00:00:00,000][DEBUG][legacy] 过期细节\n"
        "[2020-01-01 00:00:00,000][WARNING][legacy] 旧版警告\n",
        encoding="utf-8",
    )
    first = DebugLogHandler(path)
    first.close()
    reopened = DebugLogHandler(path)
    try:
        assert "过期细节" not in path.read_text(encoding="utf-8")
        assert reopened.snapshot()[1].count("旧版警告") == 1
    finally:
        reopened.close()
