"""统一上下文压缩管线的单测（工单27）。"""

from __future__ import annotations

import pytest

from backend.utils import context as ctx
from backend.utils.llm import estimate_tokens


def _lines(n: int, filler: str = "台词内容内容内容") -> list[str]:
    return [f"角色{i % 3}: {filler}{i}" for i in range(n)]


def test_empty_input_returns_empty_text():
    result = ctx.fit_lines([], ctx.ContextBudget(max_tokens=100))
    assert result.text == ""
    assert result.dropped == 0


def test_under_budget_is_noop():
    lines = _lines(5)
    result = ctx.fit_lines(lines, ctx.ContextBudget(max_tokens=100000))
    assert result.text == "\n".join(lines)
    assert result.compacted is False


def test_single_oversized_line_is_truncated_not_emptied():
    huge = ["角色A: " + "很长的台词" * 500]
    result = ctx.fit_lines(huge, ctx.ContextBudget(max_tokens=50))
    assert result.text  # 绝不返回空串
    assert estimate_tokens(result.text) <= 60  # 允许估算误差，但必须显著小于原文
    assert result.text != huge[0]


def test_head_tail_keeps_first_and_last_with_count():
    lines = _lines(200)
    result = ctx.fit_lines(
        lines, ctx.ContextBudget(max_tokens=300, strategy=ctx.HEAD_TAIL)
    )
    assert lines[0] in result.text
    assert lines[-1] in result.text  # 绝不截尾：结局必须保留
    assert f"省略 {result.dropped} 轮" in result.text
    assert result.dropped > 0


def test_tail_only_drops_head():
    lines = _lines(200)
    result = ctx.fit_lines(
        lines, ctx.ContextBudget(max_tokens=300, strategy=ctx.TAIL_ONLY)
    )
    assert lines[-1] in result.text
    assert lines[0] not in result.text


def test_block_drop_start_index_is_monotonic():
    """前缀稳定性（契约3）：窗口起点只能前进，不能回退。"""
    budget = ctx.ContextBudget(max_tokens=400, strategy=ctx.BLOCK_DROP)
    start = 0
    seen = []
    for n in range(10, 200, 10):
        result = ctx.fit_lines(_lines(n), budget, start_hint=start)
        assert result.start_index >= start
        start = result.start_index
        seen.append(start)
    assert seen == sorted(seen)
    assert seen[-1] > 0  # 确实触发过丢弃，否则这条测试没测到东西


def test_unknown_strategy_falls_back_to_head_tail():
    lines = _lines(200)
    result = ctx.fit_lines(
        lines, ctx.ContextBudget(max_tokens=300, strategy="不存在的策略")
    )
    assert lines[0] in result.text
    assert lines[-1] in result.text


async def test_compact_lines_uses_summary(monkeypatch):
    async def _fake_chat(messages, **kwargs):
        return "中段要点：两人爆发争执。"

    monkeypatch.setattr(ctx, "chat_safe", _fake_chat)
    lines = _lines(200)
    result = await ctx.compact_lines(
        lines, ctx.ContextBudget(max_tokens=300, strategy=ctx.LLM_SUMMARY)
    )
    assert "中段要点" in result.text
    assert result.summary
    assert lines[-1] in result.text


async def test_compact_lines_degrades_when_llm_fails(monkeypatch):
    """契约6：LLM 不可用时必须降级为纯本地裁剪，而不是把异常抛给调用方。"""

    async def _boom(messages, **kwargs):
        raise RuntimeError("service unavailable")

    monkeypatch.setattr(ctx, "chat_safe", _boom)
    lines = _lines(200)
    result = await ctx.compact_lines(
        lines, ctx.ContextBudget(max_tokens=300, strategy=ctx.LLM_SUMMARY)
    )
    assert result.summary == ""
    assert lines[0] in result.text
    assert lines[-1] in result.text
    assert f"省略 {result.dropped} 轮" in result.text


async def test_compact_lines_noop_under_budget(monkeypatch):
    async def _should_not_be_called(messages, **kwargs):
        raise AssertionError("未超预算时不应发起 LLM 调用")

    monkeypatch.setattr(ctx, "chat_safe", _should_not_be_called)
    lines = _lines(5)
    result = await ctx.compact_lines(
        lines, ctx.ContextBudget(max_tokens=100000, strategy=ctx.LLM_SUMMARY)
    )
    assert result.text == "\n".join(lines)


@pytest.mark.parametrize("strategy", list(ctx.STRATEGIES))
def test_all_strategies_never_return_empty(strategy):
    lines = _lines(50)
    result = ctx.fit_lines(lines, ctx.ContextBudget(max_tokens=20, strategy=strategy))
    assert result.text.strip()


# --- 最终预算校验 ------------------------------------------------------------


@pytest.mark.parametrize("strategy", list(ctx.STRATEGIES))
def test_oversized_forced_line_is_truncated(strategy):
    """被强制保留的行自己就超预算时，不能原样放行——行粒度丢弃解决不了它。"""
    lines = ["前", "长" * 1000]
    result = ctx.fit_lines(lines, ctx.ContextBudget(max_tokens=50, strategy=strategy))
    assert estimate_tokens(result.text) <= 50
    assert result.compacted is True


def test_single_oversized_line_block_drop():
    result = ctx.fit_lines(
        ["长" * 1000], ctx.ContextBudget(max_tokens=50, strategy=ctx.BLOCK_DROP)
    )
    assert estimate_tokens(result.text) <= 50
    assert result.text.strip()


@pytest.mark.parametrize("n,budget", [(1, 5), (2, 30), (50, 120), (300, 400)])
def test_fit_lines_always_within_budget(n, budget):
    lines = [f"角色: {'台' * (i * 7 % 50 + 1)}" for i in range(n)]
    for strategy in ctx.STRATEGIES:
        result = ctx.fit_lines(
            lines, ctx.ContextBudget(max_tokens=budget, strategy=strategy)
        )
        assert estimate_tokens(result.text) <= budget, strategy


async def test_summary_input_is_bounded(monkeypatch):
    """中段原文不能不限长地拼进一次摘要请求，否则越该压缩的场景越容易把它自己撑爆。"""
    seen: list[str] = []

    async def _fake(messages, **kwargs):
        seen.append(messages[0]["content"])
        return "要点。"

    monkeypatch.setattr(ctx, "chat_safe", _fake)
    lines = _lines(5000)
    budget = ctx.ContextBudget(
        max_tokens=500, strategy=ctx.LLM_SUMMARY, summary_input_tokens=300
    )
    result = await ctx.compact_lines(lines, budget)

    assert seen, "应当真的发起了摘要调用"
    assert estimate_tokens(seen[0]) <= 300 + estimate_tokens(ctx._SUMMARY_PROMPT)
    assert estimate_tokens(result.text) <= 500


async def test_oversized_summary_is_clamped(monkeypatch):
    """模型可能不理会字数要求，摘要本身也得受预算约束。"""

    async def _fake(messages, **kwargs):
        return "啰" * 5000

    monkeypatch.setattr(ctx, "chat_safe", _fake)
    result = await ctx.compact_lines(
        _lines(200), ctx.ContextBudget(max_tokens=300, strategy=ctx.LLM_SUMMARY)
    )
    assert estimate_tokens(result.text) <= 300
