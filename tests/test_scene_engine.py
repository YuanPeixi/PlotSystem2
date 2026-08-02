"""SceneEngine 测试：使用 mock LLM，验证快照前置、轮次解析、终止条件。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.exceptions import LLMError
from backend.memory import MemoryManager
from backend.models import (
    CharacterCard,
    DialogueTurn,
    Scene,
    SceneConfig,
    SpeakerMode,
)
from backend.scene_engine import SceneEngine
from backend.scene_engine.speaker_selector import ScoringSpeakerSelector, detect_addressed
from backend.scene_engine.termination import check_termination
from backend.snapshot import SnapshotManager


def _make_agent(cid: str, name: str) -> CharacterAgent:
    card = CharacterCard(character_id=cid, project_id="proj-se", name=name, persona="测试角色")
    mem = MemoryManager(cid, "proj-se")
    return CharacterAgent(card, mem)


def test_termination_max_turns():
    turns = [DialogueTurn(turn_number=i, dialogue=f"line{i}") for i in range(5)]
    stop, reason = check_termination(turns, max_turns=5)
    assert stop and reason == "达到最大轮次"


def test_termination_interrupt():
    stop, reason = check_termination([], max_turns=10, director_interrupt=True)
    assert stop and reason == "导演中断"


def test_parse_turn_separates_formats():
    agent = _make_agent("c1", "甲")
    scene = Scene(scene_id="s1", project_id="proj-se", branch_id="b1")
    config = SceneConfig(name="测试", participating_characters=["c1"])
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))

    raw = "*缓缓起身* 你终于来了。[他在隐藏什么]"
    turn = engine._parse_turn(raw, agent, 1)
    assert turn.action == "缓缓起身"
    assert "你终于来了" in (turn.dialogue or "")
    assert turn.inner_thought == "他在隐藏什么"


@pytest.mark.asyncio
async def test_scene_run_creates_snapshots_and_log():
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    scene = Scene(scene_id="s-run", project_id="proj-se", branch_id="b-run")
    config = SceneConfig(
        name="对峙",
        description="两人对峙",
        participating_characters=["c1", "c2"],
        location="客栈",
        max_turns=4,
    )
    engine = SceneEngine(scene, config, [agent_a, agent_b], SnapshotManager("proj-se"))

    # mock 角色回应，避免真实 LLM 调用
    with patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="*点头* 我明白了。")):
        result = await engine.run()

    assert result.snapshot_id_before
    assert result.snapshot_id_after
    assert result.turns_completed == 4
    assert len(result.dialogue_log) == 4
    # round-robin 应交替发言
    assert result.dialogue_log[0].character_id == "c1"
    assert result.dialogue_log[1].character_id == "c2"


@pytest.mark.asyncio
async def test_scene_run_perceives_all_participants_without_duplication():
    """工单15：每轮台词应写入本场全部参演角色的记忆恰好一次（在场即记忆），
    而不是只写发言者本人、也不是重复写入。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    scene = Scene(scene_id="s-perceive", project_id="proj-se", branch_id="b-perceive")
    config = SceneConfig(
        name="对峙",
        description="两人对峙",
        participating_characters=["c1", "c2"],
        location="客栈",
        max_turns=4,
    )
    engine = SceneEngine(scene, config, [agent_a, agent_b], SnapshotManager("proj-se"))

    # 每轮回应内容不同，避免"同一发言者说了两句相同的话"掩盖了真正的去重校验
    replies = [f"*点头* 第{i}句回应。[内心{i}]" for i in range(4)]
    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=replies)),
        # 跳过固化，只检查场景运行过程中短期缓冲的写入情况
        patch("backend.memory.memory_manager.MemoryManager.consolidate", new=AsyncMock()),
    ):
        await engine.run()

    texts_a = agent_a.memory.short_term.dump()
    texts_b = agent_b.memory.short_term.dump()
    # 4 轮全部感知到了两个角色的记忆中，而不只是各自发言的 2 轮
    assert len(texts_a) == 4
    assert len(texts_b) == 4
    # 同一轮台词在自己的缓冲中不应出现重复记录
    assert len(texts_a) == len(set(texts_a))
    assert len(texts_b) == len(set(texts_b))


