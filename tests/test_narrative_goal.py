"""工单28：主线目标锚点的持久化与跨场传递。"""

from __future__ import annotations

import json

from backend.models import (
    PROGRESS_UNAVAILABLE,
    CharacterCard,
    DirectorDecision,
    Project,
    Scene,
    SceneConfig,
    SceneEvaluation,
    goal_revision,
)
from backend.services import orchestrator, repository
from backend.utils import db


async def _insert_legacy_project(project_id: str) -> None:
    """写入一条改造前的项目记录：data_json 里没有任何新字段。"""
    legacy = {
        "project_id": project_id,
        "name": "老项目",
        "description": "改造前建的",
        "seed_texts": [],
        "status": "ready",
    }
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO projects "
            "(project_id, name, description, status, created_at, updated_at, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, "老项目", "改造前建的", "ready", "", "", json.dumps(legacy)),
        )
        await conn.commit()


async def _insert_legacy_evaluation(scene_id: str) -> None:
    legacy = {
        "scene_id": scene_id,
        "synopsis": "老评估",
        "narrative_goal_score": 7.0,
        "dramatic_tension_score": 6.0,
        "plot_deviation_score": 2.0,
        "character_consistency_score": 8.0,
        "recommended_decision": "next_scene",
    }
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO evaluations (scene_id, created_at, data_json) "
            "VALUES (?, ?, ?)",
            (scene_id, "", json.dumps(legacy)),
        )
        await conn.commit()


async def test_legacy_project_deserializes_with_empty_goal():
    """老项目的 data_json 没有新字段，读取必须给默认值而不是 KeyError。"""
    await _insert_legacy_project("proj-legacy-goal")

    project = await repository.get_project("proj-legacy-goal")
    assert project.narrative_goal == ""
    assert project.ending_criteria == ""

    listed = {p.project_id: p for p in await repository.list_projects()}
    assert listed["proj-legacy-goal"].narrative_goal == ""


async def test_legacy_evaluation_progress_is_unavailable_not_zero():
    """老评估没度量过进度，退成 0.0 会伪装成"一点没推进"并让后续场次被判停滞。"""
    await _insert_legacy_evaluation("scene-legacy-eval")
    evaluation = await repository.get_evaluation("scene-legacy-eval")
    assert evaluation is not None
    assert evaluation.story_progress == PROGRESS_UNAVAILABLE
    assert evaluation.goal_revision == ""
    assert evaluation.unresolved_threads == []


async def test_evaluation_round_trip_keeps_new_fields():
    await repository.save_evaluation(
        SceneEvaluation(
            scene_id="scene-eval-roundtrip",
            story_progress=0.4,
            story_progress_raw=0.35,
            progress_stalled=True,
            is_ending_reached=True,
            ending_reason="丞相伏法",
            unresolved_threads=["公主的身世"],
            goal_revision="abc123",
        )
    )
    loaded = await repository.get_evaluation("scene-eval-roundtrip")
    assert loaded is not None
    assert loaded.story_progress == 0.4
    assert loaded.story_progress_raw == 0.35
    assert loaded.progress_stalled is True
    assert loaded.is_ending_reached is True
    assert loaded.ending_reason == "丞相伏法"
    assert loaded.unresolved_threads == ["公主的身世"]
    assert loaded.goal_revision == "abc123"


class _CapturingDirector:
    """记录导演收到的规划参数，避免真实 LLM 调用。"""

    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def plan_scene(
        self,
        branch_id,
        narrative_goal,
        cards,
        history_scenes=None,
        scene_intent="",
        recent_results=None,
    ):
        type(self).calls.append(
            {
                "goal": narrative_goal,
                "intent": scene_intent,
                "recent": list(recent_results or []),
            }
        )
        return SceneConfig(
            name="AI 规划场景",
            description="AI 规划描述",
            participating_characters=[c.character_id for c in cards[:1]],
            location="某处",
        )

    async def make_decision(self, evaluation, human_override):
        return human_override


async def _setup_project(project_id: str, goal: str) -> str:
    await repository.save_project(
        Project(project_id=project_id, name="锚点项目", narrative_goal=goal)
    )
    await repository.save_character(
        CharacterCard(character_id=f"{project_id}-c1", project_id=project_id, name="王子")
    )
    return f"{project_id}-c1"


