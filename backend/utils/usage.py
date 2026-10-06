"""LLM 调用计数（工单25）。

计数点只有 `utils/llm.py` 一处（契约7），这里只提供计数器与归属机制：
`run_scene` 用 `usage_scope()` 装一个计数器，期间经 `llm.py` 发出的调用都记到它上面。

归属靠 ContextVar 而不是参数逐层传递：后者要改所有 agent 的签名，新增调用点漏传是静默的。
ContextVar 会被 `asyncio.create_task` / `gather` 的子任务和 `asyncio.to_thread` 复制，
因此 selector 的并发打分、线程里的 embedding 都能归到同一场；同一时刻跑着的两场
各在自己的任务上下文里，互不串。

只增观测：计数器不进任何 prompt、不参与任何判断；计数本身出错不得让调用失败。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, fields

from backend.models import LLMPurpose, LLMUsageStat
from backend.utils.logger import get_logger

logger = get_logger("usage")

_INT_FIELDS = tuple(f.name for f in fields(LLMUsageStat) if f.name != "seconds")


class UsageMeter:
    """一个归属单位（一场戏的一段运行，或一次评估）的计数。

    embedding 在 `asyncio.to_thread` 的线程里累加，与事件循环线程并发，故加锁。
    """

    def __init__(self, initial: Mapping[str, LLMUsageStat] | None = None):
        self._lock = threading.Lock()
        self._stats: dict[str, LLMUsageStat] = {}
        self.seed(initial or {})

    def seed(self, stats: Mapping[str, LLMUsageStat]) -> None:
        """把已有计数并进来。continue 续跑在场景已有的计数上累加；复制一份，不改来源对象。"""
        with self._lock:
            for purpose, add in stats.items():
                stat = self._stat(purpose)
                for name in _INT_FIELDS:
                    setattr(stat, name, getattr(stat, name) + getattr(add, name))
                stat.seconds += add.seconds

    def _stat(self, purpose: str) -> LLMUsageStat:
        stat = self._stats.get(purpose)
        if stat is None:
            stat = self._stats[purpose] = LLMUsageStat()
        return stat

    def record_call(
        self,
        purpose: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        estimated: bool,
        seconds: float,
    ) -> None:
        with self._lock:
            stat = self._stat(purpose)
            stat.calls += 1
            stat.prompt_tokens += prompt_tokens
            stat.completion_tokens += completion_tokens
            stat.estimated_calls += int(estimated)
            stat.seconds += seconds

    def record_failure(self, purpose: str, *, seconds: float) -> None:
        with self._lock:
            stat = self._stat(purpose)
            stat.failures += 1
            stat.seconds += seconds

    def record_retry(self, purpose: str) -> None:
        with self._lock:
            self._stat(purpose).retries += 1

    def snapshot(self) -> dict[str, LLMUsageStat]:
        """当前计数的独立副本。落盘前取一份，免得序列化时撞上线程里的累加。"""
        with self._lock:
            return {k: LLMUsageStat(**asdict(v)) for k, v in self._stats.items()}


_current: ContextVar[UsageMeter | None] = ContextVar("llm_usage_meter", default=None)


def current_meter() -> UsageMeter | None:
    return _current.get()


def activate(meter: UsageMeter) -> Token[UsageMeter | None]:
    """在当前上下文装上 `meter`，返回撤下用的 token。成对使用，或直接用 `usage_scope`。

    **调用方必须自己装，不能沿用继承来的**：AutoPilot 的自动 continue 是在 `run_scene`
    内部 `create_task` 起下一轮的，新任务复制了父任务的计数器，不重新装两轮就混在一起。
    """
    return _current.set(meter)


def deactivate(token: Token[UsageMeter | None]) -> None:
    _current.reset(token)


@contextmanager
def usage_scope(meter: UsageMeter | None = None) -> Iterator[UsageMeter]:
    """在本上下文里装上 `meter`（缺省新建一个），退出时撤下。

    嵌套时内层独占：评估装自己的计数器，它的调用不会再记到场景上。
    """
    meter = meter if meter is not None else UsageMeter()
    token = activate(meter)
    try:
        yield meter
    finally:
        deactivate(token)


def purpose_key(purpose: str | LLMPurpose | None) -> str:
    """漏标的调用归入 untagged：在统计里可见，而不是静默丢失。"""
    if isinstance(purpose, LLMPurpose):
        return purpose.value
    return purpose or LLMPurpose.UNTAGGED.value


def total(stats: Mapping[str, LLMUsageStat]) -> LLMUsageStat:
    """所有用途的合计。"""
    out = LLMUsageStat()
    for stat in stats.values():
        for name in _INT_FIELDS:
            setattr(out, name, getattr(out, name) + getattr(stat, name))
        out.seconds += stat.seconds
    return out


def _count(value: object) -> int | None:
    # bool 是 int 的子类，True 不能读成 1 次调用；负数与非整数一律不认
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def token_count(value: object) -> int | None:
    """服务商 `usage` 里的一个 token 数；结构不对时为 None，调用方据此改用估算。"""
    return _count(value)


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def deserialize_usage(raw: object, label: str) -> dict[str, LLMUsageStat]:
    """还原落盘的计数。旧数据缺键即空计数；内容损坏降级并 warning，不得让列表五百。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("%s的 LLM 计数不是对象，按空计数处理：%r", label, raw)
        return {}
    out: dict[str, LLMUsageStat] = {}
    for purpose, item in raw.items():
        if not isinstance(item, dict):
            logger.warning("%s的 LLM 计数条目 %r 不是对象，已跳过", label, purpose)
            continue
        stat = LLMUsageStat()
        for name in _INT_FIELDS:
            value = _count(item.get(name, 0))
            if value is None:
                logger.warning("%s的 LLM 计数 %s.%s 非法，按 0 处理：%r", label, purpose, name, item.get(name))
                value = 0
            setattr(stat, name, value)
        seconds = _seconds(item.get("seconds", 0.0))
        if seconds is None:
            logger.warning("%s的 LLM 计数 %s.seconds 非法，按 0 处理：%r", label, purpose, item.get("seconds"))
            seconds = 0.0
        stat.seconds = seconds
        out[str(purpose)] = stat
    return out
