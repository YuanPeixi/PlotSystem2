"""真实 run → 快照 → fork/rollback 链路；只替换模型和无关的记忆 IO。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from backend.agents import CharacterAgent, DirectorAgent
from backend.memory import MemoryManager
from backend.models import (
    CharacterCard,
    DirectorDecision,
    Project,
    Scene,
    SceneEvaluation,
    goal_revision,
)
from backend.services import orchestrator, repository
from backend.snapshot import SnapshotManager
from backend.utils import db

GOAL = "揭露叛徒"


@pytest.fixture
async def story(monkeypatch, request):
    pid = request.node.name
    await repository.save_project(Project(project_id=pid, name=pid, narrative_goal=GOAL))
    await repository.save_character(CharacterCard(project_id=pid, character_id="c", name="甲"))
    monkeypatch.setattr(MemoryManager, "connect", AsyncMock())
    monkeypatch.setattr(MemoryManager, "add_experience", AsyncMock())
    monkeypatch.setattr(MemoryManager, "consolidate", AsyncMock())
    monkeypatch.setattr(CharacterAgent, "respond", AsyncMock(return_value="正在调查。"))

    async def evaluate(self, scene, *args, **kwargs):
        first = scene.name == "A"
        return SceneEvaluation(
            scene_id=scene.scene_id,
            story_progress=0.2 if first else 0.9,
            goal_revision=goal_revision(GOAL),
            synopsis="叛徒未明" if first else "叛徒已揭露",
            unresolved_threads=["叛徒身份"] if first else [],
        )

    monkeypatch.setattr(DirectorAgent, "evaluate_scene", evaluate)
    a = Scene(
        project_id=pid, branch_id="main", name="A", participating_characters=["c"], max_turns=1
    )
    await repository.save_scene(a)
    await orchestrator.run_scene(a.scene_id)
    b = Scene(
        project_id=pid,
        branch_id="main",
        parent_scene_id=a.scene_id,
        name="B",
        participating_characters=["c"],
        max_turns=1,
    )
    await repository.save_scene(b)
    await orchestrator.run_scene(b.scene_id)
    b = await repository.get_scene(b.scene_id)
    assert b.snapshot_id_before and b.snapshot_id_after
    assert (await repository.get_evaluation(b.scene_id)).story_progress == 0.9
    assert (await repository.get_evaluation(b.scene_id)).evaluated_snapshot_id == b.snapshot_id_after
    return a, b, SnapshotManager(pid)


async def test_before_snapshot_rollback_excludes_source_outcome(story):
    _, b, _ = story
    result = await orchestrator.apply_decision(
        b.scene_id, DirectorDecision(decision_type="rollback")
    )
    child = await repository.get_scene(result.next_scene_id)
    progress, threads, synopses = await orchestrator._story_context(child, GOAL)
    assert progress == 0.2
    assert threads == ["叛徒身份"]
    assert synopses == ["【A】叛徒未明"]


async def test_after_snapshot_keeps_its_evaluation_after_source_continues(story):
    _, b, sm = story
    old_snapshot = b.snapshot_id_after
    # 真正续跑源场景，产生另一份后置快照和覆盖式评估。
    b.name = "A"  # fixture 的模型现在输出另一份结果
    b.max_turns = 2
    await repository.save_scene(b)
    await orchestrator.run_scene(b.scene_id)
    assert (await repository.get_scene(b.scene_id)).snapshot_id_after != old_snapshot
    _, child = await orchestrator.fork_from_snapshot(b.project_id, old_snapshot, "旧后置分叉")
    progress, threads, synopses = await orchestrator._story_context(child, GOAL)
    assert progress == 0.9
    assert threads == []
    assert synopses[-1] == "【B】叛徒已揭露"
    # 删除来源快照也不改变已创建分支的认知。
    await sm.delete_snapshot(old_snapshot)
    child = await repository.get_scene(child.scene_id)
    assert (await orchestrator._story_context(child, GOAL))[0] == 0.9


async def test_nested_fork_keeps_original_cutoff(story):
    _, b, sm = story
    _, child = await orchestrator.fork_from_snapshot(b.project_id, b.snapshot_id_before, "第一层")
    await orchestrator.run_scene(child.scene_id)
    child = await repository.get_scene(child.scene_id)
    _, nested = await orchestrator.fork_from_snapshot(
        b.project_id, child.snapshot_id_before, "第二层"
    )
    assert await orchestrator._story_context(nested, GOAL) == (0.2, ["叛徒身份"], ["【A】叛徒未明"])
    snap = await sm.get_snapshot(child.snapshot_id_before)
    assert snap.story_history is not None  # 确认真正写入/还原了快照字段
    indexed = next(s for s in await sm.list_snapshots() if s["snapshot_id"] == snap.snapshot_id)
    assert indexed["story_history"] == snap.story_history


async def test_legacy_snapshot_does_not_guess_future_evaluation(story, caplog):
    _, b, sm = story
    legacy = await sm.create_snapshot(b.scene_id, b.branch_id, {}, label="after:legacy")
    _, child = await orchestrator.fork_from_snapshot(b.project_id, legacy.snapshot_id, "旧格式")
    assert await orchestrator._story_context(child, GOAL) == (-1, [], [])
    assert "没有导演历史副本" in caplog.text


async def test_pending_next_scene_freezes_previous_result(story, monkeypatch):
    _, b, _ = story
    from backend.models import SceneConfig

    monkeypatch.setattr(
        orchestrator, "plan_scene", AsyncMock(return_value=SceneConfig(name="下一场"))
    )
    result = await orchestrator.apply_decision(
        b.scene_id, DirectorDecision(decision_type="next_scene")
    )
    await repository.save_evaluation(
        SceneEvaluation(scene_id=b.scene_id, story_progress=0.1, goal_revision=goal_revision(GOAL))
    )
    child = await repository.get_scene(result.next_scene_id)
    assert (await orchestrator._story_context(child, GOAL))[0] == 0.9


async def test_scene_created_at_survives_resave(story):
    a, b, _ = story
    loaded = await repository.get_scene(a.scene_id)
    assert loaded.created_at == a.created_at
    await repository.save_scene(loaded)
    async with db.connect() as conn:
        row = await (
            await conn.execute(
                "SELECT created_at, data_json FROM scenes WHERE scene_id=?", (a.scene_id,)
            )
        ).fetchone()
    assert row[0] == json.loads(row[1])["created_at"] == a.created_at.isoformat()
    assert [s.scene_id for s in await repository.list_scenes(b.project_id)][:2] == [
        a.scene_id,
        b.scene_id,
    ]


async def test_review_cutoff_survives_next_scene(story):
    _, b, _ = story
    _, fork = await orchestrator.fork_from_snapshot(b.project_id, b.snapshot_id_before, "IF")
    await repository.save_evaluation(
        SceneEvaluation(
            scene_id=fork.scene_id,
            story_progress=0.2,
            goal_revision=goal_revision(GOAL),
            synopsis="IF 未揭露叛徒",
            unresolved_threads=["叛徒身份"],
        )
    )
    next_scene = Scene(
        project_id=b.project_id,
        branch_id=fork.branch_id,
        parent_scene_id=fork.scene_id,
        name="IF 下一场",
    )
    await repository.save_scene(next_scene)
    first = await orchestrator._story_context(fork, GOAL)
    later = await orchestrator._story_context(next_scene, GOAL)
    assert not any("【B】" in s for s in first[2])
    assert not any("【B】" in s for s in later[2]), later


async def test_fork_during_evaluation_keeps_history_known_at_fork(story, monkeypatch):
    a, b, _ = story
    captured = []

    async def evaluate(self, scene, *args, **kwargs):
        current = await repository.get_scene(scene.scene_id)
        _, child = await orchestrator.fork_from_snapshot(
            scene.project_id, current.snapshot_id_after, "评估尚未返回"
        )
        captured.append(child)
        return SceneEvaluation(
            scene_id=scene.scene_id,
            story_progress=0.7,
            goal_revision=goal_revision(GOAL),
            synopsis="新的结局",
        )

    monkeypatch.setattr(DirectorAgent, "evaluate_scene", evaluate)
    b.max_turns = 2
    await repository.save_scene(b)
    await orchestrator.run_scene(b.scene_id)
    child = await repository.get_scene(captured[0].scene_id)
    assert await orchestrator._story_context(child, GOAL) == (0.2, ["叛徒身份"], ["【A】叛徒未明"])
    # 同一快照补评估完成后创建的新分支可见本轮结果，已有分支不追写。
    current = await repository.get_scene(b.scene_id)
    _, later = await orchestrator.fork_from_snapshot(
        b.project_id, current.snapshot_id_after, "评估完成"
    )
    assert (await orchestrator._story_context(later, GOAL))[0] == 0.7


async def test_empty_history_survives_parent_changes_and_snapshot_deletion(story):
    a, _, sm = story
    current = await repository.get_scene(a.scene_id)
    _, child = await orchestrator.fork_from_snapshot(
        a.project_id, current.snapshot_id_before, "空起点"
    )
    assert child.inherited_story_history == []
    await repository.save_evaluation(
        SceneEvaluation(
            scene_id=a.scene_id,
            story_progress=1,
            goal_revision=goal_revision(GOAL),
            synopsis="未来",
        )
    )
    await sm.delete_snapshot(current.snapshot_id_before)
    child = await repository.get_scene(child.scene_id)
    assert await orchestrator._story_context(child, GOAL) == (-1, [], [])


async def test_failed_evaluation_leaves_snapshot_at_known_history(story, monkeypatch):
    _, b, sm = story
    monkeypatch.setattr(
        DirectorAgent, "evaluate_scene", AsyncMock(side_effect=RuntimeError("model down"))
    )
    b.max_turns = 2
    await repository.save_scene(b)
    await orchestrator.run_scene(b.scene_id)
    current = await repository.get_scene(b.scene_id)
    assert current.status == "completed"
    _, child = await orchestrator.fork_from_snapshot(
        b.project_id, current.snapshot_id_after, "评估失败"
    )
    assert await orchestrator._story_context(child, GOAL) == (0.2, ["叛徒身份"], ["【A】叛徒未明"])


async def test_history_write_failure_keeps_valid_evaluation(story, monkeypatch, caplog):
    _, b, _ = story
    monkeypatch.setattr(
        SnapshotManager, "record_story_history", AsyncMock(side_effect=OSError("disk error"))
    )
    monkeypatch.setattr(
        DirectorAgent,
        "evaluate_scene",
        AsyncMock(
            return_value=SceneEvaluation(
                scene_id=b.scene_id, story_progress=0.6, goal_revision=goal_revision(GOAL)
            )
        ),
    )
    b.max_turns = 2
    await repository.save_scene(b)
    await orchestrator.run_scene(b.scene_id)
    assert (await repository.get_scene(b.scene_id)).status == "completed"
    assert (await repository.get_evaluation(b.scene_id)).story_progress == 0.6
    assert "后置快照导演历史补写失败" in caplog.text
