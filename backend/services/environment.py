"""环境层的预算、规整与渲染（工单20）。

一层纯函数：无状态、不碰 IO、不调 LLM（同 `world_state.py` / `storyboard.py` / `objects.py`，
拆出来是为了让 `repository` 在读取那一刻就能压回预算，而不引入 import 环）。

`Scene.environment_state` 是场景内的物件公开状态，形如 `{object_id: {属性: 值}}`：
- **按 object_id 而不是物件名归属**（PR-2a 评审）：名称可改、旧数据里还可能重名，按名称拼键
  的话，改名后读不回旧状态，两个同名物件会共用一份状态，裁决基于错误的状态；
- 进每个在场角色 **user** 消息里的【当前环境】块（契约3 补充条款），渲染时才按物件**当前**的
  名字拼成 `物件名·属性`；按轮次计费，所以有预算；
- 值为 None 表示本场清除了该属性，渲染时跳过（PR-2b 跨场延续时据此删除）。

两道闸门：写入侧（裁决产出的状态变化，超预算**拒掉这一条**、叙述照常保留）；
读取侧 `clamp_environment_state`（场景 JSON 可被人工编辑或是旧数据，**只压不写回**）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from backend.models import ActionStatus, DialogueTurn, RevealEntry
from backend.services.world_state import normalize_world_value
from backend.utils.context import ContextBudget, fit_lines
from backend.utils.llm import estimate_tokens
from backend.utils.logger import get_logger
from backend.utils.turns import is_character_turn

logger = get_logger("services.environment")

#: 渲染【当前环境】时物件名与属性名之间的分隔符（`物件名·属性`）
STATE_KEY_SEPARATOR = "·"
#: 属性名上限。属性名会原样进 prompt，长了说明模型在拿它当句子用
ENVIRONMENT_ATTR_CHARS = 12
#: 场景内物件状态的总条数（所有物件的属性加起来）与总预算。它进每个角色、每一轮的 user 消息
MAX_ENVIRONMENT_STATE_KEYS = 12
ENVIRONMENT_STATE_BUDGET_TOKENS = 400
#: 预算估算时给物件名留的份额：名字渲染时才拼上，规整阶段只知道 ID
_NAME_COST_TOKENS = 8

EnvironmentState = dict[str, dict[str, str | None]]


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def normalize_attribute(raw: object) -> str:
    """属性名：塌单行、截到上限、不许带分隔符（渲染出来会被读成另一个物件的属性）。空串 = 不可用。"""
    attr = _one_line(raw)[:ENVIRONMENT_ATTR_CHARS]
    return "" if STATE_KEY_SEPARATOR in attr else attr


def _cost(attr: str, value: str | None) -> int:
    # 清除标记不渲染，但要占一条：跨场延续时它是一次删除
    return estimate_tokens(f"- {attr}：{value or ''}") + _NAME_COST_TOKENS


def _flatten(state: Mapping[str, Mapping[str, str | None]]) -> list[tuple[str, str, str | None]]:
    return [(oid, attr, value) for oid, attrs in state.items() for attr, value in attrs.items()]


def _nest(entries: list[tuple[str, str, str | None]]) -> EnvironmentState:
    out: EnvironmentState = {}
    for oid, attr, value in entries:
        out.setdefault(oid, {})[attr] = value
    return out


def _within_budget(entries: list[tuple[str, str, str | None]]) -> bool:
    return (
        len(entries) <= MAX_ENVIRONMENT_STATE_KEYS
        and sum(_cost(attr, value) for _, attr, value in entries) <= ENVIRONMENT_STATE_BUDGET_TOKENS
    )


def clamp_environment_state(raw: object, label: str = "") -> EnvironmentState:
    """把一份**来历不明**的场景物件状态压回形状与预算。读取侧闸门，只压不写回。

    外层键是物件 ID（塌单行、非空），内层是属性 → 值；值塌单行并限单值预算，None 与空串都按
    "已清除"。超出条数或总预算时保留**最近写入**的（写入侧每次都把改动的物件与属性挪到最后）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("%s的场景物件状态不是对象，按空处理：%r", label, raw)
        return {}
    entries: list[tuple[str, str, str | None]] = []
    dropped: list[str] = []
    for raw_oid, raw_attrs in raw.items():
        oid = _one_line(raw_oid)
        if not oid or not isinstance(raw_attrs, dict):
            dropped.append(str(raw_oid))
            continue
        for raw_attr, raw_value in raw_attrs.items():
            attr = normalize_attribute(raw_attr)
            if not attr:
                dropped.append(f"{oid}.{raw_attr}")
                continue
            entries = [e for e in entries if e[:2] != (oid, attr)]
            entries.append((oid, attr, normalize_world_value(raw_value) or None))
    kept: list[tuple[str, str, str | None]] = []
    for entry in reversed(entries):
        if _within_budget([entry, *kept]) or not kept:
            kept.insert(0, entry)
        else:
            dropped.append(f"{entry[0]}.{entry[1]}")
    if dropped:
        logger.warning("%s的场景物件状态有 %d 项形状非法或超出预算，已忽略：%s",
                       label, len(dropped), "、".join(dropped))
    return _nest(kept)


