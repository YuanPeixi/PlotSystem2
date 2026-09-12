"""事件摘要记忆。检测重要事件并生成摘要文本，存入长期记忆。"""

from __future__ import annotations

from collections.abc import Sequence

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

    def is_important(self, turn: DialogueTurn, include_inner_thought: bool = True) -> bool:
        """启发式判断一轮对话是否构成重要事件。

        include_inner_thought=False 用于记录他人轮次时：不得用其他角色的私有内心
        独白参与判定或被写入摘要（CLAUDE.md 第7节“契约1”）。
        """
        texts = [turn.dialogue, turn.action]
        if include_inner_thought:
            texts.append(turn.inner_thought)
        text = " ".join(t for t in texts if t)
        return any(kw in text for kw in _IMPORTANT_KEYWORDS)

    def record(self, turn: DialogueTurn, include_inner_thought: bool = True) -> bool:
        """若为重要事件则计入摘要，返回是否重要。

        不再返回可另外写入长期记忆的正文副本——摘要只用于 dump()/续跑回填，
        原文由调用方统一走 add_experience → consolidate 一次性入库（工单15去重）。
        """
        snippet = self._snippet(turn, include_inner_thought=include_inner_thought)
        if snippet is None:
            return False
        self._events.append(snippet)
        return True

    def replay(self, turns: Sequence[DialogueTurn], *, self_character_id: str = "") -> None:
        """按场景日志重建这批轮次的事件条目，**幂等**（工单26 复盘·断言4）。

        续跑时这批轮次可能已经由 `prime()` 载入过一部分：`resolve_scene_states`
        的四级优先级里，正常 continue 命中的 `snapshot_id_after` 是本场上一次
        跑完时打的，其 `episodic_summary` 已含这些事件；崩溃续跑命中的
        `snapshot_id_before` 则一条都不含；continue 跑到一半再崩会落在两者之间
        （快照只覆盖前半段）。**判断不了"载入了多少"，所以不去判断** —— 先按
        正文剔除本批已存在的条目，再整批按日志顺序追加，三种来源收敛到同一结果。

        逐条 append 的写法在正常 continue 上会把整段翻倍，而 `_events` 只保留
        末 10 条，等于把更早的重要事件挤出窗口（永久丢失，episodic 不落盘）。

        代价与长期记忆的内容寻址一致：同一角色说出**完全相同**的一句话会合并
        成一条。
        """
        wanted: list[str] = []
        for turn in turns:
            snippet = self._snippet(
                turn, include_inner_thought=turn.character_id == self_character_id
            )
            if snippet is not None:
                wanted.append(snippet)
        if not wanted:
            return
        seen = set(wanted)
        self._events = [e for e in self._events if e not in seen] + wanted

    def _snippet(self, turn: DialogueTurn, *, include_inner_thought: bool = True) -> str | None:
        """渲染一条事件摘要；不构成重要事件时返回 None。

        内心独白只参与重要性判定、不进正文——摘要会被他人可见的路径读取（契约1）。
        """
        if not self.is_important(turn, include_inner_thought=include_inner_thought):
            return None
        parts = []
        if turn.action:
            parts.append(f"（{turn.action}）")
        if turn.dialogue:
            parts.append(turn.dialogue)
        return f"[重要] {turn.character_name}: {' '.join(parts)}".strip()

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
