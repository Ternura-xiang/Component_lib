"""为业务入口记录可读结果及详细执行信息。"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


def log_operation(action: str, *, level: int = logging.INFO) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """只记录操作、耗时和结果摘要，输入正文与认证信息由业务代码持有。"""

    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        logger = logging.getLogger(function.__module__)
        signature = inspect.signature(function)

        @wraps(function)
        def run(*args: P.args, **kwargs: P.kwargs) -> T:
            started = time.perf_counter()
            arguments = signature.bind(*args, **kwargs).arguments
            identifiers: dict[str, str | int | float] = {
                key: value for key, value in arguments.items()
                if key in {"component_id", "transaction_id", "group_id", "session_id",
                           "location_id", "catalog", "kind", "command", "actor"}
                and isinstance(value, (str, int))
            }
            data = arguments.get("data")
            quantity = getattr(data, "quantity", None)
            for field in ("component_id", "location_id", "from_location_id", "to_location_id", "batch_id"):
                value = getattr(data, field, None)
                if isinstance(value, str):
                    identifiers[field] = value
            for field in ("quantity", "expected_delta", "target_id", "component_id"):
                value = arguments.get(field)
                if isinstance(value, (str, int, float)):
                    identifiers[field] = value
            if isinstance(quantity, int):
                identifiers["数量"] = quantity
            logger.debug("开始%s；入口=%s；上下文=%s", action, function.__qualname__, identifiers)
            try:
                result = function(*args, **kwargs)
            except (ValueError, RuntimeError) as error:
                # DomainError 属于 RuntimeError，业务冲突由 Warning 表达。
                if isinstance(error, ValueError) or hasattr(error, "code"):
                    logger.warning("%s未完成：%s", action, error)
                    logger.debug("%s的校验与冲突详情", action, exc_info=True)
                else:
                    logger.exception("%s失败：%s", action, error)
                raise
            except Exception as error:
                logger.exception("%s失败：%s", action, error)
                raise
            elapsed = round((time.perf_counter() - started) * 1000)
            if result is False:
                logger.debug("%s未产生变更，耗时 %d 毫秒", action, elapsed)
            else:
                quantity_summary = f"，数量 {quantity}" if isinstance(quantity, int) else ""
                logger.log(
                    level, "%s完成%s%s，耗时 %d 毫秒",
                    action, quantity_summary, _result_summary(result), elapsed,
                )
            logger.debug(
                "结束%s；入口=%s；线程=%s；结果标识=%s",
                action, function.__qualname__, threading.current_thread().name,
                _result_identifiers(result),
            )
            return result

        return run

    return decorate


def _result_summary(result: Any) -> str:
    if isinstance(result, bool):
        return ""
    if isinstance(result, int):
        return f"，处理 {result} 项"
    if isinstance(result, list):
        return f"，共 {len(result)} 项"
    if not isinstance(result, dict):
        return ""
    labels = {
        "imported": "已导入", "skipped": "已跳过", "created": "新建元件",
        "affected_transactions": "涉及流水", "moved_lots": "移动批次",
    }
    counts = [f"{label} {result[key]}" for key, label in labels.items() if key in result]
    return "，" + "，".join(counts) if counts else ""


def _result_identifiers(result: Any) -> dict[str, str]:
    if not isinstance(result, dict):
        return {}
    return {
        key: str(result[key])
        for key in ("id", "transaction_group_id", "session_id", "batch_id", "bom_id")
        if result.get(key) is not None
    }
