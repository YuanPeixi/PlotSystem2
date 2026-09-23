"""SceneEngine 测试：使用 mock LLM，验证快照前置、轮次解析、终止条件。"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.config import settings
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
async def test_after_snapshot_callback_fires_before_snapshot_is_indexed():
    """工单07：后置快照一进 snapshots 表就对 `/snapshots/{id}/fork` 可见，
    而本场的世界变量要等评估之后才补写回它。编排层的"待补写"守卫因此必须在
    **索引之前**就拿到快照 id —— 把回调挪到 create_snapshot 之后（乃至 run()
    返回之后）都会留下一个"可分叉但世界变量还没补写"的窗口，在那里分叉出的
    分支会永久缺失本场对世界的改动。
    """
    agent = _make_agent("c1", "甲")
    scene = Scene(scene_id="s-hook", project_id="proj-se", branch_id="b-hook")
    config = SceneConfig(name="回调", participating_characters=["c1"], max_turns=1)
    sm = SnapshotManager("proj-se")

    marked: list[str] = []
    marked_before_index: list[bool] = []
    original_index = sm._index_snapshot

    async def spy_index(snap):
        if snap.label.startswith("after:"):
            marked_before_index.append(snap.snapshot_id in marked)
        await original_index(snap)

    sm._index_snapshot = spy_index
    engine = SceneEngine(scene, config, [agent], sm)

    with patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="我明白了。")):
        result = await engine.run(on_after_snapshot=marked.append)

    # 后置快照被索引（= 对分叉可见）时，守卫已经挂上，且挂的是同一个 id
    assert marked_before_index == [True]
    assert marked == [result.snapshot_id_after]


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
async def test_resume_after_crash_replays_unconsolidated_turns_into_memory():
    """中断续跑：已落盘但未固化的轮次必须补写回记忆，否则"对话还在，角色忘了"。"""
    agent_a = _make_agent("c1", "甲")
    agent_b = _make_agent("c2", "乙")
    crashed = [
        DialogueTurn(
            scene_id="s-resume",
            turn_number=i + 1,
            character_id="c1" if i % 2 == 0 else "c2",
            character_name="甲" if i % 2 == 0 else "乙",
            dialogue=f"中断前第{i + 1}句",
        )
        for i in range(3)
    ]
    scene = Scene(
        scene_id="s-resume",
        project_id="proj-se",
        branch_id="b-resume",
        status="paused",
        snapshot_id_before="snap-before-resume",  # 已打过前置快照，续跑不重打
        dialogue_log=crashed,
        turns_completed=3,
        turns_consolidated=0,  # 崩溃时还没来得及固化
    )
    config = SceneConfig(
        name="续跑", participating_characters=["c1", "c2"], location="客栈", max_turns=4
    )
    engine = SceneEngine(scene, config, [agent_a, agent_b], SnapshotManager("proj-se"))
    engine.inject_history(crashed)

    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="*点头* 续上的第四句。")),
        patch("backend.memory.memory_manager.MemoryManager.consolidate", new=AsyncMock()),
    ):
        result = await engine.run()

    texts_a = agent_a.memory.short_term.dump()
    assert len(result.dialogue_log) == 4
    # 中断前的 3 轮 + 续跑产生的 1 轮都应进入记忆，且不重复
    assert len(texts_a) == 4
    assert len(texts_a) == len(set(texts_a))
    assert any("中断前第1句" in t for t in texts_a)
    assert scene.turns_consolidated == 4


@pytest.mark.asyncio
async def test_consolidated_turns_are_not_replayed_into_memory():
    """continue 续跑（上一轮已固化）不得把历史轮次二次写入记忆。"""
    agent_a = _make_agent("c1", "甲")
    done = [
        DialogueTurn(
            scene_id="s-cont",
            turn_number=i + 1,
            character_id="c1",
            character_name="甲",
            dialogue=f"已固化第{i + 1}句",
        )
        for i in range(2)
    ]
    scene = Scene(
        scene_id="s-cont",
        project_id="proj-se",
        branch_id="b-cont",
        snapshot_id_before="snap-before-cont",
        dialogue_log=done,
        turns_completed=2,
        turns_consolidated=2,  # 上一轮生命周期结束时已固化
    )
    config = SceneConfig(name="续跑", participating_characters=["c1"], max_turns=3)
    engine = SceneEngine(scene, config, [agent_a], SnapshotManager("proj-se"))
    engine.inject_history(done)

    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="新的一句。")),
        patch("backend.memory.memory_manager.MemoryManager.consolidate", new=AsyncMock()),
    ):
        await engine.run()

    texts = agent_a.memory.short_term.dump()
    assert len(texts) == 1
    assert not any("已固化" in t for t in texts)


@pytest.mark.asyncio
async def test_consolidation_watermark_persisted_before_after_snapshot():
    """固化完成后必须立刻落盘水位线，且早于后置快照。

    后置快照要拷贝整个 kuzu/chroma，进程若在这段窗口里被硬杀（走不到
    orchestrator 的 except），库里仍是旧水位线，续跑会把整场对话二次写入长期记忆。
    """
    agent = _make_agent("c1", "甲")
    scene = Scene(
        scene_id="s-watermark",
        project_id="proj-se",
        branch_id="b-watermark",
        snapshot_id_before="snap-before-watermark",  # 已有前置快照，只会打后置这一份
    )
    config = SceneConfig(name="水位线", participating_characters=["c1"], max_turns=2)
    sm = SnapshotManager("proj-se")
    engine = SceneEngine(scene, config, [agent], sm)

    seen: list[tuple[str, int]] = []

    async def _persist() -> None:
        seen.append(("persist", scene.turns_consolidated))

    original_create = SnapshotManager.create_snapshot

    async def _tracked_create(self, *args, **kwargs):
        seen.append(("snapshot", scene.turns_consolidated))
        return await original_create(self, *args, **kwargs)

    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="一句话。")),
        patch.object(SnapshotManager, "create_snapshot", new=_tracked_create),
    ):
        await engine.run(on_persist=_persist)

    assert [name for name, _ in seen] == ["persist", "snapshot"]
    assert seen[0][1] == len(scene.dialogue_log) == 2


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
# 工单26：场景内周期固化与水位线
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_consolidation_advances_watermark(monkeypatch):
    """场景内每满一个固化周期就必须固化并推进水位线，且在同一次落盘内写库。

    旧实现由 MemoryManager.add_experience 在缓冲写满时自行 consolidate，
    水位线毫不知情：跑满一个缓冲后崩溃，续跑会把已入库的轮次二次写入长期记忆。
    """
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 2)
    agent = _make_agent("c1", "甲")
    scene = Scene(
        scene_id="s-periodic",
        project_id="proj-se",
        branch_id="b-periodic",
        snapshot_id_before="snap-before-periodic",  # 已有前置快照，只打后置这一份
    )
    config = SceneConfig(name="长场", participating_characters=["c1"], max_turns=5)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))

    persisted: list[tuple[int, int]] = []

    async def _persist() -> None:
        persisted.append((len(scene.dialogue_log), scene.turns_consolidated))

    # 每轮内容必须不同：连续 4 轮重复会命中 check_termination 的"对话停滞"提前收场
    replies = [f"第{i}句话。" for i in range(5)]
    with patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=replies)):
        await engine.run(on_persist=_persist)

    # 2、4 轮时各固化一次，收尾再固化一次
    assert len(persisted) >= 3
    # 水位线**绝不能超前**于已产生的轮次——那等于宣称没写过的内容已入库。
    # 反向的落后是允许且有意的：固化前会先单独落一次日志（日志已到 N、水位线仍是
    # 旧值），崩溃续跑时重放该轮由内容寻址的幂等兜底收敛（工单26 复盘·断言1）。
    for turns_done, watermark in persisted:
        assert watermark <= turns_done
    # 每个固化周期结束时，必须存在一次"水位线追平轮次"的落盘
    for expected in (2, 4):
        assert (expected, expected) in persisted, persisted
    assert scene.turns_consolidated == 5


@pytest.mark.asyncio
async def test_periodic_consolidation_persists_before_on_turn(monkeypatch):
    """红线 R1：水位线落盘必须早于 on_turn（后者会推 SSE，落盘先于推送，工单23）。"""
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 2)
    agent = _make_agent("c1", "甲")
    scene = Scene(
        scene_id="s-order26",
        project_id="proj-se",
        branch_id="b-order26",
        snapshot_id_before="snap-before-order26",
    )
    config = SceneConfig(name="顺序", participating_characters=["c1"], max_turns=2)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))

    seen: list[str] = []

    async def _persist() -> None:
        seen.append("persist")

    async def _on_turn(_turn) -> None:
        seen.append("turn")

    with patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="一句话。")):
        await engine.run(on_turn=_on_turn, on_persist=_persist)

    # 第 2 轮触发周期固化，该轮里有两次 persist（工单26 复盘·断言1）：
    # 先单独落对话日志，再固化+推水位线；两次都必须排在同轮的 turn 之前。
    assert seen[:4] == ["turn", "persist", "persist", "turn"], seen
    # 无论多少次 persist，最后一轮之后都不得再有 turn 插进固化与落盘之间
    assert seen.index("turn", 1) > 2


@pytest.mark.asyncio
async def test_resume_replays_in_chunks_without_buffer_overflow(monkeypatch, caplog):
    """崩溃重放也要走周期固化。

    短期缓冲是定长 deque：一次补回远超容量的轮次会静默淘汰最早的内容，
    而那些内容还没进过长期记忆。旧实现靠 add_experience 的自动固化兜住，
    工单26 把触发权移走后，重放循环必须自己接上。
    """
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 3)
    agent = _make_agent("c1", "甲")
    agent.memory.short_term.capacity = 4
    agent.memory.short_term._buffer = deque(maxlen=4)
    agent.memory.short_term._meta = deque(maxlen=4)

    crashed = [
        DialogueTurn(
            scene_id="s-chunk",
            turn_number=i + 1,
            character_id="c1",
            character_name="甲",
            dialogue=f"崩溃前第{i + 1}句",
        )
        for i in range(9)
    ]
    scene = Scene(
        scene_id="s-chunk",
        project_id="proj-se",
        branch_id="b-chunk",
        status="paused",
        snapshot_id_before="snap-before-chunk",
        dialogue_log=crashed,
        turns_completed=9,
        turns_consolidated=0,  # 崩溃时一轮都没固化
    )
    config = SceneConfig(name="重放", participating_characters=["c1"], max_turns=9)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))
    engine.inject_history(crashed)

    written: list[str] = []

    async def _capture(text, _meta=None):
        written.append(text)

    with (
        caplog.at_level("WARNING"),
        patch.object(agent.memory.long_term, "add", new=_capture),
    ):
        await engine.run()

    # 9 轮全部进了长期记忆，没有任何一轮在 deque 里被静默淘汰
    assert len(written) == 9
    for i in range(9):
        assert any(f"崩溃前第{i + 1}句" in t for t in written)
    assert scene.turns_consolidated == 9
    assert not [r for r in caplog.records if "短期缓冲已达容量" in r.getMessage()]


@pytest.mark.asyncio
async def test_zero_period_falls_back_to_end_of_scene_only(monkeypatch):
    """周期配成 0 表示关闭场景内固化，收尾那次仍必须发生。"""
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 0)
    agent = _make_agent("c1", "甲")
    scene = Scene(
        scene_id="s-zero",
        project_id="proj-se",
        branch_id="b-zero",
        snapshot_id_before="snap-before-zero",
    )
    config = SceneConfig(name="关闭周期", participating_characters=["c1"], max_turns=3)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))

    persists = 0

    async def _persist() -> None:
        nonlocal persists
        persists += 1

    with patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="一句话。")):
        await engine.run(on_persist=_persist)

    assert persists == 1
    assert scene.turns_consolidated == 3


def _shrink_buffer(agent: CharacterAgent, capacity: int) -> None:
    """把短期缓冲容量改小，用于在少量轮次内复现溢出场景。"""
    agent.memory.short_term.capacity = capacity
    agent.memory.short_term._buffer = deque(maxlen=capacity)
    agent.memory.short_term._meta = deque(maxlen=capacity)


# ---------------------------------------------------------------------------
# 工单26 复盘：PR review 查出的三个洞
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidates_under_buffer_pressure_even_if_period_not_due(monkeypatch):
    """缓冲逼近容量时必须强制固化，哪怕固化周期还没到点。

    只看周期不看容量时：周期(20) > 容量(3) 的配置下跑 8 轮，前 5 轮会在到达
    consolidate() 之前就被定长 deque 静默淘汰，而收尾固化照样把水位线推到 8 ——
    宣称"8 轮都已入库"，实际只进了 3 条，且无痕（不像重复写入还能被
    check_memory_dupes 查出来）。这比工单26 原本要修的重复写入更糟。
    """
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 20)
    agent = _make_agent("c1", "甲")
    _shrink_buffer(agent, 4)

    scene = Scene(
        scene_id="s-pressure",
        project_id="proj-se",
        branch_id="b-pressure",
        snapshot_id_before="snap-before-pressure",
    )
    config = SceneConfig(name="超长场", participating_characters=["c1"], max_turns=8)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))

    written: list[str] = []

    async def _capture(text, _meta=None):
        written.append(text)

    replies = [f"第{i}句话。" for i in range(8)]
    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=replies)),
        patch.object(agent.memory.long_term, "add", new=_capture),
    ):
        await engine.run()

    # 8 轮一条不少地进了长期记忆，水位线才配说"已入库 8 轮"
    assert len(written) == 8, f"漏写了 {8 - len(written)} 条：{written}"
    for i in range(8):
        assert any(f"第{i}句话。" in t for t in written)
    assert scene.turns_consolidated == 8


@pytest.mark.asyncio
async def test_zero_period_still_guards_buffer_capacity(monkeypatch):
    """周期=0（关闭场景内固化）时跨度是整场 max_turns，同样不能溢出缓冲。"""
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 0)
    agent = _make_agent("c1", "甲")
    _shrink_buffer(agent, 4)

    scene = Scene(
        scene_id="s-zero-guard",
        project_id="proj-se",
        branch_id="b-zero-guard",
        snapshot_id_before="snap-before-zero-guard",
    )
    config = SceneConfig(name="关周期长场", participating_characters=["c1"], max_turns=8)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))

    written: list[str] = []

    async def _capture(text, _meta=None):
        written.append(text)

    replies = [f"第{i}句话。" for i in range(8)]
    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=replies)),
        patch.object(agent.memory.long_term, "add", new=_capture),
    ):
        await engine.run()

    assert len(written) == 8
    assert scene.turns_consolidated == 8


@pytest.mark.asyncio
async def test_resume_rebuilds_episodic_below_watermark(monkeypatch):
    """续跑必须补回水位线**之前**那段的事件摘要，但不得重写它们的正文。

    事件摘要是纯内存的独立一层：不随长期记忆持久化，也不受 turns_consolidated
    保护。按水位线切重放会让 [0, watermark) 的重要事件在续跑后彻底消失。
    这个洞在工单26 之前就存在，只是那时水位线中途不推进、崩溃时通常为 0，
    重放恰好覆盖全部而掩盖了它。
    """
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 2)
    agent = _make_agent("c1", "甲")

    # "发誓"是 episodic 的重要关键词，4 轮全部命中
    crashed = [
        DialogueTurn(
            scene_id="s-epi",
            turn_number=i + 1,
            character_id="c1",
            character_name="甲",
            dialogue=f"我发誓第{i}件事。",
        )
        for i in range(4)
    ]
    scene = Scene(
        scene_id="s-epi",
        project_id="proj-se",
        branch_id="b-epi",
        status="paused",
        snapshot_id_before="snap-before-epi",
        dialogue_log=crashed,
        turns_completed=4,
        turns_consolidated=2,  # 前 2 轮已固化，崩在第 3、4 轮
    )
    config = SceneConfig(name="续跑摘要", participating_characters=["c1"], max_turns=4)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))
    engine.inject_history(crashed)

    written: list[str] = []

    async def _capture(text, _meta=None):
        written.append(text)

    with patch.object(agent.memory.long_term, "add", new=_capture):
        await engine.run()

    # 4 轮的重要事件一条不少
    summary = agent.memory.episodic.dump()
    for i in range(4):
        assert f"我发誓第{i}件事。" in summary, f"第{i}轮的事件摘要丢了：{summary}"

    # 但水位线之前的两轮**不得**重新写进长期记忆——它们已经在库里了
    for i in range(2):
        assert not any(f"我发誓第{i}件事。" in t for t in written), (
            f"第{i}轮被二次写入长期记忆：{written}"
        )
    # 水位线之后的两轮必须补写
    for i in (2, 3):
        assert any(f"我发誓第{i}件事。" in t for t in written)


@pytest.mark.asyncio
async def test_continue_replay_does_not_evict_earlier_episodes(monkeypatch):
    """正常 continue 的重放不得重复追加事件摘要，挤掉更早场次的重要事件。

    契约4 的四级优先级里，正常 continue 命中的是 `snapshot_id_after` —— 本场
    上次跑完时打的快照，其 `episodic_summary` 已经含了水位线之前的全部事件，
    `build_character_agents` 又已 `prime()` 载入。此时若逐轮 append 重放，
    这段就在 `_events` 里出现两遍；而 `build_summary()` 只保留末 10 条，
    等于把**更早场次**的重要事件挤出窗口。episodic 不落盘、无处可查，
    一旦挤掉就是永久丢失。

    修法不是"判断快照来源"：continue 跑到一半再崩时，`snapshot_id_after` 只
    覆盖前半段，按来源判断会漏掉后半段。整批按正文去重后重建，三种来源收敛
    到同一结果。
    """
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 20)
    agent = _make_agent("c1", "甲")

    # prime 的摘要 = 上一场留下的 4 条 + 本场已固化的 6 条，正好填满保留窗口
    prior = [f"[重要] 甲: 上一场的关键决定{i}。" for i in range(4)]
    current = [f"[重要] 甲: 我发誓第{i}件事。" for i in range(6)]
    agent.memory.prime(None, "\n".join(prior + current))

    log = [
        DialogueTurn(
            scene_id="s-cont-epi",
            turn_number=i + 1,
            character_id="c1",
            character_name="甲",
            dialogue=f"我发誓第{i}件事。",
        )
        for i in range(6)
    ]
    scene = Scene(
        scene_id="s-cont-epi",
        project_id="proj-se",
        branch_id="b-cont-epi",
        snapshot_id_before="snap-before-cont-epi",
        snapshot_id_after="snap-after-cont-epi",  # 跑完过一次 → 正常 continue
        dialogue_log=log,
        turns_completed=6,
        turns_consolidated=6,  # 全部已固化，重放只该补摘要
    )
    config = SceneConfig(name="续跑", participating_characters=["c1"], max_turns=8)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))
    engine.inject_history(log)

    with patch.object(CharacterAgent, "respond", new=AsyncMock(return_value="继续说。")):
        await engine.run()

    lines = agent.memory.episodic.dump().splitlines()
    assert len(lines) == len(set(lines)), f"事件摘要出现重复行：{lines}"
    for i in range(4):
        assert any(f"上一场的关键决定{i}。" in ln for ln in lines), (
            f"上一场的第{i}条重要事件被挤出窗口：{lines}"
        )
    for i in range(6):
        assert any(f"我发誓第{i}件事。" in ln for ln in lines)


def test_episodic_replay_is_idempotent_and_keeps_order():
    """`EpisodicMemory.replay` 的去重语义：重复调用不增长，且保持日志顺序。

    三种续跑来源（after 全含 / before 全不含 / 半途崩溃只含前半段）都会落到
    同一个终态，引擎因此不必去判断"prime 载入了多少"。
    """
    from backend.memory.episodic import EpisodicMemory

    turns = [
        DialogueTurn(
            turn_number=i + 1,
            character_id="c1",
            character_name="甲",
            dialogue=f"我发誓第{i}件事。",
        )
        for i in range(4)
    ]

    def _final(preloaded: int) -> list[str]:
        ep = EpisodicMemory("c1")
        ep.load("\n".join(f"[重要] 甲: 我发誓第{i}件事。" for i in range(preloaded)))
        ep.replay(turns, self_character_id="c1")
        ep.replay(turns, self_character_id="c1")  # 重复调用不得增长
        return ep._events

    baseline = _final(4)
    assert len(baseline) == 4
    assert _final(0) == baseline  # 崩溃续跑：一条都没载入
    assert _final(2) == baseline  # 半途崩溃：只载入了前半段

    # 不重要的轮次不进摘要，也不会顶掉已有条目
    ep = EpisodicMemory("c1")
    ep.replay(
        [DialogueTurn(turn_number=1, character_id="c1", character_name="甲", dialogue="今天天气不错")],
        self_character_id="c1",
    )
    assert ep._events == []


def test_episodic_entry_survives_dump_load_roundtrip():
    """`record → dump → load → replay` 的往返必须恒等，含多行动作（工单26 复盘）。

    摘要是"一行一条"序列化的（dump 用 \\n join、load 用 \\n split），而
    `SceneEngine._parse_turn` 的动作正则带 `re.DOTALL` —— 跨行的 *动作* 会产出
    含 \\n 的 `action`（对白已被 `re.sub(r"\\s+", " ")` 规整过，只有动作漏了）。
    条目正文一旦带换行，存进快照再恢复就裂成多条：既与重放生成的单条对不上
    （去重失效、条目净膨胀），又多占 `_events[-10:]` 的保留窗口挤掉更早的事件。
    """
    from backend.memory.episodic import EpisodicMemory

    turn = DialogueTurn(
        turn_number=1,
        character_id="c1",
        character_name="甲",
        action="拔出剑\n指向门口",  # _parse_turn 的 re.DOTALL 会这样产出
        dialogue="我发誓要走。",
    )

    ep = EpisodicMemory("c1")
    assert ep.record(turn) is True
    assert len(ep._events) == 1
    assert "\n" not in ep._events[0], f"条目正文里残留换行：{ep._events[0]!r}"

    restored = EpisodicMemory("c1")
    restored.load(ep.dump())
    assert restored._events == ep._events, "dump→load 不是恒等变换"

    # 正常 continue：快照恢复后再重放同一轮，必须收敛成同一条而不是累加
    restored.replay([turn], self_character_id="c1")
    assert restored._events == ep._events, f"重放后条目膨胀：{restored._events}"


def test_episodic_load_merges_legacy_multiline_entry():
    """改造前落盘的快照里存着多行条目，load 必须并回单条（无需迁移脚本）。

    并回后与 `_snippet` 的规整结果一致，因此老快照恢复出来的条目能与重放生成的
    条目对上，去重照常生效。
    """
    from backend.memory.episodic import EpisodicMemory

    turn = DialogueTurn(
        turn_number=1,
        character_id="c1",
        character_name="甲",
        action="拔出剑\n指向门口",
        dialogue="我发誓要走。",
    )
    legacy = "[重要] 甲: （拔出剑\n指向门口） 我发誓要走。\n[重要] 乙: 我恨你。"

    ep = EpisodicMemory("c1")
    ep.load(legacy)
    assert len(ep._events) == 2, f"多行条目没有并回：{ep._events}"
    assert all("\n" not in e for e in ep._events)

    ep.replay([turn], self_character_id="c1")
    assert len(ep._events) == 2, f"老条目与重放的新条目没对上：{ep._events}"


def test_episodic_replay_strips_others_inner_thought():
    """契约1：重放他人轮次时，内心独白不得参与判定、更不得进摘要。"""
    from backend.memory.episodic import EpisodicMemory

    turn = DialogueTurn(
        turn_number=1,
        character_id="c2",
        character_name="乙",
        dialogue="今天天气不错",
        inner_thought="我要背叛他",  # 只有独白里含重要关键词
    )

    others = EpisodicMemory("c1")
    others.replay([turn], self_character_id="c1")  # c1 视角：这是别人的轮次
    assert others._events == [], others._events

    owner = EpisodicMemory("c2")
    owner.replay([turn], self_character_id="c2")  # c2 视角：自己的轮次
    assert len(owner._events) == 1
    assert "背叛" not in owner._events[0], "内心独白泄露进了摘要正文"


@pytest.mark.asyncio
async def test_dialogue_log_persisted_before_consolidation(monkeypatch):
    """固化前先落一次对话日志，把跨库不一致导向"已有幂等兜底"的那一侧。

    Chroma 与 SQLite 没有跨库事务，写完长期记忆到水位线落盘之间必然有窗口。
    不先落日志的话，窗口内崩溃会留下"角色记得一句日志里还没有的台词"；
    先落日志则变成"日志有、水位线旧"，续跑重放该轮时撞上内容寻址的幂等兜底
    （long_term.memory_id）收敛成一条。后者有兜底，前者没有。
    """
    monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 3)
    agent = _make_agent("c1", "甲")
    scene = Scene(
        scene_id="s-window",
        project_id="proj-se",
        branch_id="b-window",
        snapshot_id_before="snap-before-window",
    )
    config = SceneConfig(name="窗口", participating_characters=["c1"], max_turns=3)
    engine = SceneEngine(scene, config, [agent], SnapshotManager("proj-se"))

    # 记录每次 on_persist 时"库里"会有几轮日志、水位线是多少
    persisted: list[tuple[int, int]] = []
    consolidated_at: list[int] = []

    async def _persist() -> None:
        persisted.append((len(scene.dialogue_log), scene.turns_consolidated))

    async def _capture(text, _meta=None):
        consolidated_at.append(len(scene.dialogue_log))

    replies = [f"第{i}句话。" for i in range(3)]
    with (
        patch.object(CharacterAgent, "respond", new=AsyncMock(side_effect=replies)),
        patch.object(agent.memory.long_term, "add", new=_capture),
    ):
        await engine.run(on_persist=_persist)

    # 第 3 轮触发周期固化：固化前有一次"日志已到 3、水位线仍是 0"的落盘
    assert (3, 0) in persisted, persisted
    # 长期记忆写入时，日志里已经有这一轮了——不存在"记得日志里没有的台词"
    assert consolidated_at and all(n >= 3 for n in consolidated_at)
    # 落盘顺序：日志先行，水位线随后
    log_first = persisted.index((3, 0))
    assert any(wm == 3 for _, wm in persisted[log_first:])


def test_consolidate_period_must_leave_buffer_headroom():
    """固化周期与缓冲容量的耦合必须是机制，不能只是一句注释。

    原实现把"应显著小于 SHORT_TERM_BUFFER_SIZE"写在 config.py 的注释里，
    但没有任何代码强制它 —— 配成等于容量就静默丢记忆。这类约束应当启动即失败
    （与 DEFAULT_SPEAKER_MODE 同理），而不是靠运行时 warning。
    """
    from backend.config import Settings

    with pytest.raises(ValueError, match="MEMORY_CONSOLIDATE_EVERY_TURNS"):
        Settings(SHORT_TERM_BUFFER_SIZE=40, MEMORY_CONSOLIDATE_EVERY_TURNS=40)
    with pytest.raises(ValueError, match="MEMORY_CONSOLIDATE_EVERY_TURNS"):
        Settings(SHORT_TERM_BUFFER_SIZE=40, MEMORY_CONSOLIDATE_EVERY_TURNS=60)
    # 默认配置必须合法，否则所有人启动即失败
    assert Settings().MEMORY_CONSOLIDATE_EVERY_TURNS > 0
    # 0 是合法的（关闭周期固化），由引擎的缓冲压力兜底
    assert Settings(MEMORY_CONSOLIDATE_EVERY_TURNS=0).MEMORY_CONSOLIDATE_EVERY_TURNS == 0


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