@pytest.mark.asyncio
async def test_scene_run_strips_inner_thought_for_other_agents():
    """工单15/契约1：写入他人轮次时必须剥离内心独白，避免私有内心泄露给旁观角色。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    scene = Scene(scene_id="s-thought", project_id="proj-se", branch_id="b-thought")
    config = SceneConfig(
        name="对峙", participating_characters=["c1", "c2"], location="客栈", max_turns=2
    )
    engine = SceneEngine(scene, config, [agent_a, agent_b], SnapshotManager("proj-se"))

    with (
        patch.object(
            CharacterAgent, "respond", new=AsyncMock(return_value="*点头* 我明白了。[这局面不妙]")
        ),
        patch("backend.memory.memory_manager.MemoryManager.consolidate", new=AsyncMock()),
    ):
        await engine.run()

    # 第 1 轮由 c1（甲）发言：甲自己的记忆保留内心独白，乙（旁观者）的记忆不应包含
    assert "这局面不妙" in agent_a.memory.short_term.dump()[0]
    assert "这局面不妙" not in agent_b.memory.short_term.dump()[0]


@pytest.mark.asyncio
async def test_scene_run_after_snapshot_has_empty_short_term_buffer():
    """固化必须先于后置快照，否则 continue/rollback/next_scene 的记忆回填
    （prime()）会让已经写入长期记忆的台词随下一场的 consolidate 被二次写入
    （长期记忆按角色+项目共享、不随分支回滚，重复只会累积）。

    验证方式：真实（不 mock）走 consolidate，检查后置快照里保存的
    short_term_buffer 必须是空的——这正是"已落库、可安全回填"的标志。
    """
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    scene = Scene(scene_id="s-order", project_id="proj-se", branch_id="b-order")
    config = SceneConfig(
        name="对峙", participating_characters=["c1", "c2"], location="客栈", max_turns=2
    )
    sm = SnapshotManager("proj-se")
    engine = SceneEngine(scene, config, [agent_a, agent_b], sm)

    with patch.object(
        CharacterAgent, "respond", new=AsyncMock(return_value="*点头* 我明白了。")
    ):
        result = await engine.run()

    snap_after = await sm.get_snapshot(result.snapshot_id_after)
    assert snap_after is not None
    for cid in ("c1", "c2"):
        assert snap_after.character_states[cid].short_term_buffer == []


# ---------------------------------------------------------------------------
# 工单11：selector 独立评分选人
# ---------------------------------------------------------------------------


def _selector(*agents: CharacterAgent) -> ScoringSpeakerSelector:
    return ScoringSpeakerSelector(list(agents))


def _score_json(urge: int, relevance: int, initiative: int) -> str:
    return (
        f'{{"urge": {urge}, "relevance": {relevance}, '
        f'"initiative": {initiative}, "reason": "测试"}}'
    )


def _turn(cid: str, name: str, number: int, dialogue: str = "随便说点什么") -> DialogueTurn:
    return DialogueTurn(
        scene_id="s-sel",
        turn_number=number,
        character_id=cid,
        character_name=name,
        dialogue=dialogue,
    )


def test_detect_addressed_prefers_longest_name():
    """中文没有词边界，短名不得被长名误命中（旧实现的 `name in choice` 之痛）。"""
    names = {"c1": "李明", "c2": "李明远"}
    assert detect_addressed("李明远，你怎么看？", names) == {"c2"}
    assert detect_addressed("李明，你怎么看？", names) == {"c1"}


@pytest.mark.asyncio
async def test_selector_picks_highest_scorer():
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    sel = _selector(agent_a, agent_b)

    with patch(
        "backend.scene_engine.speaker_selector.chat_safe",
        new=AsyncMock(side_effect=[_score_json(2, 2, 2), _score_json(9, 9, 9)]),
    ):
        chosen, trace = await sel.select(["甲: 开场"], [_turn("c1", "甲", 1)])

    assert chosen.character_id == "c2"
    assert trace.llm_failures == 0 and not trace.degraded


@pytest.mark.asyncio
async def test_selector_falls_back_to_median_on_partial_failure():
    """单个候选打分失败时取其余人的中位数，而不是被 0 分永久排除。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    sel = _selector(agent_a, agent_b)

    with patch(
        "backend.scene_engine.speaker_selector.chat_safe",
        new=AsyncMock(side_effect=[LLMError("boom"), _score_json(5, 5, 5)]),
    ):
        _chosen, trace = await sel.select(["乙: 开场"], [_turn("c2", "乙", 1)])

    failed = next(s for s in trace.scores if s.character_id == "c1")
    assert trace.llm_failures == 1 and not trace.degraded
    assert (failed.urge, failed.relevance, failed.initiative) == (5.0, 5.0, 5.0)


