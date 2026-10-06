"""环境层的预算、规整与渲染（工单20）。

一层纯函数：无状态、不碰 IO、不调 LLM（同 `world_state.py` / `storyboard.py` / `objects.py`，
拆出来是为了让 `repository` 在读取那一刻就能压回预算，而不引入 import 环）。

`Scene.environment_state` 是场景内的物件公开状态，键为 `物件名·属性`：
- 进每个在场角色 **user** 消息里的【当前环境】块（契约3 补充条款），按轮次计费，所以有预算；
- 场景结束时并入分支世界变量（PR-2b），所以键的形状必须同时是合法的世界变量名
  （`world_state.WORLD_KEY_CHARS` 以内、单行）；
- 值为 None 表示本场清除了该属性，渲染时跳过，并入世界变量时据此删除。

两道闸门：写入侧（裁决产出的状态变化，超预算**拒掉这一条**、叙述照常保留）；
读取侧 `clamp_environment_state`（场景 JSON 可被人工编辑或是旧数据，**只压不写回**）。
"""

from __future__ import annotations

from backend.services.world_state import WORLD_KEY_CHARS, normalize_world_value
from backend.utils.llm import estimate_tokens
from backend.utils.logger import get_logger

logger = get_logger("services.environment")

#: 物件名与属性名之间的分隔符（设计单 A5：`物件名·属性`）
STATE_KEY_SEPARATOR = "·"
#: 属性名上限。物件名最长 24 字（`objects.OBJECT_NAME_CHARS`），加分隔符与属性仍在
#: 世界变量键长上限之内，并入世界变量时不会被截断
ENVIRONMENT_ATTR_CHARS = 12
#: 场景内物件状态的条数与总预算。它进每个角色、每一轮的 user 消息
MAX_ENVIRONMENT_STATE_KEYS = 12
ENVIRONMENT_STATE_BUDGET_TOKENS = 400


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def state_key(object_name: str, attribute: str) -> str:
    """拼出 `物件名·属性`。任一部分规整后为空、或属性里带分隔符时返回空串（不可用）。"""
    name = _one_line(object_name)
    attr = _one_line(attribute)[:ENVIRONMENT_ATTR_CHARS]
    if not name or not attr or STATE_KEY_SEPARATOR in attr:
        return ""
    key = f"{name}{STATE_KEY_SEPARATOR}{attr}"
    return key if len(key) <= WORLD_KEY_CHARS else ""


def split_state_key(key: str) -> tuple[str, str] | None:
    """`物件名·属性` → (物件名, 属性)；形状不对返回 None。物件名本身可以含分隔符，按最后一个切。"""
    name, sep, attr = str(key).rpartition(STATE_KEY_SEPARATOR)
    if not sep or not name.strip() or not attr.strip():
        return None
    return name, attr


def _normalize_key(raw_key: object) -> str:
    parts = split_state_key(_one_line(raw_key))
    return state_key(*parts) if parts else ""


def _cost(key: str, value: str | None) -> int:
    # 清除标记不渲染，但要占一条：并入世界变量时它是一次删除
    return estimate_tokens(f"- {key}：{value or ''}")


def clamp_environment_state(raw: object, label: str = "") -> dict[str, str | None]:
    """把一份**来历不明**的场景物件状态压回形状与预算。读取侧闸门，只压不写回。

    键必须是 `物件名·属性`、塌单行；值塌单行并限单值预算，None 与空串都按"已清除"。
    超出条数或总预算时保留**最近写入**的（dict 保持插入序，裁决每次写入都先 pop 再插入）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("%s的场景物件状态不是对象，按空处理：%r", label, raw)
        return {}
    normalized: dict[str, str | None] = {}
    dropped: list[str] = []
    for raw_key, raw_value in raw.items():
        key = _normalize_key(raw_key)
        if not key:
            dropped.append(str(raw_key))
            continue
        normalized.pop(key, None)
        normalized[key] = normalize_world_value(raw_value) or None
    kept: list[tuple[str, str | None]] = []
    used = 0
    for key, value in reversed(list(normalized.items())):
        cost = _cost(key, value)
        if len(kept) >= MAX_ENVIRONMENT_STATE_KEYS or (kept and used + cost > ENVIRONMENT_STATE_BUDGET_TOKENS):
            dropped.append(key)
            continue
        kept.append((key, value))
        used += cost
    if dropped:
        logger.warning("%s的场景物件状态有 %d 项形状非法或超出预算，已忽略：%s",
                       label, len(dropped), "、".join(dropped))
    return dict(reversed(kept))


def describe_environment_state(state: dict[str, str | None] | None) -> str:
    """【当前环境】块的正文，一行一条；已清除的属性不渲染。没有可渲染的内容时返回空串。"""
    lines = [f"- {k}：{v}" for k, v in (state or {}).items() if v]
    return "\n".join(lines)
