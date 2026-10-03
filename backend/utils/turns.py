"""轮次的统一渲染、感知判定与计数口径（工单24/20 PR-0）。

三件事各只有一处实现，理由相同：环境回合（`TurnKind.ENVIRONMENT`）落地时要改的是
"一轮怎么呈现 / 谁能看见什么 / 算不算一轮"，散在各处各写一份就会出现
"导演看得见、角色看不见"或"现场写入与续跑重放对不上"（CLAUDE.md 陷阱 13、工单26 复盘 9/10）。

不在这里的：`EpisodicMemory._snippet` 的条目格式——它是序列化格式，老快照与重放去重
靠逐字相同，不能并进来（设计单 R9）；selector 点名检测与停滞签名是信号文本，不是渲染。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.models import DialogueTurn, TurnKind


@dataclass(frozen=True)
class Perception:
    """某个观察者对一轮的感知范围。

    现场写入记忆与续跑重放都只从 `perceive` 拿它，不自行判定 —— 两条路径各判一次
    迟早漂移，而事件摘要靠两边逐字相同去重。
    """

    is_self: bool
    # 内心独白只有本人可见（契约1）：他人既不能读到，也不能拿它参与重要性判定
    inner_thought: bool


def perceive(turn: DialogueTurn, viewer_id: str) -> Perception:
    """`viewer_id` 这个角色对 `turn` 能感知到什么。"""
    is_self = bool(viewer_id) and turn.character_id == viewer_id
    return Perception(is_self=is_self, inner_thought=is_self)


def render_turn(turn: DialogueTurn, *, inner_thought: bool = False) -> str:
    """把一轮渲染成 `角色名: *动作* 对白 [独白]` 的单行。

    不去首尾空白：记忆文本是长期记忆的寻址键（`long_term.memory_id`），改一个字符就是
    一条新记录，续跑重放会与已入库的旧文本对不上。需要去空白的调用方（角色看到的
    "目前对话"）自己 strip。
    """
    parts = []
    if turn.action:
        parts.append(f"*{turn.action}*")
    if turn.dialogue:
        parts.append(turn.dialogue)
    if inner_thought and turn.inner_thought:
        parts.append(f"[{turn.inner_thought}]")
    return f"{turn.character_name}: {' '.join(parts)}"


def is_character_turn(turn: DialogueTurn) -> bool:
    # 只认 environment 为环境回合：非法取值按角色轮次算，至少不会让 max_turns 失效
    return turn.kind != TurnKind.ENVIRONMENT.value


def character_turns(turns: Sequence[DialogueTurn]) -> list[DialogueTurn]:
    """只含角色轮次。max_turns、轮询选人、停滞检测都按它计数（设计单 §5.5）。"""
    return [t for t in turns if is_character_turn(t)]


def count_character_turns(turns: Sequence[DialogueTurn]) -> int:
    return sum(1 for t in turns if is_character_turn(t))
