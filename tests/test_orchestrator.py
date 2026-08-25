"""orchestrator.apply_decision 的回滚（rollback）分支测试。

验证工单 01：回滚后角色卡应写回快照状态，且应创建一个新场景并设置
decision.next_scene_id，新场景的 initial_conditions 与传入的
new_initial_conditions 一致。

另外验证工单13（升级后的数据库级幂等保护）：
- 并发重复提交被 scenes.status 的 CAS 条件更新拦截，只产生一个新场景；
- 顺序重试命中 decisions 表重放，返回与首次完全相同的 next_scene_id；
- 非 completed 状态的场景不可提交决策；已决策场景提交不同类型报冲突；
- continue 不持久化决策，重跑完成后开启新一轮可决策周期；
- next_scene 决策支持人工覆盖参与角色/地点/初始条件，未提供时保持 AI 自动规划行为。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.exceptions import ConflictError
from backend.models import (
    CharacterCard,
    CharacterState,
    DialogueTurn,
    DirectorDecision,
    Project,
    RelationshipState,
    Scene,
    SceneConfig,
    SceneEvaluation,
    SceneResult,
    SpeakerMode,
)
from backend.services import orchestrator, repository
from backend.snapshot import SnapshotManager


async def _setup_project_scene_and_snapshot():
    project_id = "proj-rollback-test"
    character_id = "char-rollback-test"

    project = Project(project_id=project_id, name="回滚测试项目")
    await repository.save_project(project)

    card = CharacterCard(
        character_id=character_id,
        project_id=project_id,
        name="测试角色",
        current_emotion="平静",
        current_goal="原始目标",
        current_location="原始地点",
    )
    await repository.save_character(card)

    scene = Scene(
        scene_id="scene-rollback-test",
        project_id=project_id,
        branch_id="branch-main",
        name="第一场",
        description="测试场景",
        participating_characters=[character_id],
        location="原始地点",
        initial_conditions={"weather": "sunny"},
        max_turns=10,
        status="completed",  # 只有 completed 场景可被决策（CAS 守卫）
    )
    await repository.save_scene(scene)

    # 快照中的角色状态与当前角色卡不同，用于验证回滚后是否写回
    sm = SnapshotManager(project_id)
    states = {
        character_id: CharacterState(
            character_id=character_id,
            current_emotion="愤怒",
            current_goal="快照中的目标",
            current_location="快照中的地点",
            relationships={
                "other": RelationshipState(
                    target_character_id="other", relation_type="敌对", strength=-0.5
                )
            },
        )
    }
    snap = await sm.create_snapshot(scene.scene_id, scene.branch_id, states, label="before")

    return project_id, character_id, scene, snap


@pytest.mark.asyncio
async def test_rollback_updates_character_and_creates_new_scene():
    project_id, character_id, scene, snap = await _setup_project_scene_and_snapshot()
    scene.speaker_mode = SpeakerMode.SELECTOR.value
    await repository.save_scene(scene)

    override = DirectorDecision(
        decision_type="rollback",
        rollback_to_snapshot_id=snap.snapshot_id,
        new_initial_conditions={"weather": "storm", "tension": "high"},
        rollback_notes="剧情走偏，回滚重演",
    )

    decision = await orchestrator.apply_decision(scene.scene_id, override)

    # 1. 决策应指向新创建的场景
    assert decision.next_scene_id
    assert decision.next_scene_id != scene.scene_id

    # 2. 新场景应正确创建，且携带回滚指定的新初始条件
    new_scene = await repository.get_scene(decision.next_scene_id)
    assert new_scene.parent_scene_id == scene.scene_id
    assert new_scene.initial_conditions == {"weather": "storm", "tension": "high"}
    assert new_scene.participating_characters == [character_id]
    assert new_scene.status == "pending"
    # 新场景不应携带 snapshot_id_before，否则 SceneEngine.run() 会跳过
    # 自己的模拟前快照创建，导致重演场景无法反映 new_initial_conditions
    # 恢复后的最新状态（工单 01 回归项）。
    assert new_scene.snapshot_id_before == ""
    # 但应记住回滚来源快照，供 run_scene 回填运行时记忆（工協14）
    assert new_scene.restore_snapshot_id == snap.snapshot_id    # 选人模式不能因重演而静默退回 round_robin
    assert new_scene.speaker_mode == SpeakerMode.SELECTOR.value
    # 3. 角色卡应被写回快照中的状态（而不是回滚前的最新状态）
    card = await repository.get_character(project_id, character_id)
    assert card.current_emotion == "愤怒"
    assert card.current_goal == "快照中的目标"
    assert card.current_location == "快照中的地点"
    assert card.relationships["other"].relation_type == "敌对"


@pytest.mark.asyncio
async def test_rollback_without_snapshot_target_is_noop_but_safe():
    """当场景既没有 snapshot_id_before 也没有指定 rollback_snapshot_id 时，
    不应抛异常，也不应设置 next_scene_id。"""
    project_id = "proj-rollback-noop"
    scene = Scene(
        scene_id="scene-rollback-noop",
        project_id=project_id,
        branch_id="branch-main",
        name="无快照场景",
        snapshot_id_before="",
        status="completed",
    )
    await repository.save_project(Project(project_id=project_id, name="noop"))
    await repository.save_scene(scene)

    override = DirectorDecision(decision_type="rollback")
    decision = await orchestrator.apply_decision(scene.scene_id, override)

    assert decision.next_scene_id is None
    # 未执行任何变更：不应持久化决策，用户补充快照 ID 后可重试
    assert await repository.get_decision(scene.scene_id) is None


@pytest.mark.asyncio
async def test_run_scene_rejects_concurrent_duplicate_start(monkeypatch):
    """工单 10：同一场景被并发/重复触发 start 时，只应真正执行一次模拟。"""
    project_id = "proj-concurrent-test"
    character_id = "char-concurrent-test"
    await repository.save_project(Project(project_id=project_id, name="并发测试项目"))
    await repository.save_character(
        CharacterCard(character_id=character_id, project_id=project_id, name="测试角色")
    )
    scene = Scene(
        scene_id="scene-concurrent-test",
        project_id=project_id,
        branch_id="branch-main",
        name="并发测试场景",
        participating_characters=[character_id],
        max_turns=2,
    )
    await repository.save_scene(scene)

    call_count = {"agents": 0, "engine_run": 0}

    async def fake_build_agents(pid, cids, states=None):
        call_count["agents"] += 1
        # 制造并发窗口：让第二次调用有机会在第一次完成前发起
        await asyncio.sleep(0.05)
        return []

    class FakeEngine:
        def __init__(self, *args, **kwargs):
            pass

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None):
            call_count["engine_run"] += 1
            await asyncio.sleep(0.05)
            return SceneResult(
                scene_id=scene.scene_id,
                dialogue_log=[],
                snapshot_id_before="",
                snapshot_id_after="snap-fake",
                turns_completed=0,
                terminated_reason="max_turns",
            )

    class FakeDirector:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate_scene(self, *args, **kwargs):
            return SceneEvaluation(scene_id=scene.scene_id)

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    await asyncio.gather(
        orchestrator.run_scene(scene.scene_id),
        orchestrator.run_scene(scene.scene_id),
    )

    assert call_count["agents"] == 1
    assert call_count["engine_run"] == 1
    assert orchestrator.is_scene_active(scene.scene_id) is False


class _FakeDirectorForDecision:
    """伪造 DirectorAgent：make_decision 直接透传人工决策，plan_scene 返回固定建议。

    避免真实 LLM 调用，同时可通过 plan_delay/captured 观察 apply_decision 的行为。
    """

    plan_delay: float = 0.0
    captured_goal: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def make_decision(self, evaluation, human_override):
        return human_override

    async def plan_scene(self, branch_id, narrative_goal, cards, history_scenes=None):
        if self.plan_delay:
            await asyncio.sleep(self.plan_delay)
        if self.captured_goal is not None:
            self.captured_goal["goal"] = narrative_goal
        return SceneConfig(
            name="AI规划场景",
            description="AI规划描述",
            participating_characters=[c.character_id for c in cards[:1]],
            location="AI地点",
            initial_conditions={"from": "ai"},
            max_turns=10,
        )


@pytest.mark.asyncio
async def test_apply_decision_rejects_concurrent_duplicate_submission(monkeypatch):
    """工单13：同一场景被并发/重复提交决策时，只应真正创建一个新场景，
    另一次提交应收到明确的 ConflictError（而不是静默产生第二个孤儿场景）。"""
    project_id = "proj-decision-concurrent"
    character_id = "char-decision-concurrent"
    await repository.save_project(Project(project_id=project_id, name="并发决策测试项目"))
    await repository.save_character(
        CharacterCard(character_id=character_id, project_id=project_id, name="测试角色")
    )
    scene = Scene(
        scene_id="scene-decision-concurrent",
        project_id=project_id,
        branch_id="branch-main",
        name="决策并发测试场景",
        participating_characters=[character_id],
        status="completed",
    )
    await repository.save_scene(scene)

    FakeDirector = type(
        "FakeDirector", (_FakeDirectorForDecision,), {"plan_delay": 0.05}
    )
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    override = DirectorDecision(decision_type="next_scene")
    results = await asyncio.gather(
        orchestrator.apply_decision(scene.scene_id, override),
        orchestrator.apply_decision(scene.scene_id, override),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, DirectorDecision)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ConflictError)

    all_scenes = await repository.list_scenes(project_id, "branch-main")
    children = [s for s in all_scenes if s.parent_scene_id == scene.scene_id]
    assert len(children) == 1
    # 决策已持久化，且指向唯一的新场景
    persisted = await repository.get_decision(scene.scene_id)
    assert persisted is not None
    assert persisted.next_scene_id == children[0].scene_id


@pytest.mark.asyncio
async def test_next_scene_decision_applies_user_overrides(monkeypatch):
    """工单13：next_scene 决策的人工覆盖字段（角色/地点/初始条件）应生效，
    覆盖 AI 自动规划的对应字段；叙事目标也应采用用户提交的描述。"""
    project_id = "proj-next-scene-override"
    char_ai = "char-ai-suggested"
    char_user = "char-user-picked"
    await repository.save_project(Project(project_id=project_id, name="下一场覆盖测试项目"))
    for cid, name in ((char_ai, "AI推荐角色"), (char_user, "用户指定角色")):
        await repository.save_character(
            CharacterCard(character_id=cid, project_id=project_id, name=name)
        )
    scene = Scene(
        scene_id="scene-next-scene-override",
        project_id=project_id,
        branch_id="branch-main",
        name="上一场",
        participating_characters=[char_ai],
        status="completed",
    )
    await repository.save_scene(scene)

    captured: dict = {}
    FakeDirector = type(
        "FakeDirector", (_FakeDirectorForDecision,), {"captured_goal": captured}
    )
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    override = DirectorDecision(
        decision_type="next_scene",
        next_scene_description="用户自定义目标",
        next_participating_characters=[char_user],
        next_location="用户指定地点",
        next_initial_conditions={"from": "user"},
    )
    decision = await orchestrator.apply_decision(scene.scene_id, override)

    assert captured["goal"] == "用户自定义目标"
    new_scene = await repository.get_scene(decision.next_scene_id)
    assert new_scene.participating_characters == [char_user]
    assert new_scene.location == "用户指定地点"
    assert new_scene.initial_conditions == {"from": "user"}


@pytest.mark.asyncio
async def test_next_scene_decision_without_overrides_uses_ai_plan(monkeypatch):
    """未提供覆盖字段时，next_scene 决策行为应与改动前一致（纯 AI 自动规划）。"""
    project_id = "proj-next-scene-default"
    char_ai = "char-ai-default"
    await repository.save_project(Project(project_id=project_id, name="下一场默认测试项目"))
    await repository.save_character(
        CharacterCard(character_id=char_ai, project_id=project_id, name="AI推荐角色")
    )
    scene = Scene(
        scene_id="scene-next-scene-default",
        project_id=project_id,
        branch_id="branch-main",
        name="上一场",
        participating_characters=[char_ai],
        status="completed",
    )
    await repository.save_scene(scene)

    monkeypatch.setattr(orchestrator, "DirectorAgent", _FakeDirectorForDecision)

    override = DirectorDecision(decision_type="next_scene")
    decision = await orchestrator.apply_decision(scene.scene_id, override)

    new_scene = await repository.get_scene(decision.next_scene_id)
    assert new_scene.participating_characters == [char_ai]
    assert new_scene.location == "AI地点"
    assert new_scene.initial_conditions == {"from": "ai"}


async def _setup_completed_scene(project_id: str, scene_id: str, character_id: str) -> Scene:
    """创建一个 completed 状态的场景及其项目/角色，用于决策幂等测试。"""
    await repository.save_project(Project(project_id=project_id, name=project_id))
    await repository.save_character(
        CharacterCard(character_id=character_id, project_id=project_id, name="测试角色")
    )
    scene = Scene(
        scene_id=scene_id,
        project_id=project_id,
        branch_id="branch-main",
        name="已完成场景",
        participating_characters=[character_id],
        max_turns=10,
        turns_completed=10,
        status="completed",
    )
    await repository.save_scene(scene)
    return scene


@pytest.mark.asyncio
async def test_apply_decision_sequential_retry_replays_same_result(monkeypatch):
    """工单13 核心回归：第一次决策完成后的顺序重试（网络重放/慢速二次点击）
    应幂等重放同一个 next_scene_id，不再创建新场景、不再调用 LLM。"""
    scene = await _setup_completed_scene(
        "proj-decision-retry", "scene-decision-retry", "char-decision-retry"
    )

    plan_calls = {"n": 0}

    class CountingDirector(_FakeDirectorForDecision):
        async def plan_scene(self, branch_id, narrative_goal, cards, history_scenes=None):
            plan_calls["n"] += 1
            return await super().plan_scene(branch_id, narrative_goal, cards, history_scenes)

    monkeypatch.setattr(orchestrator, "DirectorAgent", CountingDirector)

    override = DirectorDecision(decision_type="next_scene")
    first = await orchestrator.apply_decision(scene.scene_id, override)
    second = await orchestrator.apply_decision(scene.scene_id, override)
    # AI 自动决策（human_override=None）的重试同样命中重放
    third = await orchestrator.apply_decision(scene.scene_id, None)

    assert first.next_scene_id
    assert second.next_scene_id == first.next_scene_id
    assert third.next_scene_id == first.next_scene_id
    # 重放不重复规划（LLM 只调用一次）
    assert plan_calls["n"] == 1
    # 场景总数不变：只有一个子场景
    all_scenes = await repository.list_scenes(scene.project_id, "branch-main")
    children = [s for s in all_scenes if s.parent_scene_id == scene.scene_id]
    assert len(children) == 1


@pytest.mark.asyncio
async def test_apply_decision_conflicting_type_after_decided(monkeypatch):
    """场景已有生效决策后，提交不同类型的决策应报冲突而不是静默重放。"""
    scene = await _setup_completed_scene(
        "proj-decision-conflict-type", "scene-decision-conflict-type", "char-conflict-type"
    )
    monkeypatch.setattr(orchestrator, "DirectorAgent", _FakeDirectorForDecision)

    await orchestrator.apply_decision(scene.scene_id, DirectorDecision(decision_type="next_scene"))

    with pytest.raises(ConflictError):
        await orchestrator.apply_decision(scene.scene_id, DirectorDecision(decision_type="rollback"))


@pytest.mark.asyncio
async def test_apply_decision_rejected_when_scene_not_completed(monkeypatch):
    """CAS 守卫：非 completed 状态的场景（pending/running）不可提交决策。"""
    project_id = "proj-decision-not-completed"
    await repository.save_project(Project(project_id=project_id, name=project_id))
    scene = Scene(
        scene_id="scene-decision-not-completed",
        project_id=project_id,
        branch_id="branch-main",
        name="未完成场景",
        status="pending",
    )
    await repository.save_scene(scene)
    monkeypatch.setattr(orchestrator, "DirectorAgent", _FakeDirectorForDecision)

    with pytest.raises(ConflictError):
        await orchestrator.apply_decision(
            scene.scene_id, DirectorDecision(decision_type="next_scene")
        )


@pytest.mark.asyncio
async def test_continue_decision_opens_new_decision_cycle(monkeypatch):
    """continue 不持久化决策：场景重置为 pending 重跑；重跑期间的重试被拒绝；
    重跑完成（重新 completed）后允许提交新决策。"""
    scene = await _setup_completed_scene(
        "proj-decision-continue", "scene-decision-continue", "char-decision-continue"
    )
    monkeypatch.setattr(orchestrator, "DirectorAgent", _FakeDirectorForDecision)

    rerun_calls = {"n": 0}

    async def fake_run_scene(scene_id: str) -> None:
        rerun_calls["n"] += 1

    monkeypatch.setattr(orchestrator, "run_scene", fake_run_scene)

    decision = await orchestrator.apply_decision(
        scene.scene_id, DirectorDecision(decision_type="continue", extra_turns=4)
    )
    await asyncio.sleep(0)  # 让 create_task 的续跑任务被调度

    assert decision.next_scene_id == scene.scene_id
    assert rerun_calls["n"] == 1
    # continue 不写入 decisions 表
    assert await repository.get_decision(scene.scene_id) is None
    # 场景已重置为 pending，轮次上限增加
    updated = await repository.get_scene(scene.scene_id)
    assert updated.status == "pending"
    assert updated.max_turns == scene.turns_completed + 4

    # 重跑期间（pending）重试 continue：被 CAS 守卫拒绝，max_turns 不会二次膨胀
    with pytest.raises(ConflictError):
        await orchestrator.apply_decision(
            scene.scene_id, DirectorDecision(decision_type="continue", extra_turns=4)
        )
    assert (await repository.get_scene(scene.scene_id)).max_turns == scene.turns_completed + 4

    # 模拟重跑完成：场景重新 completed，新一轮决策（next_scene）应被接受
    updated.status = "completed"
    updated.turns_completed = updated.max_turns
    await repository.save_scene(updated)
    next_decision = await orchestrator.apply_decision(
        scene.scene_id, DirectorDecision(decision_type="next_scene")
    )
    assert next_decision.next_scene_id
    assert next_decision.next_scene_id != scene.scene_id


# ---------------------------------------------------------------------------
# 工单14：续跑/回滚后的运行时记忆继承
# ---------------------------------------------------------------------------


def _memory_state(character_id: str) -> CharacterState:
    return CharacterState(
        character_id=character_id,
        episodic_summary="[重要] 测试角色: 我发誓要复仇",
        short_term_buffer=["测试角色: 前情一", "测试角色: 前情二"],
    )


@pytest.mark.asyncio
async def test_build_character_agents_primes_runtime_memory(monkeypatch):
    """工单14：传入快照状态时，新建的 MemoryManager 应回填短期缓冲与事件摘要，
    而不是每次都从空白开始（此前只有 ChromaDB 长期记忆是连续的）。"""
    from backend.memory.memory_manager import MemoryManager

    async def _skip_connect(self):  # 避免测试触碰 ChromaDB
        return None

    monkeypatch.setattr(MemoryManager, "connect", _skip_connect)

    project_id = "proj-memory-inherit"
    character_id = "char-memory-inherit"
    await repository.save_project(Project(project_id=project_id, name="记忆继承测试项目"))
    await repository.save_character(
        CharacterCard(character_id=character_id, project_id=project_id, name="测试角色")
    )

    state = _memory_state(character_id)
    agents = await orchestrator.build_character_agents(
        project_id, [character_id], {character_id: state}
    )
    mem = agents[0].memory
    assert mem.episodic.summary == state.episodic_summary
    assert mem.short_term.dump() == state.short_term_buffer

    # 不传状态时保持原行为（全新记忆）
    fresh = await orchestrator.build_character_agents(project_id, [character_id])
    assert fresh[0].memory.episodic.summary == ""
    assert fresh[0].memory.short_term.dump() == []


@pytest.mark.asyncio
async def test_character_state_snapshot_restoration(monkeypatch):
    """Issue #13：场景智能体应使用快照状态，而不是角色卡的最新值。"""
    from backend.memory.memory_manager import MemoryManager

    async def _skip_connect(self):
        return None

    monkeypatch.setattr(MemoryManager, "connect", _skip_connect)

    project_id = "proj-character-state-inherit"
    character_id = "char-character-state-inherit"
    await repository.save_project(Project(project_id=project_id, name="角色状态继承测试项目"))
    await repository.save_character(
        CharacterCard(
            character_id=character_id,
            project_id=project_id,
            name="测试角色",
            current_emotion="沉着",
            current_goal="继续调查",
            current_location="工作台地点",
            relationships={
                "other": RelationshipState(
                    target_character_id="other", relation_type="友善", strength=0.6
                )
            },
        )
    )
    state = CharacterState(
        character_id=character_id,
        current_emotion="恐惧",
        current_goal="逃离现场",
        current_location="快照地点",
        relationships={
            "other": RelationshipState(
                target_character_id="other", relation_type="敌对", strength=-0.8
            )
        },
    )

    agents = await orchestrator.build_character_agents(
        project_id, [character_id], {character_id: state}
    )
    agent = agents[0]
    prompt = agent.build_system_prompt({"location": "场景默认地点"})

    assert agent.card.current_emotion == "恐惧"
    assert agent.card.current_goal == "逃离现场"
    assert agent.card.current_location == "快照地点"
    assert agent.card.relationships == state.relationships
    assert agent.card.relationships is not state.relationships
    assert "情绪：恐惧" in prompt
    assert "目标：逃离现场" in prompt
    assert "位置：快照地点" in prompt
    assert "对 other：敌对" in prompt
    assert "沉着" not in prompt
    assert "继续调查" not in prompt
    assert "工作台地点" not in prompt