async def test_plan_scene_falls_back_to_project_goal(monkeypatch):
    project_id = "proj-goal-fallback"
    await _setup_project(project_id, "扳倒丞相")
    _CapturingDirector.calls = []
    monkeypatch.setattr(orchestrator, "DirectorAgent", _CapturingDirector)

    await orchestrator.plan_scene(project_id, "branch-main", scene_intent="让公主试探王子")

    call = _CapturingDirector.calls[-1]
    assert call["goal"] == "扳倒丞相"
    assert call["intent"] == "让公主试探王子"


async def test_next_scene_decision_keeps_project_goal_across_scenes(monkeypatch):
    """验收2：连跑多场都走 next_scene，主线目标不得被"延续上一场"顶掉。"""
    project_id = "proj-goal-chain"
    character_id = await _setup_project(project_id, "扳倒丞相")
    _CapturingDirector.calls = []
    monkeypatch.setattr(orchestrator, "DirectorAgent", _CapturingDirector)

    scene = Scene(
        scene_id="scene-goal-chain-1",
        project_id=project_id,
        branch_id="branch-main",
        name="第一场",
        participating_characters=[character_id],
        status="completed",
    )
    await repository.save_scene(scene)

    for _ in range(3):
        decision = await orchestrator.apply_decision(
            scene.scene_id, DirectorDecision(decision_type="next_scene")
        )
        scene = await repository.get_scene(decision.next_scene_id)
        scene.status = "completed"
        await repository.save_scene(scene)

    assert len(_CapturingDirector.calls) == 3
    assert all(c["goal"] == "扳倒丞相" for c in _CapturingDirector.calls)
    assert all(c["intent"] == "" for c in _CapturingDirector.calls)


async def test_story_context_follows_parent_chain_across_branches():
    """分叉出的 IF 线首场应继承来源分支在分叉点的进度，而不是从 0 重爬。"""
    project_id = "proj-progress-lineage"
    await repository.save_project(Project(project_id=project_id, name="谱系项目"))

    main_scene = Scene(
        scene_id="scene-lineage-main",
        project_id=project_id,
        branch_id="branch-main",
        name="主线一场",
        status="completed",
    )
    await repository.save_scene(main_scene)
    await repository.save_evaluation(
        SceneEvaluation(
            scene_id=main_scene.scene_id,
            story_progress=0.5,
            goal_revision=goal_revision(""),
            unresolved_threads=["公主的身世"],
        )
    )

    # 分叉首场：parent_scene_id 指向来源分支的场景（分叉不变量 I4）
    fork_scene = Scene(
        scene_id="scene-lineage-fork",
        project_id=project_id,
        branch_id="branch-if",
        parent_scene_id=main_scene.scene_id,
        name="IF 首场",
    )
    await repository.save_scene(fork_scene)

    progress, threads, _ = await orchestrator._story_context(fork_scene)
    assert progress == 0.5
    assert threads == ["公主的身世"]


async def test_story_context_uses_same_branch_history_when_chain_breaks():
    """手工建的场景没有 parent_scene_id，链会断，需退到同分支内更早的场景。"""
    project_id = "proj-progress-manual"
    await repository.save_project(Project(project_id=project_id, name="手建场景项目"))

    first = Scene(
        scene_id="scene-manual-1",
        project_id=project_id,
        branch_id="branch-main",
        name="手建一场",
        status="completed",
    )
    await repository.save_scene(first)
    await repository.save_evaluation(
        SceneEvaluation(
            scene_id=first.scene_id, story_progress=0.3, goal_revision=goal_revision("")
        )
    )

    second = Scene(
        scene_id="scene-manual-2",
        project_id=project_id,
        branch_id="branch-main",
        name="手建二场",
    )
    await repository.save_scene(second)

    progress, _, _ = await orchestrator._story_context(second)
    assert progress == 0.3