@pytest.mark.asyncio
async def test_selector_degrades_to_local_scoring_when_all_fail():
    """全部打分失败不得静默回退到 agents[0]：应走纯本地分并标记 degraded。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    sel = _selector(agent_a, agent_b)

    with patch(
        "backend.scene_engine.speaker_selector.chat_safe",
        new=AsyncMock(side_effect=LLMError("boom")),
    ):
        # 甲刚发过言，纯本地分下重复惩罚应把发言权让给乙
        chosen, trace = await sel.select(["甲: 开场"], [_turn("c1", "甲", 1)])

    assert trace.degraded and trace.llm_failures == 2
    assert chosen.character_id == "c2"


@pytest.mark.asyncio
async def test_selector_repeat_penalty_suppresses_recent_speaker():
    """同分时，刚发过言的人应被重复惩罚压下去。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    sel = _selector(agent_a, agent_b)

    with patch(
        "backend.scene_engine.speaker_selector.chat_safe",
        new=AsyncMock(return_value=_score_json(7, 7, 7)),
    ):
        chosen, trace = await sel.select(["甲: 开场"], [_turn("c1", "甲", 1)])

    penalty_a = next(s for s in trace.scores if s.character_id == "c1").penalty
    penalty_b = next(s for s in trace.scores if s.character_id == "c2").penalty
    assert penalty_a > penalty_b == 0.0
    assert chosen.character_id == "c2"


