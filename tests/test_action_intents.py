"""工单24 PR-1b：动作意图的预过滤与抽取。

《玻璃王冠》缩写：王冠与暗格在场。每条用例对应设计单的一道防护（§5.1、A17–A22），
撤掉那道防护时用例必须变红。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.config import Settings
from backend.exceptions import LLMError
from backend.memory import MemoryManager
from backend.models import (
    ActionIntent,
    CharacterCard,
    EnvironmentMode,
    Scene,
    SceneConfig,
    WorldObject,
)
from backend.scene_engine import SceneEngine
from backend.scene_engine.action_intents import (
    MAX_ACTION_EXTRACTS_PER_TURN,
    ActionIntentExtractor,
    match_objects,
    strip_thoughts,
)
from backend.services import orchestrator, repository
from backend.snapshot import SnapshotManager

_LLM = "backend.scene_engine.action_intents.chat_safe"
_HIDDEN = "戴上后投射佩戴者最强烈的记忆"

CROWN = WorldObject(
    object_id="o-crown", project_id="p", name="玻璃王冠", aliases=["王冠"],
    public_description="一顶通体透明的玻璃王冠。", hidden_rules=[_HIDDEN],
)
NICHE = WorldObject(
    object_id="o-niche", project_id="p", name="暗格", aliases=["祭坛暗格"],
    public_description="祭坛下的地砖。", hidden_rules=["按顺序踩三块地砖才开"],
)


def _reply(*items: dict) -> str:
    return json.dumps(list(items), ensure_ascii=False)


class _Llm:
    """记下每次收到的 prompt；按调用顺序返回或抛错。"""

    def __init__(self, *results):
        self.results = list(results)
        self.prompts: list[str] = []

    async def __call__(self, messages, **_kw):
        self.prompts.append(messages[0]["content"])
        result = self.results.pop(0) if self.results else _reply()
        if isinstance(result, Exception):
            raise result
        return result


async def _extract(segments, llm, objects=(CROWN, NICHE)):
    with patch(_LLM, new=llm):
        return await ActionIntentExtractor(list(objects)).extract(segments, "伊莎贝尔")


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_strip_thoughts_removes_closed_fullwidth_and_unclosed_brackets():
    assert strip_thoughts("走向王冠[她想戴上]") == "走向王冠"
    assert strip_thoughts("走向王冠［她想戴上］，伸手") == "走向王冠，伸手"
    # 格式写坏了没闭合：宁可多剥一截动作，不放过一句独白
    assert strip_thoughts("拿起王冠[她其实非亲生") == "拿起王冠"
    assert strip_thoughts("缓缓\n起身") == "缓缓 起身"


def test_match_objects_uses_name_and_alias_and_ignores_latin_case():
    lamp = WorldObject(object_id="o-lamp", name="Lamp", aliases=["油灯"])
    assert match_objects("把王冠举过头顶", [CROWN, NICHE]) == [CROWN]
    assert match_objects("蹲下摸了摸祭坛暗格", [CROWN, NICHE]) == [NICHE]
    assert match_objects("点亮 LAMP", [lamp]) == [lamp]
    assert match_objects("把它戴上", [CROWN]) == [], "不做指代消解（设计单 §3）"


# ---------------------------------------------------------------------------
# 抽取器
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pure_expressions_never_call_the_llm():
    llm = _Llm()
    intents = await _extract(["皱眉", "叹了口气"], llm)
    assert llm.prompts == []
    assert [(i.status, i.skip_reason) for i in intents] == [("skipped", "no_object")] * 2


@pytest.mark.asyncio
async def test_one_call_per_turn_and_results_filled_back_by_index():
    """两个命中段只发一次；模型倒序返回也按 index 回填（A18）。"""
    llm = _Llm(_reply(
        {"index": 2, "object": "o2", "is_attempt": True, "verb": "踩", "detail": "踩祭坛暗格"},
        {"index": 0, "object": "o1", "is_attempt": True, "verb": "戴上", "detail": "戴上王冠"},
    ))
    intents = await _extract(["拿起王冠戴上", "皱眉", "踩向暗格"], llm)
    assert len(llm.prompts) == 1
    assert (intents[0].status, intents[0].object_id, intents[0].verb) == ("recorded", "o-crown", "戴上")
    assert (intents[1].status, intents[1].skip_reason) == ("skipped", "no_object")
    assert (intents[2].status, intents[2].object_id) == ("recorded", "o-niche")


@pytest.mark.asyncio
async def test_prompt_has_no_hidden_rules_no_dialogue_and_no_inline_thought():
    """R1 + A19：只传命中段与公开描述。段内的独白先剥掉。"""
    llm = _Llm(_reply({"index": 0, "object": "o1", "is_attempt": True}))
    intents = await _extract(["走向王冠[她其实非亲生]"], llm)
    prompt = llm.prompts[0]
    assert _HIDDEN not in prompt and "按顺序踩三块地砖" not in prompt
    assert "非亲生" not in prompt
    assert "非亲生" not in intents[0].text
    assert "一顶通体透明的玻璃王冠" in prompt
    # 只送提到的物件：暗格没被提到，不该进候选
    assert "暗格" not in prompt


@pytest.mark.asyncio
async def test_over_limit_truncation_is_by_segment_order():
    """A18：取前 6 段，其余 over_limit；不靠模型、不靠 dict 顺序。"""
    segments = [f"第{i}次碰王冠" for i in range(MAX_ACTION_EXTRACTS_PER_TURN + 2)]
    llm = _Llm(_reply(*[
        {"index": i, "object": "o1", "is_attempt": True} for i in range(len(segments))
    ]))
    intents = await _extract(segments, llm)
    sent = [i for i in intents if i.skip_reason != "over_limit"]
    assert [i.index for i in sent] == list(range(MAX_ACTION_EXTRACTS_PER_TURN))
    assert [i.skip_reason for i in intents[MAX_ACTION_EXTRACTS_PER_TURN:]] == ["over_limit"] * 2
    # 被截掉的段落没进 prompt，模型硬给的结果也不认
    assert f"{MAX_ACTION_EXTRACTS_PER_TURN}. " not in llm.prompts[0]
    assert all(i.status == "skipped" for i in intents[MAX_ACTION_EXTRACTS_PER_TURN:])


@pytest.mark.asyncio
async def test_llm_failure_marks_hits_failed_without_inventing_results():
    intents = await _extract(["拿起王冠", "皱眉"], _Llm(LLMError("超时")))
    assert [(i.status, i.skip_reason) for i in intents] == [
        ("skipped", "extract_failed"), ("skipped", "no_object"),
    ]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("我觉得是在戴王冠", "extract_failed"),
        (_reply({"index": 0, "object": "o9", "is_attempt": True}), "invalid_object"),
        (_reply({"index": 0, "object": "o1", "is_attempt": "false"}), "not_attempt"),
        (_reply({"index": 0, "object": "o1", "is_attempt": "maybe"}), "not_attempt"),
        (_reply({"index": 7, "object": "o1", "is_attempt": True}), "invalid_object"),
        (_reply({"index": True, "object": "o1", "is_attempt": True}), "invalid_object"),
        (_reply(), "invalid_object"),
    ],
)
@pytest.mark.asyncio
async def test_bad_model_output_never_becomes_a_recorded_attempt(reply, reason):
    intents = await _extract(["拿起王冠"], _Llm(reply))
    assert (intents[0].status, intents[0].skip_reason) == ("skipped", reason)


@pytest.mark.asyncio
async def test_duplicate_index_keeps_the_first_and_fields_are_clamped():
    llm = _Llm("```json\n" + _reply(
        {"index": 0, "object": "o1", "is_attempt": True, "verb": "戴上戴上戴上戴上戴上", "detail": "长\n" * 100},
        {"index": 0, "object": "o1", "is_attempt": False},
    ) + "\n```")
    intent = (await _extract(["戴上王冠"], llm))[0]
    assert intent.status == "recorded"
    assert len(intent.verb) <= 8
    assert len(intent.detail) <= 80 and "\n" not in intent.detail


# ---------------------------------------------------------------------------
# 引擎集成
# ---------------------------------------------------------------------------


def _agent(cid: str, name: str) -> CharacterAgent:
    card = CharacterCard(character_id=cid, project_id="p-act", name=name)
    return CharacterAgent(card, MemoryManager(cid, "p-act"))


def _engine(mode: str, scene_id: str, max_turns: int = 2, agents=None):
    agents = agents or [_agent("c1", "伊莎贝尔")]
    scene = Scene(scene_id=scene_id, project_id="p-act", branch_id="b", objects_present=["o-crown"])
    config = SceneConfig(name="加冕前夜", participating_characters=[a.character_id for a in agents],
                         max_turns=max_turns)
    engine = SceneEngine(scene, config, agents, SnapshotManager("p-act"),
                         objects=[CROWN], environment_mode=mode)
    return engine, agents


_LINES = ["*拿起王冠[她其实非亲生]* 这是母亲的。", "*皱眉* 不对。"]


@pytest.mark.asyncio
async def test_off_mode_has_no_actions_and_no_extraction_calls():
    engine, _ = _engine(EnvironmentMode.OFF.value, "s-act-off")
    llm = _Llm()
    with patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=_LINES)), patch(_LLM, new=llm):
        result = await engine.run()
    assert llm.prompts == []
    assert all(t.actions == [] for t in result.dialogue_log)


@pytest.mark.asyncio
async def test_actions_are_complete_before_the_turn_is_first_persisted():
    """A9：on_turn 拿到的那一轮意图已完整 —— 落盘先于推送，落盘的轮次就不再缺意图。"""
    engine, _ = _engine(EnvironmentMode.RECORD.value, "s-act-order")
    seen: list[list[ActionIntent]] = []

    async def on_turn(turn):
        seen.append(list(turn.actions))

    llm = _Llm(_reply({"index": 0, "object": "o1", "is_attempt": True, "verb": "拿起"}))
    with patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=_LINES)), patch(_LLM, new=llm):
        await engine.run(on_turn=on_turn)
    assert seen[0][0].status == "recorded" and seen[0][0].object_id == "o-crown"
    assert seen[1][0].skip_reason == "no_object"
    assert len(llm.prompts) == 1, "第二轮是神态，不该调用"


@pytest.mark.asyncio
async def test_record_mode_leaves_memory_and_transcript_byte_identical():
    """A20：actions 不进任何角色记忆或 transcript。逐字比对 off 与 record 两档。"""

    async def run(mode: str, scene_id: str):
        engine, agents = _engine(mode, scene_id, agents=[_agent("c1", "伊莎贝尔"), _agent("c2", "诺安")])
        written: list[str] = []
        real = MemoryManager.add_experience

        async def spy(self, turn):
            await real(self, turn)
            # 比的是真正写进去的东西（短期缓冲 + 事件摘要），不是某个内部渲染函数：
            # 换个函数名或加一条写入路径，比渲染函数的断言就失效了
            written.append(f"{self.character_id}|{self.short_term.dump()[-1]}|{self.episodic.dump()}")

        llm = _Llm(_reply({"index": 0, "object": "o1", "is_attempt": True,
                           "verb": "拿起", "detail": "细节不得进记忆"}))
        with (
            patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=_LINES)),
            patch(_LLM, new=llm),
            patch.object(MemoryManager, "add_experience", spy),
        ):
            result = await engine.run()
        transcript = [engine._turn_line(t) for t in result.dialogue_log]
        return written, transcript, result

    off_mem, off_tx, _ = await run(EnvironmentMode.OFF.value, "s-act-mem-off")
    rec_mem, rec_tx, rec = await run(EnvironmentMode.RECORD.value, "s-act-mem-rec")
    assert rec.dialogue_log[0].actions[0].detail == "细节不得进记忆", "前提：record 档确实抽到了"
    assert rec_mem == off_mem
    assert rec_tx == off_tx
    assert all("细节不得进记忆" not in line for line in rec_mem + rec_tx)


@pytest.mark.asyncio
async def test_resume_does_not_re_extract_persisted_turns():
    """续跑时已落盘轮次的 actions 原样保留，调用次数只算新轮次。"""
    engine, _ = _engine(EnvironmentMode.RECORD.value, "s-act-resume", max_turns=1)
    llm = _Llm(_reply({"index": 0, "object": "o1", "is_attempt": True, "verb": "拿起"}))
    with patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=_LINES[:1])), patch(_LLM, new=llm):
        first = await engine.run()
    original = first.dialogue_log[0].actions

    scene = engine.scene
    scene.max_turns = 2
    config = SceneConfig(name="加冕前夜", participating_characters=["c1"], max_turns=2)
    resumed = SceneEngine(scene, config, [_agent("c1", "伊莎贝尔")], SnapshotManager("p-act"),
                          objects=[CROWN], environment_mode=EnvironmentMode.RECORD.value)
    resumed.inject_history(scene.dialogue_log)
    llm2 = _Llm()
    with patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=_LINES[1:])), patch(_LLM, new=llm2):
        result = await resumed.run()
    assert result.dialogue_log[0].actions == original
    assert llm2.prompts == [], "第二轮是神态；旧轮次不重抽"


def test_engine_caps_objects_present():
    many = [WorldObject(object_id=f"o{i}", name=f"物件{i}") for i in range(30)]
    scene = Scene(scene_id="s-cap", project_id="p-act", branch_id="b")
    engine = SceneEngine(scene, SceneConfig(name="x"), [_agent("c1", "甲")], SnapshotManager("p-act"),
                         objects=many, environment_mode=EnvironmentMode.RECORD.value)
    assert len(engine._action_extractor.objects) == 10


# ---------------------------------------------------------------------------
# 持久化与配置
# ---------------------------------------------------------------------------


def _scene_with_actions(scene_id: str, actions: list) -> dict:
    return {
        "scene_id": scene_id, "project_id": "p-act", "branch_id": "b",
        "dialogue_log": [{"turn_id": "t1", "character_id": "c1", "action": "拿起王冠", "actions": actions}],
    }


def test_deserialize_tightens_unknown_status_and_skips_bad_entries():
    scene = repository._deserialize_scene(_scene_with_actions("s-de", [
        {"index": 0, "text": "拿起王冠", "object_id": "o-crown", "status": "recorded",
         "skip_reason": "no_object"},
        # pending 会被补裁决；手改出来的、没指向物件的必须落到什么都不做（PR-2a 起 pending
        # 是合法取值，没有 object_id 才收紧，原因记 invalid_object）
        {"index": 1, "text": "x", "status": "pending"},
        {"index": 2, "text": "x", "status": "乱写", "skip_reason": "no_object"},
        {"index": 3, "status": "skipped", "skip_reason": "瞎编"},
        {"index": True, "status": "recorded"},
        "不是对象",
    ]))
    actions = scene.dialogue_log[0].actions
    assert [(a.index, a.status, a.skip_reason) for a in actions] == [
        (0, "recorded", ""), (1, "skipped", "invalid_object"), (2, "skipped", ""), (3, "skipped", ""),
    ]


@pytest.mark.parametrize("raw", [None, "x", {"index": 0}, 3])
def test_deserialize_non_list_actions_is_empty(raw):
    scene = repository._deserialize_scene(_scene_with_actions("s-de2", raw))
    assert scene.dialogue_log[0].actions == []


@pytest.mark.asyncio
async def test_actions_round_trip_through_the_database():
    scene = Scene(scene_id="s-act-db", project_id="p-act", branch_id="b")
    from backend.models import DialogueTurn

    scene.dialogue_log = [DialogueTurn(turn_id="t1", character_id="c1", action="拿起王冠", actions=[
        ActionIntent(index=0, text="拿起王冠", object_id="o-crown", verb="拿起", status="recorded"),
    ])]
    await repository.save_scene(scene)
    loaded = await repository.get_scene("s-act-db")
    assert loaded.dialogue_log[0].actions == scene.dialogue_log[0].actions


@pytest.mark.parametrize("value", ["adjudicate", "ON", "recording", ""])
def test_unsupported_environment_mode_fails_at_startup(value):
    with pytest.raises(ValueError):
        Settings(ENVIRONMENT_MODE=value)


@pytest.mark.asyncio
async def test_dangling_present_object_warns(caplog):
    """A22：删了物件之后静默跳过，record 档命中率就无声归零。"""
    pid = "p-act-dangling"
    await repository.save_object(WorldObject(object_id="o-crown", project_id=pid, name="玻璃王冠"))
    scene = Scene(scene_id="s-dangling", project_id=pid, objects_present=["o-crown", "o-gone"])
    caplog.set_level("WARNING")
    objects = await orchestrator._present_objects(scene)
    assert [o.object_id for o in objects] == ["o-crown"]
    assert "o-gone" in caplog.text


# ---------------------------------------------------------------------------
# scripts/action_stats
# ---------------------------------------------------------------------------


def test_action_stats_counts_calls_per_turn_and_skips_off_mode_turns():
    from backend.models import DialogueTurn
    from scripts.action_stats import scene_stats

    def turn(*intents):
        return DialogueTurn(character_id="c1", actions=list(intents))

    def ai(index, status="skipped", reason="no_object", oid=""):
        return ActionIntent(index=index, status=status, skip_reason=reason, object_id=oid)

    scene = Scene(scene_id="s-stats", dialogue_log=[
        turn(),  # off 档跑出来的：不计入任何分母
        turn(ai(0), ai(1)),  # 纯神态：零调用
        turn(ai(0, "recorded", "", "o-crown"), ai(1, reason="not_attempt")),  # 两段命中一次调用
        turn(ai(0, reason="extract_failed")),  # 失败也发过调用
    ])
    stats = scene_stats(scene)
    assert (stats.recorded_turns, stats.segments, stats.hits, stats.calls, stats.recorded) == (3, 5, 3, 2, 1)
    assert stats.by_object["o-crown"] == 1
    assert stats.reasons["no_object"] == 2 and stats.reasons["extract_failed"] == 1