async def test_story_context_without_history_is_unavailable():
    project_id = "proj-progress-empty"
    await repository.save_project(Project(project_id=project_id, name="空项目"))
    scene = Scene(
        scene_id="scene-progress-empty",
        project_id=project_id,
        branch_id="branch-main",
        name="首场",
    )
    await repository.save_scene(scene)

    progress, threads, synopses = await orchestrator._story_context(scene)
    assert progress == PROGRESS_UNAVAILABLE
    assert threads == []
    assert synopses == []


# --- PR review 修复 -----------------------------------------------------------


async def _chain(project_id: str, names: list[str]) -> list[Scene]:
    """建一条 parent_scene_id 相连的场景链，返回从旧到新的列表。"""
    await repository.save_project(Project(project_id=project_id, name=project_id))
    scenes: list[Scene] = []
    parent: str | None = None
    for i, name in enumerate(names):
        scene = Scene(
            scene_id=f"{project_id}-s{i}",
            project_id=project_id,
            branch_id="branch-main",
            parent_scene_id=parent,
            name=name,
            status="completed",
        )
        await repository.save_scene(scene)
        scenes.append(scene)
        parent = scene.scene_id
    return scenes


async def test_resolved_threads_do_not_resurrect():
    """A 留下线索、B 显式收束成空表，C 必须看到空表而不是回头取 A 的。

    旧实现把"空列表"和"还没找到线索状态"混为一谈，会让已收束的线索被提示词
    重新要求模型保留。
    """
    a, b, c = await _chain("proj-threads-resurrect", ["A", "B", "C"])
    await repository.save_evaluation(
        SceneEvaluation(scene_id=a.scene_id, unresolved_threads=["公主的身世"])
    )
    await repository.save_evaluation(
        SceneEvaluation(scene_id=b.scene_id, unresolved_threads=[])
    )

    _, threads, _ = await orchestrator._story_context(c)
    assert threads == []


async def test_threads_inherited_when_latest_evaluation_missing():
    """B 压根没评估过（≠ 显式收束）时仍应沿用 A 的线索。"""
    a, _b, c = await _chain("proj-threads-missing-eval", ["A", "B", "C"])
    await repository.save_evaluation(
        SceneEvaluation(scene_id=a.scene_id, unresolved_threads=["公主的身世"])
    )

    _, threads, _ = await orchestrator._story_context(c)
    assert threads == ["公主的身世"]


async def test_progress_not_inherited_across_goal_change():
    """换了主线目标就是换了尺子：旧目标下的 0.9 不得钳死新目标的真实进度。"""
    a, b = await _chain("proj-goal-changed", ["A", "B"])
    await repository.save_evaluation(
        SceneEvaluation(
            scene_id=a.scene_id,
            story_progress=0.9,
            goal_revision=goal_revision("扳倒丞相"),
        )
    )

    same, _, _ = await orchestrator._story_context(b, "扳倒丞相")
    assert same == 0.9

    changed, _, _ = await orchestrator._story_context(b, "找回失踪的妹妹")
    assert changed == PROGRESS_UNAVAILABLE


async def test_legacy_evaluation_progress_not_inherited():
    """旧记录没有 goal_revision，其进度不该被当成当前目标下的度量。"""
    a, b = await _chain("proj-goal-legacy-rev", ["A", "B"])
    await repository.save_evaluation(
        SceneEvaluation(scene_id=a.scene_id, story_progress=0.7)  # goal_revision 为空
    )

    progress, _, _ = await orchestrator._story_context(b, "扳倒丞相")
    assert progress == PROGRESS_UNAVAILABLE


async def test_story_context_collects_ancestor_synopses_in_time_order():
    """结局往往跨场次达成：评估时导演必须看得到前面几场发生了什么。"""
    a, b, c = await _chain("proj-synopses", ["揭露叛徒", "两家对峙", "当下"])
    await repository.save_evaluation(
        SceneEvaluation(scene_id=a.scene_id, synopsis="王子当众揭穿了丞相")
    )
    await repository.save_evaluation(
        SceneEvaluation(scene_id=b.scene_id, synopsis="两家在朝堂上剑拔弩张")
    )

    _, _, synopses = await orchestrator._story_context(c)
    assert len(synopses) == 2
    assert "王子当众揭穿了丞相" in synopses[0]
    assert "两家在朝堂上剑拔弩张" in synopses[1]
