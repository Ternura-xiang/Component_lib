"""Qt 后台任务适配器。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    """后台任务的类型无关信号。"""

    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    """在线程池执行一个无参数函数，并把异常转换为界面错误信号。"""

    def __init__(self, function: Callable[[], Any]) -> None:
        """保存待执行的无参数函数，并为本次任务创建独立信号对象。

        ``function`` 会在线程池线程运行，因此它不能读取或修改 QWidget；结果应
        通过 ``succeeded`` 信号回到主线程处理。
        """

        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        """执行任务；不得从工作线程直接操作 QWidget。"""

        try:
            started = time.perf_counter()
            logging.getLogger(__name__).debug(
                "后台任务开始；入口=%s",
                getattr(self.function, "__qualname__", type(self.function).__name__),
            )
            self.signals.succeeded.emit(self.function())
            logging.getLogger(__name__).debug(
                "后台任务完成；耗时=%d 毫秒", round((time.perf_counter() - started) * 1000)
            )
        except Exception as error:  # 界面边界必须把任意库异常变成可见错误。
            if isinstance(error, ValueError) or hasattr(error, "code"):
                logging.getLogger(__name__).warning("后台任务未完成：%s", error)
            else:
                logging.getLogger(__name__).exception("后台任务失败：%s", error)
            self.signals.failed.emit(str(error))
        finally:
            self.signals.finished.emit()
