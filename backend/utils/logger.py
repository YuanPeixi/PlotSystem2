"""统一日志配置。"""

from __future__ import annotations

import io
import logging
import sys

from backend.config import settings

_CONFIGURED = False


def _safe_stdout():
    """返回编码安全的 stdout。

    Windows 控制台默认代码页常是 cp936(GBK)，打印中文以外的某些字符
    （或反过来，某些环境下的生僻符号）会抛 UnicodeEncodeError，
    导致 logging 内部 handleError 打印出一大段 Message/Arguments 诊断堆栈，
    看起来像程序崩溃。这里统一改为 UTF-8 + errors=replace，避免因日志打印
    本身而中断/污染输出。
    """
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        return stream
    except Exception:  # noqa: BLE001
        pass
    try:
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            return io.TextIOWrapper(
                buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
    except Exception:  # noqa: BLE001
        pass
    return stream


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    handler = logging.StreamHandler(_safe_stdout())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger("plotsystem")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取带统一格式的命名日志器。"""
    _configure_root()
    return logging.getLogger(f"plotsystem.{name}")
