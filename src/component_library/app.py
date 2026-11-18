"""桌面应用入口。"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import (
    QLockFile,
    QMessageLogContext,
    QSettings,
    QtMsgType,
    qInstallMessageHandler,
)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .api import ApiContext, ApiServer, create_api
from .connection import ConnectionManager
from .database import Database
from .debug_log import LOG_LEVEL_SETTING, LOG_LEVELS, DebugLogHandler, DependencyLogFilter
from .paths import data_directory, load_or_create_token
from .recognition import RecognitionEngine
from .sessions import SessionService
from .ui.main_window import MainWindow


def configure_logging(directory: Path) -> DebugLogHandler:
    """配置只保存在本机的轮换日志，不记录图片或 API 令牌。"""

    path = directory / "logs"
    path.mkdir(parents=True, exist_ok=True)
    preferences = QSettings("local.component-library.app", "元件库")
    level = LOG_LEVELS.get(str(preferences.value(LOG_LEVEL_SETTING, "DEBUG")), logging.DEBUG)
    handler = DebugLogHandler(path / "component-library.log", level)
    handler.addFilter(DependencyLogFilter())
    logging.basicConfig(level=logging.DEBUG, handlers=[handler], force=True)
    return handler


def install_exception_logging() -> None:
    """接收 Qt 回调、Python 主线程及独立线程的未处理异常。"""

    def main_exception(
        kind: type[BaseException], error: BaseException, traceback: TracebackType | None
    ) -> None:
        logging.getLogger(__name__).error("程序遇到未处理异常", exc_info=(kind, error, traceback))
        sys.__excepthook__(kind, error, traceback)

    def thread_exception(args: threading.ExceptHookArgs) -> None:
        logging.getLogger(__name__).error(
            "后台线程异常退出；线程=%s", args.thread.name if args.thread else "未知",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback) if args.exc_value is not None else None,
        )

    def qt_message(kind: QtMsgType, context: QMessageLogContext, message: str) -> None:
        level = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.DEBUG,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.ERROR,
        }[kind]
        logging.getLogger("component_library.qt").log(
            level, "界面或设备运行信息：%s；来源=%s", message, context.category or "Qt"
        )

    sys.excepthook = main_exception
    threading.excepthook = thread_exception
    qInstallMessageHandler(qt_message)


def main() -> int:
    """初始化路径、数据库、API 和 Qt 主窗口。"""

    application = QApplication(sys.argv)
    application.setApplicationName("元件库")
    application.setOrganizationName("local.component-library.app")
    package = Path(__file__).resolve().parent
    icon_path = package / "assets" / "component-library.png"
    if not icon_path.is_file():
        icon_path = package.parents[1] / "packaging/assets/icons/component-library.png"
    if icon_path.is_file():
        application.setWindowIcon(QIcon(str(icon_path)))
    directory = data_directory()
    log_handler = configure_logging(directory)
    install_exception_logging()
    logger = logging.getLogger(__name__)
    logger.info("元件库正在启动；日志目录=%s", directory / "logs")

    # QLockFile 在进程异常退出后可以自动判断陈旧锁，比单纯创建 PID 文件可靠。
    lock = QLockFile(str(directory / "application.lock"))
    lock.setStaleLockTime(10_000)
    if not lock.tryLock(100):
        logger.warning("启动取消：元件库已有运行实例")
        QMessageBox.warning(None, "元件库", "元件库已经在运行。")
        return 1

    database_path = directory / "inventory.db"
    Database.apply_pending_restore(database_path)
    database = Database(database_path)
    database.integrity_check()
    token = load_or_create_token(directory)
    recognition = RecognitionEngine()
    sessions = SessionService(database)
    connection = ConnectionManager(database, directory)
    api = create_api(ApiContext(database, token, recognition, connection, sessions))
    try:
        api_server = ApiServer(api)
    except OSError as error:
        logger.error("本机 API 启动失败，端口 17635 无法监听：%s", error, exc_info=True)
        QMessageBox.critical(
            None,
            "元件库无法启动",
            f"本机 API 端口 127.0.0.1:17635 无法监听。\n"
            f"请关闭另一个元件库实例或占用该端口的程序后重试。\n\n{error}",
        )
        lock.unlock()
        return 1
    api_server.start()
    logger.info("本机 API 服务已启动，桌面操作接口已就绪")

    lan_server: ApiServer | None = None
    try:
        lan_server = ApiServer(
            api,
            host="0.0.0.0",
            port=connection.port,
            ssl_certfile=str(connection.cert_path),
            ssl_keyfile=str(connection.key_path),
        )
        lan_server.start()
        logger.info("手机 HTTPS 连接服务已启动，端口 %d", connection.port)
    except OSError as error:
        logger.warning("手机连接服务未启动，端口 %d 无法监听：%s", connection.port, error)
        QMessageBox.warning(
            None,
            "局域网连接未启动",
            f"HTTPS 端口 0.0.0.0:{connection.port} 无法监听，手机连接暂不可用。\n\n{error}",
        )
    if lan_server is not None:
        try:
            connection.start_discovery()
        except Exception as error:
            logging.getLogger(__name__).warning("mDNS 自动发现启动失败：%s", error)
            QMessageBox.warning(
                None,
                "自动发现未启动",
                "手机仍可使用配对二维码中的地址连接，但桌面 IP 变化后需要重新配对。",
            )

    window = MainWindow(database, recognition, token, connection, sessions, log_handler=log_handler)
    window.show()
    logger.info("元件库已就绪")
    exit_code = application.exec()
    logger.info("正在关闭元件库，停止设备连接和 API 服务")
    api_server.stop()
    api_server.join(timeout=5)
    if lan_server is not None:
        lan_server.stop()
        lan_server.join(timeout=5)
    connection.stop_discovery()
    lock.unlock()
    logger.info("元件库已关闭，退出码 %d", exit_code)
    log_handler.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
