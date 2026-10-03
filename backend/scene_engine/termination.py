"""场景终止条件判断。"""

from __future__ import annotations

from backend.models import DialogueTurn
from backend.utils.turns import character_turns

# 外部中断（pause 接口）的终止原因。AutoPilot 靠它区分"用户踩了刹车"与正常收场：
# 中断走的是正常终止路径，场景照样是 completed（CLAUDE.md 12.1）。
INTERRUPTED_REASON = "导演中断"


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

    轮次上限与停滞都只数角色轮次（工单24/20 设计单 §5.5）：环境回合不计入 max_turns，
    连续几个环境回合也不该被判成"对话停滞"。
    """
    if director_interrupt:
        return True, INTERRUPTED_REASON
    spoken = character_turns(turns)
    if len(spoken) >= max_turns:
        return True, "达到最大轮次"
    if len(spoken) >= 4:
        recent = [_signature(t) for t in spoken[-4:]]
        # 检测近乎重复（停滞）
        unique = {r.strip() for r in recent if r.strip()}
        if len(unique) <= 1:
            return True, "对话停滞"
    return False, ""
