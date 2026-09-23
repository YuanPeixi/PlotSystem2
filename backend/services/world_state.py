"""世界变量的规范化、预算与渲染（工单07）。

一层纯函数：无状态、不碰 IO、不调 LLM。从 `agents/director_agent.py` 拆出来的**唯一
理由是 import 方向** —— `repository` 必须在**读文件的那一刻**就把世界变量压回预算
（`world_state/{branch_id}.json` 是可人工编辑的文件，绕过了 `merge_world_variables`
这道写入侧闸门），而 `repository → director_agent → services.inspection → repository`
是一个环。`director_agent` 仍从这里再导出同名函数，老 import 路径不变。

两道闸门缺一不可（与"固化周期 + 缓冲压力"同一种结构）：
- 写入侧 `merge_world_variables`：挡住导演产出的 delta；
- 读取侧 `clamp_world_variables`：挡住人工编辑、以及立预算之前落盘的历史数据。
"""

from __future__ import annotations

from backend.models import MAX_WORLD_VARIABLES, RESERVED_SCENE_CONTEXT_KEYS
from backend.utils.context import ContextBudget, fit_lines
from backend.utils.llm import estimate_tokens
from backend.utils.logger import get_logger

logger = get_logger("services.world_state")

#: 世界变量的总预算与单值上限（工单07）。与 `unresolved_threads` 同一条教训，但更紧迫：
#: 线索只进导演上下文，世界变量进**每一场、每个角色、每一轮**的 system prompt，
#: 超预算是按轮次计费的。
WORLD_BUDGET_TOKENS = 800
WORLD_VALUE_TOKENS = 60
#: 变量名上限。键会原样进 prompt，长键既浪费预算又说明模型在拿它当句子用。
WORLD_KEY_CHARS = 40


def normalize_world_key(raw_key) -> str:
    """把任意键收成**单行**且不超过长度上限的短标识。返回空串表示该键不可用。

    塌单行与 `normalize_world_value` 同一条理由，但只做在值上是做不全的：
    "一行一条"是**渲染出来的行**的不变量，而键值同在 `f"- {k}：{v}"` 一行里。
    键里留一个换行，就能在导演提示词与每个在场角色的 system prompt 里凭空多出
    一条看起来合法的世界变量（来源可以是评估 LLM 的 JSON，也可以是人工编辑的
    `world_state/{branch_id}.json`）。三道闸门都只按名字判保留字，不看形状，
    所以形状必须在这里一次收干净。
    """
    return " ".join(str(raw_key).split())[:WORLD_KEY_CHARS]


def normalize_world_value(raw_value) -> str:
    """把任意值收成**单行**且不超过单值预算的文本。返回空串表示"无值"。

    塌成单行不是洁癖：世界变量按"一行一条"渲染进 prompt，带换行的值会让同一条变量
    看起来像两条（与 episodic 条目的单行不变量同一道理）。
    """
    if raw_value is None:
        return ""
    text = " ".join(str(raw_value).split())
    if not text:
        return ""
    return fit_lines([text], ContextBudget(max_tokens=WORLD_VALUE_TOKENS)).text


def describe_world_state(variables: dict[str, str] | None) -> str:
    """把世界变量渲染成"一行一条"的文本块，供导演与角色的提示词共用。"""
    if not variables:
        return "（暂无世界层变量）"
    return "\n".join(f"- {k}：{v}" for k, v in variables.items())


