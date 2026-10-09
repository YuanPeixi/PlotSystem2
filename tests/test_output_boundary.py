"""工单30 PR-30a：角色回复的输出边界。

三件事：续写截断（解析之前，截下的内容不进任何字段）、角色调用的输出上限、
服务商的空正文不再当成功。两段续写样本取自 2026-10-09 烟测的真实落库内容
（落库时空白已被塌成空格，所以另起一行的形式另有用例）。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
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
from backend.scene_engine.continuation import PROMPT_SECTION_TITLES, trim_continuation
from backend.snapshot import SnapshotManager
from backend.utils import llm
from backend.utils.usage import usage_scope

ROOT = Path(__file__).resolve().parent.parent

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
        # 本人标签之后才出现他人台词：截在他人那里，本人标签照常剥掉
        ("好。\n塞芙拉：再说一句。\n诺安：是。", "塞芙拉", "好。\n再说一句。"),
        # 伪发言人
        ("夜深了。 旁白：烛火摇曳。", "诺安", "夜深了。"),
        # 动作收尾后直接接他人标签
        ("*转身*赫尔：站住。", "诺安", "*转身*"),
        # 自造的伪发言人变体、prompt 区块标题、角色名标签
        ("说完了。【导演视角】（可公开给用户）", "诺安", "说完了。"),
        ("好。【当前环境】- 王冠·状态：亮起", "诺安", "好。"),
        ("就这样。【 赫尔 】站住！", "诺安", "就这样。"),
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
        # 游戏 / 奇幻种子常用【】标技能、物品、称号（PR #34 评审）：句中、动作后、开头都不截
        "他学会了【火球术】。",
        "*举起法杖*【火球术】！",
        "【火球术】！我不会让你过去。",
    ],
)
def test_normal_replies_are_untouched(raw):
    assert trim_continuation(raw, self_name="塞芙拉", other_names=_others("塞芙拉")) == (raw, "")


def test_prompt_section_titles_match_the_character_prompt():
    """角色 prompt 新增区块标题时，续写标记的名单必须跟上：模型照抄的正是这些标题。"""
    source = (ROOT / "backend" / "agents" / "character_agent.py").read_text(encoding="utf-8")
    titles = {t for t in re.findall(r"【([^】\n]+)】", source) if "{" not in t}
    assert titles, "没扫到任何区块标题，扫描本身失效了"
    assert titles <= set(PROMPT_SECTION_TITLES), titles - set(PROMPT_SECTION_TITLES)


def test_own_name_prefix_is_stripped_not_cut():
    raw = "塞芙拉: 诺安大人，您怀中的账本……似乎比往日更厚了些。"
    assert trim_continuation(raw, self_name="塞芙拉", other_names=_others("塞芙拉")) == (
        "诺安大人，您怀中的账本……似乎比往日更厚了些。",
        "",
    )


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        # 30b 回放里最常见的写法：动作一行，换行后带着本人名字说台词。仍是本人这一轮
        ("*指尖滑过底座的接缝*\n阿德里安: 底座完好，骨粉胶无异常。",
         "*指尖滑过底座的接缝*\n底座完好，骨粉胶无异常。"),
        ("*收回银针*\n阿德里安：王后殿下说得是。\n\n*后退一步*\n阿德里安：玻璃王冠不会说谎。",
         "*收回银针*\n王后殿下说得是。\n\n*后退一步*\n玻璃王冠不会说谎。"),
        ("好。 阿德里安：再说一句。", "好。 再说一句。"),
    ],
)
def test_own_name_label_midway_is_stripped_not_cut(raw, kept):
    """本人名字标签不是续写：截掉它会把本人这一轮的台词截掉，只剩动作（30b 发现的误判）。"""
    assert trim_continuation(raw, self_name="阿德里安", other_names=_others("阿德里安")) == (kept, "")


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
    # 导演看得到发生过截断：标签 + 字数 + 开头一小段
    assert turn.output_notice.startswith("已截断：")
    assert "【环境】" in turn.output_notice


def test_output_notice_keeps_only_a_short_head():
    """伪造内容不整段落盘：只留开头一小段与字数。"""
    from backend.scene_engine.engine import _NOTICE_HEAD_CHARS, _output_notice

    cut = "【导演视角】（可公开给用户，但不可直接注入角色提示词） " * 30
    notice = _output_notice(cut, retried=False)
    assert f"已截掉 {len(cut)} 字" in notice
    head = notice.split("「", 1)[1].rstrip("」")
    assert len(head) <= _NOTICE_HEAD_CHARS + len("……")
    assert _output_notice("", retried=False) == ""


@pytest.mark.asyncio
async def test_output_notice_survives_the_repository_round_trip():
    from backend.models import DialogueTurn
    from backend.services.repository import _deserialize_turn
    from backend.utils.serializer import to_dict

    turn = DialogueTurn(turn_id="t1", dialogue="好。", output_notice="已截断：已截掉 12 字")
    assert _deserialize_turn(to_dict(turn)).output_notice == "已截断：已截掉 12 字"
    # 旧数据没有这个键
    assert _deserialize_turn({"turn_id": "t0"}).output_notice == ""


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
    assert result.dialogue_log[0].output_notice.startswith("已重新生成：")


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
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_client", lambda base_url=None, api_key=None: client)
    monkeypatch.setattr(llm._complete.retry, "wait", wait_none())
    return SimpleNamespace(script=script, requests=requests)


async def _character_call() -> str:
    return await llm.chat_safe([{"role": "user", "content": "你好"}], purpose=LLMPurpose.CHARACTER)


@pytest.mark.asyncio
async def test_empty_content_is_retried(scripted_llm):
    # 只给一次空的测不到"重试了几次"：前两次空（None 与纯空白各一），第三次才有正文
    scripted_llm.script.extend([_resp(None), _resp("  \n"), _resp("好")])
    with usage_scope() as meter:
        assert await _character_call() == "好"
    stat = meter.snapshot()["character"]
    assert (stat.calls, stat.retries, stat.failures) == (1, 2, 0)
    # 两次空正文也计费：三次响应的 token 都要进报表（PR #34 评审）
    assert (stat.prompt_tokens, stat.completion_tokens) == (30, 15)


@pytest.mark.asyncio
async def test_empty_content_every_time_is_a_failure(scripted_llm):
    scripted_llm.script.extend([_resp(""), _resp(""), _resp("")])
    with usage_scope() as meter, pytest.raises(LLMError, match="空正文"):
        await _character_call()
    stat = meter.snapshot()["character"]
    assert (stat.calls, stat.retries, stat.failures) == (0, 2, 1)
    assert (stat.prompt_tokens, stat.completion_tokens) == (30, 15)


@pytest.mark.asyncio
async def test_empty_content_from_exhausted_budget_is_not_retried(scripted_llm):
    """推理吃光 max_tokens 的空正文，同一个 prompt 再发结果一样：立即失败，调用方走兜底。"""
    scripted_llm.script.extend([_resp(None, "length"), _resp("好")])
    with usage_scope() as meter, pytest.raises(LLMError, match="finish_reason=length"):
        await llm.chat_safe(
            [{"role": "user", "content": "打分"}], max_tokens=200, purpose=LLMPurpose.SELECTOR
        )
    assert len(scripted_llm.requests) == 1
    stat = meter.snapshot()["selector"]
    assert (stat.calls, stat.retries, stat.failures) == (0, 0, 1)
    assert (stat.prompt_tokens, stat.completion_tokens) == (10, 5)


@pytest.mark.asyncio
async def test_cancellation_is_not_retried(scripted_llm):
    """取消不是调用失败：原样抛出，不再发一次付费请求。"""
    scripted_llm.script.extend([asyncio.CancelledError(), _resp("好")])
    with pytest.raises(asyncio.CancelledError):
        await _character_call()
    assert len(scripted_llm.requests) == 1


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