@pytest.mark.asyncio
async def test_load_inherited_states_prefers_rollback_source_snapshot():
    """回滚重演场景：snapshot_id_before 必须为空（工单01），
    因此运行时记忆要靠 restore_snapshot_id 找回来源快照（工单14）。"""
    project_id = "proj-memory-rollback-inherit"
    character_id = "char-memory-rollback-inherit"
    await repository.save_project(Project(project_id=project_id, name="回滚记忆继承项目"))

    sm = SnapshotManager(project_id)
    snap = await sm.create_snapshot(
        "scene-origin", "branch-main", {character_id: _memory_state(character_id)}, label="before"
    )

    replay = Scene(
        scene_id="scene-memory-rollback-replay",
        project_id=project_id,
        branch_id="branch-main",
        name="回滚重演",
        participating_characters=[character_id],
        snapshot_id_before="",
        restore_snapshot_id=snap.snapshot_id,
    )
    await repository.save_scene(replay)

    states = await orchestrator._load_inherited_states(replay, sm)
    assert states[character_id].episodic_summary == "[重要] 测试角色: 我发誓要复仇"


@pytest.mark.asyncio
async def test_load_inherited_states_falls_back_to_parent_scene():
    """next_scene：新场景本身没有任何快照，应承接父场景结束态的运行时记忆。"""
    project_id = "proj-memory-parent-inherit"
    character_id = "char-memory-parent-inherit"
    await repository.save_project(Project(project_id=project_id, name="父场景记忆继承项目"))

    sm = SnapshotManager(project_id)
    snap = await sm.create_snapshot(
        "scene-parent", "branch-main", {character_id: _memory_state(character_id)}, label="after"
    )
    parent = Scene(
        scene_id="scene-parent",
        project_id=project_id,
        branch_id="branch-main",
        name="上一场",
        snapshot_id_after=snap.snapshot_id,
        status="completed",
    )
    await repository.save_scene(parent)

    child = Scene(
        scene_id="scene-child",
        project_id=project_id,
        branch_id="branch-main",
        parent_scene_id=parent.scene_id,
        name="下一场",
        participating_characters=[character_id],
    )
    await repository.save_scene(child)

    states = await orchestrator._load_inherited_states(child, sm)
    assert states[character_id].short_term_buffer == ["测试角色: 前情一", "测试角色: 前情二"]


