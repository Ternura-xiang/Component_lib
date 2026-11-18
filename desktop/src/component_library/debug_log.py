"""本机日志归档、十分钟调试记录和 Qt 实时事件。"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Signal

LOG_LEVEL_SETTING = "debug/log_level"
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
DEBUG_RETENTION_SECONDS = 10 * 60
BRACKET_PREFIX = re.compile(r"^(\[([^\]]+)\]\[([^\]]+)\]\[[^\]]+\]) ?")
LEGACY_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (\w+) ([^:]+): (.*)$"
)
SECRET = re.compile(
    r"(?i)(\b(?:authorization|bearer|device_token|access_token|api[_-]?key|pairing_code|"
    r"password|signature|token)\b[\s\"':=]+)([^\s,;\"'<>]+)"
)
URL_QUERY = re.compile(r"(https?://[^\s?#<>\"']+)\?[^\s<>\"']+")
BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[^\s,;\"'<>]+")


def redact_log_text(text: str) -> str:
    """隐去认证字段和网页查询参数，保留排错所需的页面路径。"""

    text = URL_QUERY.sub(r"\1?[已隐藏参数]", text)
    text = BEARER_TOKEN.sub(r"\1[已隐藏]", text)
    return SECRET.sub(r"\1[已隐藏]", text)


class BracketLogFormatter(logging.Formatter):
    """多行异常沿用所属事件的时间、等级和来源。"""

    def format(self, record: logging.LogRecord) -> str:
        level = "ERROR" if record.levelno >= logging.ERROR else record.levelname
        prefix = f"[{self.formatTime(record)}][{level}][{record.name}] "
        message = redact_log_text(super().format(record))
        return "\n".join(prefix + line for line in message.splitlines() or [""])


class DebugLogEvents(QObject):
    appended = Signal(object, int, str)


class DependencyLogFilter(logging.Filter):
    """第三方库的请求细节放在 Debug，保留其 Warning 和 Error。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.levelno < logging.WARNING
            and not record.name.startswith(("component_library", "运行环境"))
        ):
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