def describe_environment_state(state: EnvironmentState | None, names: Mapping[str, str]) -> str:
    """【当前环境】块的正文：按物件**当前**的名字渲染 `- 物件名·属性：值`，一行一条。

    `names` 是本场在场物件的 ID → 名称；不在其中的（已删除、已不在场）不渲染，已清除的属性
    也不渲染。没有可渲染的内容时返回空串。
    """
    lines = [
        f"- {names[oid]}{STATE_KEY_SEPARATOR}{attr}：{value}"
        for oid, attrs in (state or {}).items()
        if oid in names
        for attr, value in attrs.items()
        if value
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 裁决（工单20 PR-2a）：输入组装、输出解析、状态变化校验
# ---------------------------------------------------------------------------

#: 揭示来源（设计单 §3），由解析结果决定、不由裁决器声明：命中了预制揭示就是 ①，否则 ③。
#: ② 现场生成要拆成独立调用（A13），归 20b
REVEAL_SCRIPT = "script"  # ① 导演预制
REVEAL_GENERATE = "generate"  # ② 现场生成（20b）
REVEAL_OBSERVABLE = "observable"  # ③ 信息不足，只给可观察现象

#: 公开叙述进全场 transcript 与每个在场角色的记忆，私密细节进当事人记忆：都要有上限
NARRATION_TOKENS = 120
PRIVATE_DETAIL_TOKENS = 200
#: 一次裁决最多改几个属性。超出的拒掉并记原因
MAX_STATE_CHANGES = 4


@dataclass
class Adjudication:
    """一次环境裁决的结果（已校验、已规整）。

    `state_changes` 的键是**属性名**，不是 `物件名·属性`：物件由调用方决定，裁决器在结构上
    就改不了别的物件（"只许动本物件"）。值为 None = 清除该属性。
    `rejected` 记下解析阶段丢掉的东西，调用方 warning，不静默。
    """

    executable: bool = True
    triggered: bool = False
    reveal_source: str = REVEAL_OBSERVABLE
    narration: str = ""
    private_detail: str = ""
    state_changes: dict[str, str | None] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)


def _fit_one_line(raw: object, max_tokens: int) -> str:
    if raw is None:
        return ""
    text = _one_line(raw)
    return fit_lines([text], ContextBudget(max_tokens=max_tokens)).text if text else ""


def _strict_bool(value: object) -> bool | None:
    # 与导演的 _parse_bool 同一口径："false" 不能变成 True；拿不准返回 None 由调用方定缺省
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _extract_object(raw: str) -> dict | None:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#: 一个物件最多几条预制揭示，以及每条的上限。条件进裁决 prompt，内容进执行者记忆
MAX_REVEAL_ENTRIES = 6
REVEAL_CONDITION_TOKENS = 60


def normalize_reveals(entries: Sequence[RevealEntry] | None) -> list[RevealEntry]:
    """规整预制揭示：塌单行、限长度，丢掉条件或内容为空的条目，超出条数的截掉并 warning。

    **裁决 prompt 与解析必须用同一份规整结果**：裁决器回答的是"第几条"，两边列表不一致，
    编号就指向了另一条揭示。
    """
    kept: list[RevealEntry] = []
    for entry in entries or []:
        condition = _fit_one_line(entry.condition, REVEAL_CONDITION_TOKENS)
        content = _fit_one_line(entry.content, PRIVATE_DETAIL_TOKENS)
        if not condition or not content:
            continue
        if len(kept) >= MAX_REVEAL_ENTRIES:
            logger.warning("预制揭示超过 %d 条，其余不参与裁决", MAX_REVEAL_ENTRIES)
            break
        kept.append(RevealEntry(condition=condition, content=content))
    return kept


