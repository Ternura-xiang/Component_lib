"""Qt 后台任务适配器。"""

from __future__ import annotations

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
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        """执行任务；不得从工作线程直接操作 QWidget。"""

        try:
            self.signals.succeeded.emit(self.function())
        except Exception as error:  # 界面边界必须把任意库异常变成可见错误。
            self.signals.failed.emit(str(error))
        finally:
            self.signals.finished.emit()
