"""短期记忆：对话窗口缓冲。无需检索，按时间顺序保留最近 N 条。"""

from __future__ import annotations

from collections import deque

from backend.config import settings


class ShortTermMemory:
    """固定容量的对话缓冲。"""

    def __init__(self, capacity: int | None = None):
        self.capacity = capacity or settings.SHORT_TERM_BUFFER_SIZE
        self._buffer: deque[str] = deque(maxlen=self.capacity)

    def add(self, text: str) -> None:
        self._buffer.append(text)

    def recent(self, n: int | None = None) -> list[str]:
        items = list(self._buffer)
        if n is None:
            return items
        return items[-n:]

    def is_full(self) -> bool:
        return len(self._buffer) >= self.capacity

    def clear(self) -> None:
        self._buffer.clear()

    def dump(self) -> list[str]:
        return list(self._buffer)

    def load(self, items: list[str]) -> None:
        self._buffer = deque(items, maxlen=self.capacity)
