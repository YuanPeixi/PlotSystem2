"""统一上下文压缩管线（工单27）。

把"把一堆行装进 token 预算"这件事收敛成一个纯函数 + 一个可选的 LLM 压缩，
避免每个消费方（角色 / selector / 导演 / 总结）各写各的裁剪逻辑。

策略语义：
- ``block_drop``  超预算时一次性丢到水位线并回报新起点，供调用方缓存 —— 相邻若干轮
  沿用同一起点，prompt 前缀保持稳定（契约3）。**不插入省略提示**，由调用方自行拼。
- ``head_tail``   保开场 + 保结尾，中段替换为带计数的省略提示。
- ``tail_only``   只保尾部。
- ``llm_summary`` ``head_tail`` 的基础上把中段交给 LLM 压成要点，需 ``compact_lines``。

绝不做尾部截断：导演评估与总结最需要的恰恰是结局。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from backend.utils.llm import chat_safe, estimate_tokens
from backend.utils.logger import get_logger

logger = get_logger("utils.context")

BLOCK_DROP = "block_drop"
HEAD_TAIL = "head_tail"
TAIL_ONLY = "tail_only"
LLM_SUMMARY = "llm_summary"

#: 与 backend/config.py 的策略校验列表保持一致（config 不能反向 import 本模块）
STRATEGIES = (BLOCK_DROP, HEAD_TAIL, TAIL_ONLY, LLM_SUMMARY)

_SUMMARY_PROMPT = """请把下面这段剧情对白压缩成不超过 200 字的要点，只保留：
发生了什么关键事件、谁的态度或处境发生了变化、留下了什么未解决的问题。
不要评价，不要补充原文没有的信息。

