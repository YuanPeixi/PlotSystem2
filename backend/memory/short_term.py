"""短期记忆：对话窗口缓冲。无需检索，按时间顺序保留最近 N 条。"""

from __future__ import annotations

from collections import deque
from typing import Any

from backend.config import settings
from backend.utils.logger import get_logger

logger = get_logger("memory.short_term")


class ShortTermMemory:
    """固定容量的对话缓冲。"""

    def __init__(self, capacity: int | None = None):
        self.capacity = capacity or settings.SHORT_TERM_BUFFER_SIZE
        self._buffer: deque[str] = deque(maxlen=self.capacity)
        # 每条记忆的元数据（重要性/发言者/是否本人），仅进程内使用，
        # 不参与 dump()/load() 的快照契约（工单15：避免影响持久化格式）。
        self._meta: deque[dict[str, Any]] = deque(maxlen=self.capacity)
        self._overflow_warned = False

    def add(self, text: str, **meta: Any) -> None:
        # deque 满了会静默丢弃最早一条。工单26 之后缓冲只应涨到
        # MEMORY_CONSOLIDATE_EVERY_TURNS 就被引擎清空，真发生淘汰说明固化链路
        # 没跑到（周期配得比容量大、或引擎没调 _consolidate_all），此时被丢掉的
        # 内容还没进过长期记忆。只警告一次，避免每轮刷屏。
        if len(self._buffer) >= self.capacity and not self._overflow_warned:
            self._overflow_warned = True
            logger.warning(
                "[memory] 短期缓冲已达容量 %d 仍在写入，最早的记录将被丢弃且未固化；"
                "请检查 MEMORY_CONSOLIDATE_EVERY_TURNS(%d) 是否小于 SHORT_TERM_BUFFER_SIZE",
                self.capacity,
                settings.MEMORY_CONSOLIDATE_EVERY_TURNS,
            )
        self._buffer.append(text)
        self._meta.append(meta)

    def recent(self, n: int | None = None) -> list[str]:
        items = list(self._buffer)
        if n is None:
            return items
        return items[-n:]

    def is_full(self) -> bool:
        return len(self._buffer) >= self.capacity

    def pressure(self) -> float:
        """缓冲占用率（0.0~1.0+）。供 SceneEngine 判断是否必须提前固化。

        长期记忆的唯一入口是本缓冲，而它是定长 deque：写满后继续 append 会静默
        淘汰最早的条目，那些内容还没进过长期记忆就永久消失了（工单26 复盘）。
        因此"还能装多少"必须是可查询的，不能只靠配置期的静态假设 —— `prime()`
        回填、多角色写入倍率都会让实际占用偏离固化周期的预期。
        """
        if self.capacity <= 0:
            return 1.0
        return len(self._buffer) / self.capacity

    def clear(self) -> None:
        self._buffer.clear()
        self._meta.clear()

    def dump(self) -> list[str]:
        return list(self._buffer)

    def dump_with_meta(self) -> list[tuple[str, dict[str, Any]]]:
        return list(zip(self._buffer, self._meta))

    def load(self, items: list[str]) -> None:
        self._buffer = deque(items, maxlen=self.capacity)
        self._meta = deque(({} for _ in items), maxlen=self.capacity)

    def load_with_meta(self, items: list[tuple[str, dict[str, Any]]]) -> None:
        """与 dump_with_meta() 对称的恢复方法，保留重要性/发言者等元数据。"""
        self._buffer = deque((text for text, _ in items), maxlen=self.capacity)
        self._meta = deque((dict(meta) for _, meta in items), maxlen=self.capacity)