def _reveal_index(value: object) -> int | None:
    """裁决器给的条目编号（从 1 起）；0 / null = 不揭示。其余取值返回 -1 表示非法。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return -1
    return value or None


def parse_adjudication(raw: str, *, reveals: Sequence[RevealEntry]) -> Adjudication | None:
    """把裁决器的输出收进可控形状。返回 None = 裁决失败（动作记 failed，**不生成环境回合**）。

    失败不伪造结果（设计单 §5.7）：没有公开叙述的裁决等于没裁决，绝不能补一句"毫无反应"。
    `executable` / `triggered` 同理是必填：缺失或不是布尔就是输出不可用，整次裁决按失败处理。
    不能替模型补缺省 —— 补成"可执行"会让残缺的输出也改写物件状态，补成"不可执行"又会让
    写着"王冠亮起"的叙述与没动过的状态对不上。

    私密细节**不由裁决器书写**（设计单 A31）：它只回答命中第几条预制揭示（`reveal_index`），
    细节由这里原样取那一条的 content。裁决器同时看着隐藏规则，让它写细节就可能夹带秘密；
    让它从一整段自由文本里摘录，又可能把所有分支整段交出去。按条选之后：
    - 每次最多给一条，跨分支泄露在结构上不可能；
    - 只在规则**真正触发**时给（没触发、含不可执行，都不给 —— 否则导演安排的秘密会进一个
      没满足条件的角色的记忆，契约1）；
    - 编号越界、类型不对一律不给；裁决器自己写了 `private_detail` 也不采纳。
    `reveals` 必须是裁决 prompt 用的那一份规整结果（`normalize_reveals`）。
    """
    data = _extract_object(raw)
    if data is None:
        return None
    narration = _fit_one_line(data.get("narration"), NARRATION_TOKENS)
    if not narration:
        return None
    executable = _strict_bool(data.get("executable"))
    triggered = _strict_bool(data.get("triggered"))
    if executable is None or triggered is None:
        logger.warning("环境裁决缺少或给错了 executable / triggered，按失败处理：%r / %r",
                       data.get("executable"), data.get("triggered"))
        return None
    result = Adjudication(narration=narration, executable=executable)
    # 做不到的动作谈不上触发
    result.triggered = triggered and executable
    if data.get("private_detail"):
        result.rejected.append("裁决器自己写的私密细节不采纳（只认预制揭示的编号）")
    index = _reveal_index(data.get("reveal_index"))
    if index == -1 or (index is not None and not 1 <= index <= len(reveals)):
        result.rejected.append(f"预制揭示编号非法：{data.get('reveal_index')!r}")
    elif index is not None and not result.triggered:
        result.rejected.append("规则未触发，不揭示导演预制内容")
    elif index is not None:
        result.reveal_source = REVEAL_SCRIPT
        result.private_detail = reveals[index - 1].content
    changes = data.get("state_changes")
    if changes is None:
        changes = {}
    if not isinstance(changes, dict):
        result.rejected.append(f"state_changes 不是对象：{type(changes).__name__}")
        changes = {}
    if changes and not result.executable:
        result.rejected.append("动作不可执行却改了物件状态，已丢弃")
        changes = {}
    for attr, value in changes.items():
        if len(result.state_changes) >= MAX_STATE_CHANGES:
            result.rejected.append(f"一次裁决最多改 {MAX_STATE_CHANGES} 个属性，其余丢弃")
            break
        attr_text = normalize_attribute(attr)
        if not attr_text:
            result.rejected.append(f"属性名非法：{attr!r}")
            continue
        result.state_changes[attr_text] = normalize_world_value(value) or None
    return result


def remaining_environment_quota(turns: Sequence[DialogueTurn], limit: int) -> int:
    """本场还能产生几个环境回合（设计单 A15）：上限 − 已有的环境回合 − 仍 pending 的动作。

    pending 必须计入：续跑补裁决会把它们变成环境回合，不计入则续跑后超限。
    failed / skipped 不占额度 —— 它们不会再产生回合。可能为负（上限被调小），调用方按 <= 0 处理。
    """
    produced = sum(1 for t in turns if not is_character_turn(t))
    pending = sum(
        1 for t in turns for a in t.actions if a.status == ActionStatus.PENDING.value
    )
    return limit - produced - pending


def object_state_view(object_id: str, environment_state: EnvironmentState | None) -> dict[str, str]:
    """裁决器看到的某个物件在本场的当前状态：属性 → 值（已清除的不含）。

    按 object_id 取，改名、重名都不影响（PR-2a 评审）。跨场延续下来的状态归 PR-2b：
    原定从世界变量里按物件名前缀读（A26），但名称不是稳定身份，那条路要随 A5 一起重议。
    """
    attrs = (environment_state or {}).get(object_id, {})
    return {attr: value for attr, value in attrs.items() if value}


def apply_state_changes(
    state: EnvironmentState,
    object_id: str,
    changes: Mapping[str, str | None],
) -> tuple[EnvironmentState, list[str]]:
    """把一次裁决的状态变化并进场景物件状态，返回 (新状态, 被拒的属性)。不改入参。

    只写 `object_id` 这一个物件：物件由调用方决定，裁决器在结构上改不了别的物件。
    写入侧闸门：超出条数或总预算时**拒掉这一条变化**，不淘汰已有的 —— 已有的是本场
    更早裁决出的事实，被一条新变化挤掉的话，角色【当前环境】里会无声少一行。
    被拒的变化由调用方 warning；叙述照常保留（它是已经裁决出的事实）。
    改写过的物件与属性挪到最后："最近写入"排在最后，读取侧淘汰时据此保留新的。
    """
    entries = _flatten(state)
    rejected: list[str] = []
    for raw_attr, value in changes.items():
        attr = normalize_attribute(raw_attr)
        if not attr:
            rejected.append(str(raw_attr))
            continue
        # 改动的物件整块挪到最后、改动的属性排在块尾：嵌套之后展平，顺序仍是"最近写入在最后"
        others = [e for e in entries if e[0] != object_id]
        mine = [e for e in entries if e[0] == object_id and e[1] != attr]
        candidate = [*others, *mine, (object_id, attr, value)]
        if not _within_budget(candidate):
            rejected.append(attr)
            continue
        entries = candidate
    return _nest(entries), rejected
