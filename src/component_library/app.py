"""桌面应用入口。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from .api import ApiContext, ApiServer, create_api
from .database import Database
from .paths import data_directory, load_or_create_token
from .recognition import RecognitionEngine
from .ui.main_window import MainWindow


def configure_logging(directory: object) -> None:
    """配置只保存在本机的轮换日志，不记录图片或 API 令牌。"""

    from pathlib import Path

    path = Path(str(directory)) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path / "component-library.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    """初始化路径、数据库、API 和 Qt 主窗口。"""

    application = QApplication(sys.argv)
    application.setApplicationName("元件库")
    application.setOrganizationName("local.component-library.app")
    directory = data_directory()
    configure_logging(directory)

    # QLockFile 在进程异常退出后可以自动判断陈旧锁，比单纯创建 PID 文件可靠。
    lock = QLockFile(str(directory / "application.lock"))
    lock.setStaleLockTime(10_000)
    if not lock.tryLock(100):
        QMessageBox.warning(None, "元件库", "元件库已经在运行。")
        return 1

    database_path = directory / "inventory.db"
    Database.apply_pending_restore(database_path)
    database = Database(database_path)
    database.integrity_check()
    token = load_or_create_token(directory)
    recognition = RecognitionEngine()
    try:
        api_server = ApiServer(create_api(ApiContext(database, token, recognition)))
    except OSError as error:
        QMessageBox.critical(
            None,
            "元件库无法启动",
            f"本机 API 端口 127.0.0.1:17635 无法监听。\n"
            f"请关闭另一个元件库实例或占用该端口的程序后重试。\n\n{error}",
        )
        lock.unlock()
        return 1
    api_server.start()

    window = MainWindow(database, recognition, token)
    window.show()
    exit_code = application.exec()
    api_server.stop()
    api_server.join(timeout=5)
    lock.unlock()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
