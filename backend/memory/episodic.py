"""事件摘要记忆。检测重要事件并生成摘要文本，存入长期记忆。"""

from __future__ import annotations

from backend.models import DialogueTurn
from backend.utils.logger import get_logger

logger = get_logger("memory.episodic")

# 触发重要事件的关键词（简单启发式，可被 LLM 检测增强）
_IMPORTANT_KEYWORDS = [
    "死", "杀", "背叛", "结盟", "决定", "发誓", "秘密", "真相",
    "离开", "归来", "战争", "和平", "爱", "恨", "目标",
]


class EpisodicMemory:
    """事件摘要记忆，负责标记重要事件。"""

    def __init__(self, character_id: str):
        self.character_id = character_id
        self.summary: str = ""
        self._events: list[str] = []

    def is_important(self, turn: DialogueTurn) -> bool:
        """启发式判断一轮对话是否构成重要事件。"""
        text = " ".join(
            t for t in (turn.dialogue, turn.action, turn.inner_thought) if t
        )
        return any(kw in text for kw in _IMPORTANT_KEYWORDS)

    def record(self, turn: DialogueTurn) -> str | None:
        """若为重要事件则记录并返回摘要文本，否则返回 None。"""
        if not self.is_important(turn):
            return None
        parts = []
        if turn.action:
            parts.append(f"（{turn.action}）")
        if turn.dialogue:
            parts.append(turn.dialogue)
        snippet = f"[重要] {turn.character_name}: {' '.join(parts)}".strip()
        self._events.append(snippet)
        return snippet

    def build_summary(self) -> str:
        """汇总所有重要事件为摘要文本。"""
        if not self._events:
            return self.summary
        self.summary = "\n".join(self._events[-10:])
        return self.summary

    def dump(self) -> str:
        return self.build_summary()

    def load(self, summary: str) -> None:
        self.summary = summary or ""
        if summary:
            self._events = summary.split("\n")