@pytest.mark.asyncio
async def test_memory_snapshot_keeps_short_term_buffer():
    """工单14：snapshot() 曾因先 consolidate 再 dump 导致短期缓冲恒为空。"""
    from backend.memory.memory_manager import MemoryManager

    mem = MemoryManager("char-snapshot-order", "proj-snapshot-order")

    async def _skip_connect():
        return None

    mem.connect = _skip_connect  # type: ignore[method-assign]
    mem.short_term.load(["甲: 第一句", "甲: 第二句"])

    snap = await mem.snapshot()
    assert snap.short_term_buffer == ["甲: 第一句", "甲: 第二句"]


@pytest.mark.asyncio
async def test_run_scene_persists_each_turn(monkeypatch):
    """工单23：轮次必须逐轮落盘，中途刷新/断线才能从场景详情拿回已产生的对话。"""
    project_id = "proj-incremental-test"
    await repository.save_project(Project(project_id=project_id, name="逐轮落盘测试"))
    scene = Scene(
        scene_id="scene-incremental-test",
        project_id=project_id,
        branch_id="branch-main",
        name="逐轮落盘场景",
        max_turns=2,
    )
    await repository.save_scene(scene)

    # 引擎跑到一半时数据库里应该已经能看到"运行中 + 第一轮"
    mid_state: dict = {}

    class FakeEngine:
        def __init__(self, scene_obj, *args, **kwargs):
            self.scene = scene_obj

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None):
            turn = DialogueTurn(
                scene_id=self.scene.scene_id,
                turn_number=1,
                character_name="甲",
                dialogue="第一句",
            )
            self.scene.dialogue_log = [turn]
            self.scene.turns_completed = 1
            if on_turn:
                await on_turn(turn)
            stored = await repository.get_scene(self.scene.scene_id)
            mid_state["status"] = stored.status
            mid_state["turns"] = len(stored.dialogue_log)
            self.scene.status = "completed"
            return SceneResult(
                scene_id=self.scene.scene_id,
                dialogue_log=[turn],
                snapshot_id_before="",
                snapshot_id_after="snap-fake",
                turns_completed=1,
                terminated_reason="max_turns",
            )

    class FakeDirector:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate_scene(self, *args, **kwargs):
            return SceneEvaluation(scene_id=scene.scene_id)

    async def fake_build_agents(pid, cids, states=None):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    await orchestrator.run_scene(scene.scene_id)

    assert mid_state == {"status": "running", "turns": 1}