@pytest.mark.asyncio
async def test_selector_addressed_bonus_overrides_lower_llm_score():
    """被直呼其名的角色即使 LLM 打分略低，也应因加分而被选中。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    sel = _selector(agent_a, agent_b)

    with patch(
        "backend.scene_engine.speaker_selector.chat_safe",
        new=AsyncMock(side_effect=[_score_json(6, 6, 6), _score_json(5, 5, 5)]),
    ):
        chosen, trace = await sel.select(
            ["丙: 乙，你到底知道什么？"],
            [_turn("c3", "丙", 1, dialogue="乙，你到底知道什么？")],
        )

    assert next(s for s in trace.scores if s.character_id == "c2").addressed
    assert chosen.character_id == "c2"


@pytest.mark.asyncio
async def test_selector_treats_unparseable_output_as_failure():
    agent_a = _make_agent("c1", "甲")
    sel = _selector(agent_a)

    with patch(
        "backend.scene_engine.speaker_selector.chat_safe",
        new=AsyncMock(return_value="我觉得应该让甲说话"),
    ):
        _chosen, trace = await sel.select(["甲: 开场"], [_turn("c1", "甲", 1)])

    assert trace.degraded and trace.llm_failures == 1


@pytest.mark.asyncio
async def test_selector_prompt_never_contains_unknown_facts():
    """契约1：打分器只能看 known_facts，unknown_facts 是导演专属。"""
    agent = _make_agent("c1", "甲")
    agent.card.known_facts = ["王子已经回城"]
    agent.card.unknown_facts = ["公主其实是刺客"]
    sel = _selector(agent)

    captured: list[list[dict]] = []

    async def _capture(messages, **_kwargs):
        captured.append(messages)
        return _score_json(5, 5, 5)

    with patch("backend.scene_engine.speaker_selector.chat_safe", new=_capture):
        await sel.select(["甲: 开场"], [_turn("c1", "甲", 1)])

    blob = "".join(m["content"] for m in captured[0])
    assert "王子已经回城" in blob
    assert "公主其实是刺客" not in blob


@pytest.mark.asyncio
async def test_engine_selector_mode_uses_scoring_selector():
    """首轮无历史轮次走 round_robin，之后交给评分选择器。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    scene = Scene(scene_id="s-sel-run", project_id="proj-se", branch_id="b-sel")
    config = SceneConfig(
        name="对峙",
        participating_characters=["c1", "c2"],
        location="客栈",
        max_turns=3,
        speaker_mode=SpeakerMode.SELECTOR.value,
    )
    engine = SceneEngine(scene, config, [agent_a, agent_b], SnapshotManager("proj-se"))

    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="*点头* 我明白了。")),
        patch(
            "backend.scene_engine.speaker_selector.chat_safe",
            new=AsyncMock(return_value=_score_json(5, 5, 5)),
        ),
    ):
        result = await engine.run()

    assert result.turns_completed == 3
    assert result.dialogue_log[0].character_id == "c1"
    # 全员同分时重复惩罚保证不会连着选同一个人
    assert result.dialogue_log[1].character_id == "c2"
    assert all(t.selector_notice == "" for t in result.dialogue_log)


@pytest.mark.asyncio
async def test_engine_marks_turns_when_selector_degrades():
    """选人降级必须落到轮次上，前端才能在角色名后给出灰字提示。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    scene = Scene(scene_id="s-sel-degrade", project_id="proj-se", branch_id="b-sel")
    config = SceneConfig(
        name="对峙",
        participating_characters=["c1", "c2"],
        max_turns=3,
        speaker_mode=SpeakerMode.SELECTOR.value,
    )
    engine = SceneEngine(scene, config, [agent_a, agent_b], SnapshotManager("proj-se"))

    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="*点头* 我明白了。")),
        patch(
            "backend.scene_engine.speaker_selector.chat_safe",
            new=AsyncMock(side_effect=LLMError("boom")),
        ),
    ):
        result = await engine.run()

    # 首轮走开场顺序，未经过 selector，不应带提示
    assert result.dialogue_log[0].selector_notice == ""
    assert result.dialogue_log[1].selector_notice == "服务不可用：降级选择"


@pytest.mark.asyncio
async def test_engine_unknown_speaker_mode_warns_once(caplog):
    """非法 speaker_mode 仍回退 round_robin，但必须留下可见告警。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    scene = Scene(scene_id="s-sel-bad", project_id="proj-se", branch_id="b-sel")
    config = SceneConfig(
        name="对峙", participating_characters=["c1", "c2"], max_turns=3, speaker_mode="foo"
    )
    engine = SceneEngine(scene, config, [agent_a, agent_b], SnapshotManager("proj-se"))

    with (
        caplog.at_level("WARNING"),
        patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="*点头* 我明白了。")),
    ):
        result = await engine.run()

    warnings = [r for r in caplog.records if "speaker_mode" in r.getMessage()]
    assert len(warnings) == 1
    assert [t.character_id for t in result.dialogue_log] == ["c1", "c2", "c1"]


def test_create_scene_request_rejects_unknown_speaker_mode():
    from pydantic import ValidationError

    from backend.api.schemas import CreateSceneRequest

    assert CreateSceneRequest(branch_id="b", name="x").speaker_mode == ""
    with pytest.raises(ValidationError):
        CreateSceneRequest(branch_id="b", name="x", speaker_mode="foo")