class DebugLogHandler(RotatingFileHandler):
    """始终采集四级日志，SQLite 保留 Info 以上历史并定期清理过期 Debug。

    文本轮换文件保存 Info 以上事件，便于直接查看。界面过滤不影响采集和归档。
    所有 SQLite 访问共用 logging.Handler 的锁，支持 Qt、API 和识别工作线程。
    """

    def __init__(self, path: Path, level: int = logging.DEBUG) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        self.setFormatter(BracketLogFormatter())
        self.setLevel(logging.DEBUG)
        self.display_level = level
        self.events = DebugLogEvents()
        self.archive_path = path.with_suffix(path.suffix + ".sqlite3")
        self._archive = sqlite3.connect(self.archive_path, check_same_thread=False, isolation_level=None)
        self._archive.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self._archive.execute("PRAGMA journal_mode=WAL")
        self._archive.execute(
            """CREATE TABLE IF NOT EXISTS events(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,
            level INTEGER NOT NULL,text TEXT NOT NULL)"""
        )
        self._archive.execute("CREATE INDEX IF NOT EXISTS event_expiry ON events(level,created)")
        self._archive.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY)")
        self._sequence = int(self._archive.execute("SELECT coalesce(max(sequence),0) FROM events").fetchone()[0])
        self._runtime_written = False
        self._stopping = threading.Event()
        self._storage_closed = False
        self._import_legacy(path)
        self.prune()
        self._maintenance = threading.Thread(target=self._maintain, name="log-retention", daemon=True)
        self._maintenance.start()

    def _import_legacy(self, path: Path) -> None:
        if self._archive.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
            return
        self._archive.execute("BEGIN")
        try:
            imported_at = time.time()
            for source in [*(path.with_name(f"{path.name}.{index}") for index in (3, 2, 1)), path]:
                if not source.is_file():
                    continue
                prefix, level = "", logging.INFO
                created = imported_at
                retained_lines = []
                with source.open(encoding="utf-8", errors="replace") as stream:
                    for raw in stream:
                        line = raw.rstrip("\n")
                        if match := BRACKET_PREFIX.match(line):
                            prefix = match[1]
                            level = LOG_LEVELS.get(match[3], logging.ERROR if match[3] == "CRITICAL" else logging.INFO)
                            try:
                                created = datetime.fromisoformat(match[2].replace(",", ".")).timestamp()
                            except ValueError:
                                created = imported_at
                        elif legacy := LEGACY_LINE.match(line):
                            prefix = f"[{legacy[1]}][{legacy[2]}][{legacy[3]}]"
                            level = LOG_LEVELS.get(legacy[2], logging.INFO)
                            created = datetime.fromisoformat(legacy[1].replace(",", ".")).timestamp()
                            line = f"{prefix} {legacy[4]}"
                        elif prefix:
                            line = f"{prefix} {line}"
                        if prefix:
                            text = redact_log_text(line)
                            if level >= logging.INFO or created >= imported_at - DEBUG_RETENTION_SECONDS:
                                self._store(created, level, text)
                            if level >= logging.INFO:
                                retained_lines.append(text)
                # 旧文本文件可能包含 Debug，只在首次迁移时清理，历史归档由 SQLite 接管。
                if self.stream is not None and source == path:
                    self.stream.close()
                    self.stream = None
                source.write_text("\n".join(retained_lines) + ("\n" if retained_lines else ""), encoding="utf-8")
            self._archive.execute("INSERT INTO metadata VALUES('legacy_imported')")
            self._archive.execute("COMMIT")
        except Exception:
            self._archive.execute("ROLLBACK")
            raise

    def _store(self, created: float, level: int, text: str) -> None:
        cursor = self._archive.execute(
            "INSERT INTO events(created,level,text) VALUES(?,?,?)", (created, level, text)
        )
        assert cursor.lastrowid is not None
        self._sequence = cursor.lastrowid

    def emit(self, record: logging.LogRecord) -> None:
        if self._storage_closed:
            return
        try:
            text = self.format(record)
            if record.levelno >= logging.INFO:
                super().emit(record)
            if record.levelno >= logging.INFO or record.created >= time.time() - DEBUG_RETENTION_SECONDS:
                self._store(record.created, min(record.levelno, logging.ERROR), text)
                self.events.appended.emit(self._sequence, record.levelno, text)
        except Exception:
            self.handleError(record)

    def prune(self) -> None:
        """即使没有新日志，也会定期删除超过十分钟的 Debug。"""

        self.acquire()
        try:
            if not self._storage_closed:
                self._archive.execute(
                    "DELETE FROM events WHERE level<? AND created<?",
                    (logging.INFO, time.time() - DEBUG_RETENTION_SECONDS),
                )
                self._archive.execute("PRAGMA incremental_vacuum(64)")
        finally:
            self.release()

    def _maintain(self) -> None:
        while not self._stopping.wait(30):
            try:
                self.prune()
            except Exception:
                # 文件系统异常通过现有 logging 错误通道报告，避免递归写日志。
                self.handleError(logging.LogRecord("日志", logging.ERROR, "", 0, "日志清理失败", (), None))

    def log_runtime_once(self, details: str) -> None:
        self.acquire()
        try:
            if not self._runtime_written:
                self._runtime_written = True
                self.handle(logging.LogRecord("运行环境", logging.INFO, "", 0, details, (), None))
        finally:
            self.release()

    def snapshot(self) -> tuple[int, str]:
        """按界面等级读取最近 300 行，过期 Debug 立即从查询结果排除。"""

        self.acquire()
        try:
            rows = self._archive.execute(
                """SELECT text FROM events WHERE level>=? AND (level>=? OR created>=?)
                ORDER BY sequence DESC LIMIT 300""",
                (self.display_level, logging.INFO, time.time() - DEBUG_RETENTION_SECONDS),
            ).fetchall()
            lines = "\n".join(row[0] for row in reversed(rows)).splitlines()
            return self._sequence, "\n".join(lines[-300:])
        finally:
            self.release()

    def export(self, destination: Path) -> None:
        """导出当前保留的全部日志，独立于面板筛选等级。"""

        if destination.resolve() in {
            Path(self.baseFilename).resolve(), self.archive_path.resolve(),
            *(Path(f"{self.archive_path}{suffix}").resolve() for suffix in ("-wal", "-shm", "-journal")),
            *(Path(f"{self.baseFilename}.{index}").resolve() for index in (1, 2, 3)),
        }:
            raise ValueError("请选择独立的日志导出文件")
        self.acquire()
        try:
            with destination.open("w", encoding="utf-8") as stream:
                for (text,) in self._archive.execute(
                    "SELECT text FROM events WHERE level>=? OR created>=? ORDER BY sequence",
                    (logging.INFO, time.time() - DEBUG_RETENTION_SECONDS),
                ):
                    stream.write(text + "\n")
        finally:
            self.release()

    def close(self) -> None:
        self._stopping.set()
        self.acquire()
        try:
            if not self._storage_closed:
                self.prune()
                self._archive.close()
                self._storage_closed = True
            super().close()
        finally:
            self.release()
