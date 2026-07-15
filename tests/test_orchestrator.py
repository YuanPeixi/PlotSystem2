"""orchestrator.apply_decision 的回滚（rollback）分支测试。

验证工单 01：回滚后角色卡应写回快照状态，且应创建一个新场景并设置
decision.next_scene_id，新场景的 initial_conditions 与传入的
new_initial_conditions 一致。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.models import (
    CharacterCard,
    CharacterState,
    DirectorDecision,
    Project,
    RelationshipState,
    Scene,
    SceneEvaluation,
    SceneResult,
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
    )
    await repository.save_project(Project(project_id=project_id, name="noop"))
    await repository.save_scene(scene)

    override = DirectorDecision(decision_type="rollback")
    decision = await orchestrator.apply_decision(scene.scene_id, override)

    assert decision.next_scene_id is None


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

    async def fake_build_agents(pid, cids):
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
