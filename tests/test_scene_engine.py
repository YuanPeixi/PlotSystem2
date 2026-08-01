"""SceneEngine 测试：使用 mock LLM，验证快照前置、轮次解析、终止条件。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.memory import MemoryManager
from backend.models import CharacterCard, Scene, SceneConfig
from backend.scene_engine import SceneEngine
from backend.scene_engine.termination import check_termination
from backend.snapshot import SnapshotManager


def _make_agent(cid: str, name: str) -> CharacterAgent:
    card = CharacterCard(character_id=cid, project_id="proj-se", name=name, persona="测试角色")
    mem = MemoryManager(cid, "proj-se")
    return CharacterAgent(card, mem)


def test_termination_max_turns():
    from backend.models import DialogueTurn

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