{body}
"""


@dataclass(frozen=True)
class ContextBudget:
    """一次裁剪的预算与策略。``max_tokens <= 0`` 表示不限。"""

    max_tokens: int
    strategy: str = HEAD_TAIL
    head_ratio: float = 0.2
    tail_ratio: float = 0.65
    # block_drop 丢弃后回落到的水位线（占预算比例），留出后续若干轮的增长空间
    drop_target_ratio: float = 0.75
    # 交给摘要模型的中段原文上限。越是需要压缩的超长场景，中段越大，
    # 不设上限就会变成"摘要请求自己爆上下文，重试三次后才降级"。
    summary_input_tokens: int = 8000


@dataclass
class FitResult:
    """裁剪结果。``start_index`` 仅 block_drop 有意义，供调用方缓存窗口起点。"""

    text: str
    start_index: int = 0
    dropped: int = 0
    summary: str = ""
    compacted: bool = False


def _ellipsis(n: int) -> str:
    return f"（此处省略 {n} 轮）"


def _truncate_line(line: str, max_tokens: int) -> str:
    """把单行精确压到预算内（二分）。绝不返回空串。

    不用按字符比例估算：estimate_tokens 对 CJK/非 CJK 权重不同，按比例切会系统性偏大，
    而本函数是最终预算校验的收尾手段，必须真的能收敛。
    """
    if max_tokens <= 0 or estimate_tokens(line) <= max_tokens:
        return line
    lo, hi = 0, len(line)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(line[:mid] + "…") <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return line[:lo] + "…"


def _enforce_budget(parts: list[str], budget: ContextBudget) -> tuple[list[str], bool]:
    """返回前的最终预算校验。返回 ``(parts, 是否动过)``。

    行粒度的丢弃无法解决"被强制保留的行自己就超预算"（如必须保留的末行），
    这里按 token 从长到短依次截断，直到真的进预算。
    """
    if budget.max_tokens <= 0 or not parts:
        return parts, False
    costs = [estimate_tokens(p) for p in parts]
    total = sum(costs)
    if total <= budget.max_tokens:
        return parts, False
    out = list(parts)
    for i in sorted(range(len(out)), key=lambda x: costs[x], reverse=True):
        if total <= budget.max_tokens:
            break
        allowance = max(1, budget.max_tokens - (total - costs[i]))
        out[i] = _truncate_line(out[i], allowance)
        new_cost = estimate_tokens(out[i])
        total += new_cost - costs[i]
        costs[i] = new_cost
    return out, True


def _split_head_tail(lines: list[str], budget: ContextBudget) -> tuple[int, int]:
    """按预算算出保留的头部行数与尾部起点，返回 ``(head_end, tail_start)``。

    尾部优先：先从后往前吃满尾部预算，再用剩余预算从前往后吃头部。
    """
    tail_budget = max(1, int(budget.max_tokens * budget.tail_ratio))
    head_budget = max(0, int(budget.max_tokens * budget.head_ratio))

    tail_start = len(lines)
    used = 0
    for i in range(len(lines) - 1, -1, -1):
        cost = estimate_tokens(lines[i])
        if used + cost > tail_budget and tail_start < len(lines):
            break
        used += cost
        tail_start = i

    head_end = 0
    used = 0
    for i in range(tail_start):
        cost = estimate_tokens(lines[i])
        if used + cost > head_budget:
            break
        used += cost
        head_end = i + 1

    return head_end, tail_start


def fit_lines(
    lines: list[str], budget: ContextBudget, *, start_hint: int = 0
) -> FitResult:
    """把 ``lines`` 裁剪进预算。纯本地，不调 LLM（契约6：离线必须能跑）。"""
    if not lines:
        return FitResult(text="")

    strategy = budget.strategy
    if strategy not in STRATEGIES:
        logger.warning("未知的上下文裁剪策略 %r，回退 head_tail", strategy)
        strategy = HEAD_TAIL
    if strategy == LLM_SUMMARY:
        # 本函数不发起 LLM 调用；需要摘要的调用方走 compact_lines
        strategy = HEAD_TAIL

    if strategy == BLOCK_DROP:
        return _fit_block_drop(lines, budget, start_hint)

    total = sum(estimate_tokens(line) for line in lines)
    if budget.max_tokens <= 0 or total <= budget.max_tokens:
        return FitResult(text="\n".join(lines))

    if strategy == TAIL_ONLY:
        _, tail_start = _split_head_tail(lines, replace(budget, head_ratio=0.0))
        kept = lines[tail_start:]
        dropped = len(lines) - len(kept)
        parts = [_ellipsis(dropped), *kept] if dropped > 0 else list(kept)
        parts, forced = _enforce_budget(parts, budget)
        return FitResult(
            text="\n".join(parts), dropped=dropped, compacted=dropped > 0 or forced
        )

    head_end, tail_start = _split_head_tail(lines, budget)
    dropped = tail_start - head_end
    parts = [*lines[:head_end]]
    if dropped > 0:
        parts.append(_ellipsis(dropped))
    parts.extend(lines[tail_start:])
    parts, forced = _enforce_budget(parts, budget)
    return FitResult(
        text="\n".join(parts), dropped=max(dropped, 0), compacted=dropped > 0 or forced
    )


def _fit_block_drop(lines: list[str], budget: ContextBudget, start_hint: int) -> FitResult:
    start = min(max(start_hint, 0), max(len(lines) - 1, 0))
    if budget.max_tokens > 0:
        total = sum(estimate_tokens(line) for line in lines[start:])
        if total > budget.max_tokens:
            target = int(budget.max_tokens * budget.drop_target_ratio)
            while start < len(lines) - 1 and total > target:
                total -= estimate_tokens(lines[start])
                start += 1
    parts, forced = _enforce_budget(lines[start:], budget)
    return FitResult(
        text="\n".join(parts),
        start_index=start,
        dropped=start,
        compacted=start > start_hint or forced,
    )


async def compact_lines(
    lines: list[str],
    budget: ContextBudget,
    *,
    model: str | None = None,
    temperature: float = 0.2,
) -> FitResult:
    """带 LLM 摘要的裁剪。非 ``llm_summary`` 策略等价于 :func:`fit_lines`。

    摘要调用失败时降级为 ``head_tail`` 而不是抛出：契约6 要求离线/CI 能跑通。
    """
    if not lines:
        return FitResult(text="")
    if budget.strategy != LLM_SUMMARY:
        return fit_lines(lines, budget)

    fallback = replace(budget, strategy=HEAD_TAIL)
    total = sum(estimate_tokens(line) for line in lines)
    if budget.max_tokens <= 0 or total <= budget.max_tokens:
        return FitResult(text="\n".join(lines))

    head_end, tail_start = _split_head_tail(lines, budget)
    middle = lines[head_end:tail_start]
    if not middle:
        return fit_lines(lines, fallback)

    # 中段先本地裁剪再送去摘要：不限长地拼进一次请求，越是该压缩的场景
    # 越容易把摘要请求自己撑爆，而 chat_safe 还会重试三次才降级。
    summary_input = fit_lines(
        middle,
        ContextBudget(max_tokens=budget.summary_input_tokens, strategy=HEAD_TAIL),
    )

    try:
        raw = await chat_safe(
            [{"role": "user", "content": _SUMMARY_PROMPT.format(body=summary_input.text)}],
            temperature=temperature,
            model=model,
            max_tokens=512,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("中段摘要失败，降级为 head_tail 裁剪：%s", exc)
        return fit_lines(lines, fallback)

    summary = raw.strip()
    if not summary:
        return fit_lines(lines, fallback)

    dropped = len(middle)
    parts = [
        *lines[:head_end],
        f"【中段摘要（原 {dropped} 轮）】{summary}",
        *lines[tail_start:],
    ]
    # 模型可能不理会字数要求，摘要本身也得受预算约束
    parts, _ = _enforce_budget(parts, budget)
    return FitResult(
        text="\n".join(parts),
        dropped=dropped,
        summary=summary,
        compacted=True,
    )
