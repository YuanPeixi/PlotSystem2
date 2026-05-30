"""场景终止条件判断。"""

from __future__ import annotations

from backend.models import DialogueTurn


def _signature(turn: DialogueTurn) -> str:
    return (turn.dialogue or "") + (turn.action or "")


def check_termination(
    turns: list[DialogueTurn],
    max_turns: int,
    director_interrupt: bool = False,
) -> tuple[bool, str]:
    """检查是否应终止场景。返回 (是否终止, 原因)。

    满足任一即停止：
    - 已达 max_turns
    - 导演中断信号
    - 连续 3 轮无新信息（停滞检测）
    """
    if director_interrupt:
        return True, "导演中断"
    if len(turns) >= max_turns:
        return True, "达到最大轮次"
    if len(turns) >= 4:
        recent = [_signature(t) for t in turns[-4:]]
        # 检测近乎重复（停滞）
        unique = {r.strip() for r in recent if r.strip()}
        if len(unique) <= 1:
            return True, "对话停滞"
    return False, ""
