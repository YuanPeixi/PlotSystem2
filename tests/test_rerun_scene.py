"""场景复测脚本（`scripts.rerun_scene`）：统计口径与无人值守时的停机条件。

不调 LLM：分叉与运行都换成假的，只验证脚本自己的逻辑。
"""

from __future__ import annotations

import json

from backend.models import (
    ActionIntent,
    ActionStatus,
    DialogueTurn,
    LLMUsageStat,
    Project,
    Scene,
    SceneEvaluation,
    SceneStatus,
    TurnKind,
    new_id,
)
from backend.services import orchestrator, repository
from scripts import rerun_scene


def _played_scene(*, projection: str | None = "雨夜，襁褓递出宫墙。") -> Scene:
    source = DialogueTurn(
        turn_id="t1", turn_number=1, character_id="queen", character_name="塞芙拉",
        action="王冠已举至鬓边", dialogue="退，还是不退？",
        actions=[
            ActionIntent(index=0, text="王冠已举至鬓边", object_id="crown", verb="戴上",
                         status=ActionStatus.RESOLVED.value),
            ActionIntent(index=1, text="转身", status=ActionStatus.SKIPPED.value, skip_reason="no_object"),
        ],
    )
    env = DialogueTurn(
        turn_id="e1", turn_number=2, kind=TurnKind.ENVIRONMENT.value, character_name="环境",
        narration="尚未触及发丝，玻璃表面便泛起微光。", projection=projection, source_turn_id="t1",
    )
    other = DialogueTurn(turn_id="t2", turn_number=3, character_id="isa", character_name="伊莎贝尔", dialogue="那襁褓在哪里？")
    return Scene(
        project_id="p", branch_id="b", name="正午加冕", status=SceneStatus.COMPLETED.value,
        dialogue_log=[source, env, other],
        llm_usage={
            "character": LLMUsageStat(calls=2, prompt_tokens=900, completion_tokens=80),
            "selector": LLMUsageStat(calls=4, failures=1, prompt_tokens=3000, completion_tokens=40),
            "embedding": LLMUsageStat(calls=6, prompt_tokens=5000),
        },
    )


def test_summary_attributes_the_projection_to_the_action_that_caused_it():
    scene = _played_scene()
    result = rerun_scene.summarize_scene(1, scene, SceneEvaluation(scene_id=scene.scene_id, story_progress=1.0))

    assert result.ok
    assert (result.character_turns, result.environment_turns) == (2, 1)
    # 没提到物件的动作段不算物件动作
    assert result.object_actions == {"resolved": 1}
    [trigger] = result.triggers
    # 动作原文与环境叙述并排留下：人靠它分辨"真戴上"与"举着就被判触发"
    assert (trigger.turn_number, trigger.actor, trigger.action) == (2, "塞芙拉", "王冠已举至鬓边")
    assert "尚未触及发丝" in trigger.narration
    # 向量化不计入对话模型的 token
    assert (result.prompt_tokens, result.completion_tokens, result.llm_failures) == (3900, 120, 1)
    assert result.story_progress == 1.0


def test_report_counts_only_finished_runs():
    hit = rerun_scene.summarize_scene(1, _played_scene(), None)
    miss = rerun_scene.summarize_scene(2, _played_scene(projection=None), None)
    paused = rerun_scene.summarize_scene(3, _played_scene(), None)
    paused.status = SceneStatus.PAUSED.value

    report = rerun_scene.render_report([hit, miss, paused], planned=5, header={"快照": "s"})

    assert "已跑 3 / 5 次，其中 2 次跑完" in report
    # 中断的那次即使已经出过投影，也不进分子分母：没跑完的场不能当样本
    assert "**出现公开投影：1 / 2 次**" in report
    assert "- 没有公开投影" in report and "动作：王冠已举至鬓边" in report


async def test_rerun_writes_after_every_run_and_stops_after_repeated_failures(tmp_path, monkeypatch):
    forked: list[str] = []
    outcomes = iter([SceneStatus.COMPLETED.value, SceneStatus.PAUSED.value, SceneStatus.PAUSED.value])

    async def fake_fork(project_id, snapshot_id, branch_name="", conditions=None, director_notes=""):
        scene = _played_scene()
        scene.scene_id, scene.project_id, scene.status = new_id(), project_id, SceneStatus.PENDING.value
        scene.dialogue_log, scene.speaker_mode = [], "selector"
        await repository.save_scene(scene)
        forked.append(branch_name)
        return None, scene

    async def fake_run(scene_id):
        scene = await repository.get_scene(scene_id)
        played = _played_scene()
        scene.dialogue_log, scene.llm_usage, scene.status = played.dialogue_log, played.llm_usage, next(outcomes)
        await repository.save_scene(scene)
        seen_modes.append(scene.speaker_mode)

    seen_modes: list[str] = []
    monkeypatch.setattr(orchestrator, "fork_from_snapshot", fake_fork)
    monkeypatch.setattr(orchestrator, "run_scene", fake_run)
    project = Project(name="复测")
    await repository.save_project(project)

    results = await rerun_scene.rerun(project.project_id, "snap", 5, tmp_path, speaker_mode="round_robin")

    # 第 2、3 次连续没跑完就停，不把剩下两次的额度烧掉
    assert [r.status for r in results] == ["completed", "paused", "paused"]
    assert len(forked) == 3 and len(set(forked)) == 3
    assert seen_modes == ["round_robin"] * 3
    saved = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert [r["index"] for r in saved] == [1, 2, 3]
    assert "出现公开投影：1 / 1 次" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.glob("run-*.md")) == ["run-01.md", "run-02.md", "run-03.md"]
