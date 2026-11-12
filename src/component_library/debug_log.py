"""轮换日志及 Qt 调试页使用的实时事件。"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Signal

LOG_LEVEL_SETTING = "debug/log_level"
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
BRACKET_PREFIX = re.compile(r"^(\[[^\]]+\]\[([^\]]+)\]\[[^\]]+\]) ?")
LEGACY_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (\w+) ([^:]+): (.*)$"
)


class BracketLogFormatter(logging.Formatter):
    """多行异常沿用所属事件的时间、等级和来源。"""

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"[{self.formatTime(record)}][{record.levelname}][{record.name}] "
        return "\n".join(prefix + line for line in super().format(record).splitlines() or [""])


class DebugLogEvents(QObject):
    appended = Signal(object, int, str)


class DebugLogHandler(RotatingFileHandler):
    """写入本机日志，并把新事件排队送往调试界面。"""

    def __init__(self, path: Path, level: int = logging.INFO) -> None:
        super().__init__(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        self.setFormatter(BracketLogFormatter())
        self.setLevel(level)
        self.events = DebugLogEvents()
        self._sequence = 0
        self._runtime_written = False

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self._sequence += 1
        try:
            self.events.appended.emit(self._sequence, record.levelno, self.format(record))
        except Exception:
            self.handleError(record)

    def log_runtime_once(self, details: str) -> None:
        """每次启动记录一条环境快照。"""

        self.acquire()
        try:
            if not self._runtime_written:
                self._runtime_written = True
                self.handle(logging.LogRecord("运行环境", logging.INFO, "", 0, details, (), None))
        finally:
            self.release()

    def snapshot(self) -> tuple[int, str]:
        """在同一日志锁下读取历史末尾和最后一个事件序号。"""

        self.acquire()
        try:
            with Path(self.baseFilename).open("rb") as stream:
                start = max(0, stream.seek(0, 2) - 64 * 1024)
                if start:
                    stream.seek(start - 1)
                    stream.readline()
                else:
                    stream.seek(0)
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
                visible = []
                prefix, level = "", 0
                for line in lines:
                    if match := BRACKET_PREFIX.match(line):
                        prefix, level = match[1], LOG_LEVELS.get(match[2], logging.INFO)
                    elif legacy := LEGACY_LINE.match(line):
                        prefix = f"[{legacy[1]}][{legacy[2]}][{legacy[3]}]"
                        level = LOG_LEVELS.get(legacy[2], logging.INFO)
                        line = f"{prefix} {legacy[4]}"
                    elif prefix:
                        line = f"{prefix} {line}"
                    if prefix and level >= self.level:
                        visible.append(line)
                return self._sequence, "\n".join(visible[-300:])
        finally:
            self.release()
