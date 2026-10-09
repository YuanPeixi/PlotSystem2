"""工单30 PR-30a：角色回复的输出边界。

三件事：续写截断（解析之前，截下的内容不进任何字段）、角色调用的输出上限、
服务商的空正文不再当成功。两段续写样本取自 2026-10-09 烟测的真实落库内容
（落库时空白已被塌成空格，所以另起一行的形式另有用例）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from tenacity import wait_none

from backend.agents.character_agent import CharacterAgent
from backend.config import Settings, settings
from backend.exceptions import LLMError
from backend.memory import MemoryManager
from backend.models import CharacterCard, LLMPurpose, Scene, SceneConfig
from backend.scene_engine import SceneEngine
from backend.scene_engine.continuation import trim_continuation
from backend.snapshot import SnapshotManager
from backend.utils import llm
from backend.utils.usage import usage_scope

# 149ff4bd 第 5 轮（阿德里安）：替环境写出裁决结果，再复读一个自造标签直到输出上限
_FORGED_ENVIRONMENT = (
    "王后殿下，您说玻璃易碎——可碎了的玻璃，每一片都还映着光。 这顶王冠，底座上确实有一道极细的胶痕，"
    "像是补过。 镜子不会说谎。王冠亦然。 【用户】 【环境】王冠底座在阿德里安指尖下依旧冰凉……"
    "底座内侧，确实藏着一片极薄的夹层。 "
    + "【导演视角】（可公开给用户，但不可直接注入角色提示词） " * 30
)
# 1198aeb6 第 11 轮（塞芙拉）：先写环境，再替阿德里安、替自己续写台词
_FORGED_LINES = (
    "诺安大人既如此执着于那处胶痕，不如让本宫替你看一眼。 只是阿德里安师傅，封箱吧。 "
    "【环境】门外的脚步声愈发清晰，赫尔的身影出现在门口。 "
    "阿德里安: 王后殿下说得对，时辰不早了。 "
    "塞芙拉: 阿德里安师傅倒是心细如发。 【旁白】塞芙拉的话音刚落……"
)
_CAST = ["阿德里安", "诺安", "塞芙拉", "赫尔"]


def _others(name: str) -> list[str]:
    return [n for n in _CAST if n != name]


# ---------------------------------------------------------------------------
# trim_continuation：纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "speaker", "kept"),
    [
        (_FORGED_ENVIRONMENT, "阿德里安", "王后殿下，您说玻璃易碎——可碎了的玻璃，每一片都还映着光。 "
         "这顶王冠，底座上确实有一道极细的胶痕，像是补过。 镜子不会说谎。王冠亦然。"),
        (_FORGED_LINES, "塞芙拉", "诺安大人既如此执着于那处胶痕，不如让本宫替你看一眼。 只是阿德里安师傅，封箱吧。"),
        # 另起一行的他人台词（原始回复的样子）
        ("*点头* 好，就这么办。\n诺安：臣这就去取账册。", "塞芙拉", "*点头* 好，就这么办。"),
        ("*点头* 好。\n\n【环境】王冠亮了。", "塞芙拉", "*点头* 好。"),
        # 本人在后文又起一行，同样是续写
        ("好。\n塞芙拉：再说一句。", "塞芙拉", "好。"),
        # 伪发言人
        ("夜深了。 旁白：烛火摇曳。", "诺安", "夜深了。"),
        # 动作收尾后直接接他人标签
        ("*转身*赫尔：站住。", "诺安", "*转身*"),
    ],
)
def test_continuation_is_cut(raw, speaker, kept):
    got, cut = trim_continuation(raw, self_name=speaker, other_names=_others(speaker))
    assert got == kept
    assert cut and raw.endswith(cut)


@pytest.mark.parametrize(
    "raw",
    [
        # 名字后不跟冒号：正常台词里提到别人
        "诺安大人说得对，诺安，你先说。",
        # 冒号前面直接是正文字词，不是另起一行的发言人标签
        "我只问一句，诺安：你昨夜在哪？",
        # 独白用方括号，与【】无关
        "*抬眼* [他在说谎] 是吗？［我不信］",
        "账册上写着：十二罐未开封。",
    ],
)
def test_normal_replies_are_untouched(raw):
    assert trim_continuation(raw, self_name="塞芙拉", other_names=_others("塞芙拉")) == (raw, "")


def test_own_name_prefix_is_stripped_not_cut():
    raw = "塞芙拉: 诺安大人，您怀中的账本……似乎比往日更厚了些。"
    assert trim_continuation(raw, self_name="塞芙拉", other_names=_others("塞芙拉")) == (
        "诺安大人，您怀中的账本……似乎比往日更厚了些。",
        "",
    )


def test_reply_that_is_all_continuation_keeps_nothing():
    kept, cut = trim_continuation("【环境】王冠亮了。", self_name="诺安", other_names=_others("诺安"))
    assert kept == "" and cut == "【环境】王冠亮了。"


# ---------------------------------------------------------------------------
# 引擎：截断早于解析，截下的内容不进任何字段 / transcript / 记忆
# ---------------------------------------------------------------------------


def _agent(cid: str, name: str) -> CharacterAgent:
    card = CharacterCard(character_id=cid, project_id="proj-30a", name=name, persona="测试角色")
    return CharacterAgent(card, MemoryManager(cid, "proj-30a"))


def _engine(scene_id: str, agents: list[CharacterAgent], max_turns: int = 1) -> SceneEngine:
    scene = Scene(scene_id=scene_id, project_id="proj-30a", branch_id="b-30a")
    config = SceneConfig(
        name="验冠", participating_characters=[a.character_id for a in agents], max_turns=max_turns
    )
    return SceneEngine(scene, config, agents, SnapshotManager("proj-30a"))


@pytest.mark.asyncio
async def test_forged_environment_never_reaches_turn_transcript_or_memory():
    adrian, noah = _agent("c1", "阿德里安"), _agent("c2", "诺安")
    engine = _engine("s-30a-forged", [adrian, noah])
    # 续写里带一个动作：截断若晚于解析，它会进 action 与意图抽取
    raw = "*俯身细看* 镜子不会说谎。 【环境】*底座弹开* 确实藏着夹层。 诺安: 果然如此。"
    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(return_value=raw)),
        patch("backend.memory.memory_manager.MemoryManager.consolidate", new=AsyncMock()),
    ):
        result = await engine.run()

    turn = result.dialogue_log[0]
    assert (turn.action, turn.dialogue) == ("俯身细看", "镜子不会说谎。")
    for text in [engine._turn_line(turn), *noah.memory.short_term.dump(), *adrian.memory.short_term.dump()]:
        assert "夹层" not in text and "底座弹开" not in text and "果然如此" not in text


@pytest.mark.asyncio
async def test_reply_that_is_all_continuation_is_asked_again():
    adrian = _agent("c1", "阿德里安")
    engine = _engine("s-30a-retry", [adrian, _agent("c2", "诺安")])
    respond = AsyncMock(side_effect=["【环境】王冠亮了。", "*伸手* 让我看看。"])
    with (
        patch.object(CharacterAgent, "respond", new=respond),
        patch("backend.memory.memory_manager.MemoryManager.consolidate", new=AsyncMock()),
    ):
        result = await engine.run()
    assert respond.await_count == 2
    assert len(result.dialogue_log) == 1
    assert result.dialogue_log[0].dialogue == "让我看看。"


@pytest.mark.asyncio
@pytest.mark.parametrize("replies", [["【环境】王冠亮了。", "诺安: 是吗？"], ["[]", "  "]])
async def test_no_empty_turn_is_ever_recorded(replies):
    """截完什么都不剩、连要两次都这样：按调用失败抛出，不落一个三项全空的轮次（R5）。"""
    engine = _engine("s-30a-empty", [_agent("c1", "阿德里安"), _agent("c2", "诺安")])
    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=replies)),
        patch("backend.memory.memory_manager.MemoryManager.consolidate", new=AsyncMock()),
        pytest.raises(LLMError),
    ):
        await engine.run()
    assert engine.scene.dialogue_log == []


# ---------------------------------------------------------------------------
# llm.py：空正文是可重试的失败
# ---------------------------------------------------------------------------


def _resp(text: str | None, finish: str | None = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


@pytest.fixture
def scripted_llm(monkeypatch):
    script: list = []
    requests: list[dict] = []

    async def create(**kwargs):
        requests.append(kwargs)
        await asyncio.sleep(0)
        return script.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_client", lambda base_url=None, api_key=None: client)
    monkeypatch.setattr(llm._complete.retry, "wait", wait_none())
    return SimpleNamespace(script=script, requests=requests)


async def _character_call() -> str:
    return await llm.chat_safe([{"role": "user", "content": "你好"}], purpose=LLMPurpose.CHARACTER)


@pytest.mark.asyncio
async def test_empty_content_is_retried(scripted_llm):
    # 只给一次空的测不到"重试了几次"：前两次空（None 与纯空白各一），第三次才有正文
    scripted_llm.script.extend([_resp(None, "length"), _resp("  \n"), _resp("好")])
    with usage_scope() as meter:
        assert await _character_call() == "好"
    stat = meter.snapshot()["character"]
    assert (stat.calls, stat.retries, stat.failures) == (1, 2, 0)


@pytest.mark.asyncio
async def test_empty_content_every_time_is_a_failure(scripted_llm):
    scripted_llm.script.extend([_resp(""), _resp(""), _resp("")])
    with usage_scope() as meter, pytest.raises(LLMError, match="空正文"):
        await _character_call()
    stat = meter.snapshot()["character"]
    assert (stat.calls, stat.retries, stat.failures) == (0, 2, 1)


# ---------------------------------------------------------------------------
# 输出上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("configured", "sent"), [(2000, 2000), (0, None)])
async def test_character_reply_carries_the_output_cap(scripted_llm, monkeypatch, configured, sent):
    monkeypatch.setattr(settings, "CHARACTER_MAX_TOKENS", configured)
    scripted_llm.script.append(_resp("*点头* 好。"))
    await _agent("c1", "阿德里安").respond({"name": "验冠"}, [])
    assert scripted_llm.requests[0]["max_tokens"] == sent


def test_negative_output_cap_is_rejected():
    with pytest.raises(ValidationError, match="CHARACTER_MAX_TOKENS"):
        Settings(CHARACTER_MAX_TOKENS=-1)