def normalize_world_delta(value) -> dict[str, str | None]:
    """把 LLM 给的世界变量增量收进可控形状。

    `None` 必须原样保留 —— 它是"该变量不再成立，删掉"的唯一表达方式。没有删除语义的话，
    变量只增不减，迟早把预算占满，之后每一条新的世界事实都会被挤掉。
    空字符串按同义处理：模型表达"取消"时给空串和给 null 一样常见。
    """
    if not isinstance(value, dict):
        return {}
    delta: dict[str, str | None] = {}
    shadowed: list[str] = []
    truncated = 0
    for index, (raw_key, raw_value) in enumerate(value.items()):
        key = normalize_world_key(raw_key)
        if not key or key in delta:
            continue
        # 保留字在这里就拦掉：delta 会落进 evaluations 表、并被快照的 story_history
        # 复制，留着它等于在库里存一条"导演改了本场地点"的假记录。
        if key in RESERVED_SCENE_CONTEXT_KEYS:
            shadowed.append(key)
            continue
        text = normalize_world_value(raw_value)
        delta[key] = text or None
        # delta 自身也要限量：它会落进 evaluations 表并被快照的 story_history 复制
        if len(delta) >= MAX_WORLD_VARIABLES:
            truncated = len(value) - index - 1
            break
    if shadowed:
        logger.warning(
            "导演给出的世界变量与场景固有字段同名，已忽略：%s（世界变量只能补充场景上下文，不能改写场景本身）",
            "、".join(shadowed),
        )
    if truncated:
        # 与 `merge_world_variables` 的淘汰同一口径：世界事实被吃掉不会表现为报错，
        # 只表现为下一场角色忽然不知道某件事，不能静默。
        logger.warning(
            "导演给出的世界变量增量超过 %d 条上限，已截断丢弃其余 %d 条",
            MAX_WORLD_VARIABLES,
            truncated,
        )
    return delta


def merge_world_variables(
    current: dict[str, str] | None, delta: dict[str, str | None] | None
) -> tuple[dict[str, str], list[str]]:
    """应用增量并压回预算，返回 (合并结果, 被淘汰的键)。

    与 `_normalize_threads` 同一条教训：**限条数不等于限预算**。世界变量比线索更狠 ——
    它进的是每一场、每个角色、每一轮的 system prompt，超预算按轮次计费。

    超限时淘汰**最久未更新**的键：世界事实越旧越可能已经不成立，而最近一场刚写下的
    多半正在生效。被淘汰的键返回给调用方 warning，不静默丢。

    保留字（`RESERVED_SCENE_CONTEXT_KEYS`）一律淘汰：它们同名于场景固有字段，
    留下来会在 `_scene_context` 里把本场的地点/名称顶掉。
    """
    merged: dict[str, str] = {k: v for k, v in (current or {}).items()}
    for key, value in (delta or {}).items():
        # 先 pop 再插入：dict 保持插入序，重新插入等于把"最近更新"刷到队尾
        merged.pop(key, None)
        if value is not None:
            merged[key] = str(value)

    kept: list[tuple[str, str]] = []
    used = 0
    for key, value in reversed(list(merged.items())):
        if key in RESERVED_SCENE_CONTEXT_KEYS:
            continue
        if len(kept) >= MAX_WORLD_VARIABLES:
            break
        cost = estimate_tokens(f"- {key}：{value}")
        if kept and used + cost > WORLD_BUDGET_TOKENS:
            break
        kept.append((key, value))
        used += cost
    kept.reverse()
    result = dict(kept)
    return result, [k for k in merged if k not in result]


def clamp_world_variables(
    variables: dict[str, str] | None,
) -> tuple[dict[str, str], list[str]]:
    """把一份**来历不明**的世界变量压回与 delta 相同的形状与预算。

    读取侧闸门。`merge_world_variables` 只管得住导演写进来的那条路径，而
    `world_state/{branch_id}.json` 摆在项目目录里、明确支持人工编辑：手写一条
    五千字的变量，或是在立预算之前落盘的历史数据，都会绕过写入侧直接进
    每一轮的 system prompt。值一律转成字符串，下游按字符串拼接。
    """
    normalized: dict[str, str] = {}
    for raw_key, raw_value in (variables or {}).items():
        key = normalize_world_key(raw_key)
        text = normalize_world_value(raw_value)
        # 键为空拼出来是个无名变量；值为空则是一行"xxx："
        if not key or not text or key in normalized:
            continue
        normalized[key] = text
    return merge_world_variables(normalized, None)