@pytest.mark.asyncio
async def test_run_scene_failure_marks_scene_paused(monkeypatch):
    """运行失败必须落库，否则场景永远停在 running，前端既等不到也重启不了。"""
    project_id = "proj-failure-test"
    await repository.save_project(Project(project_id=project_id, name="失败落库测试"))
    scene = Scene(
        scene_id="scene-failure-test",
        project_id=project_id,
        branch_id="branch-main",
        name="失败场景",
    )
    await repository.save_scene(scene)

    class BoomEngine:
        def __init__(self, *args, **kwargs):
            pass

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None):
            raise RuntimeError("LLM 挂了")

    async def fake_build_agents(pid, cids, states=None):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", BoomEngine)

    await orchestrator.run_scene(scene.scene_id)

    stored = await repository.get_scene(scene.scene_id)
    assert stored.status == "paused"
    assert orchestrator.is_scene_active(scene.scene_id) is False


@pytest.mark.asyncio
async def test_reconcile_stale_scenes_marks_running_as_paused():
    """服务重启后遗留的 running 场景应被对账为 paused，否则前端永远显示模拟中。"""
    project_id = "proj-reconcile-test"
    await repository.save_project(Project(project_id=project_id, name="对账测试"))
    stale = Scene(
        scene_id="scene-stale-test",
        project_id=project_id,
        branch_id="branch-main",
        name="遗留运行场景",
        status="running",
    )
    done = Scene(
        scene_id="scene-done-test",
        project_id=project_id,
        branch_id="branch-main",
        name="已完成场景",
        status="completed",
    )
    await repository.save_scene(stale)
    await repository.save_scene(done)

    await orchestrator.reconcile_stale_scenes()

    assert (await repository.get_scene(stale.scene_id)).status == "paused"
    assert (await repository.get_scene(done.scene_id)).status == "completed"

