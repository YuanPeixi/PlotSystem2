"""AutoPilot（工单12）：自动决策的连锁、停止条件与幂等。

引擎与导演的 LLM 部分全部打桩，但 make_decision 用真实的规则化实现 ——
停止条件（尤其是回滚上限）判的就是它给出的决策类型。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agents.director_agent import DirectorAgent
from backend.exceptions import ConflictError, InvalidRequestError
from backend.models import (
    AutoPilotSession,
    DirectorDecision,
    Project,
    Scene,
    SceneConfig,
    SceneEvaluation,
    SceneResult,
)
from backend.scene_engine.termination import INTERRUPTED_REASON
from backend.services import autopilot, events, orchestrator, repository


def _eval(scene_id: str, **overrides) -> SceneEvaluation:
    """一份各项都及格、导演建议下一场的评估。"""
    data = dict(
        scene_id=scene_id,
        narrative_goal_score=8,
        dramatic_tension_score=8,
        plot_deviation_score=1,
        character_consistency_score=8,
        recommended_decision="next_scene",
    )
    data.update(overrides)
    return SceneEvaluation(**data)


class _Harness:
    """打桩 run_scene 依赖的引擎/导演，并记录每一场被跑了几次。"""

    def __init__(self, monkeypatch, evaluate=None, terminated_reason="达到最大轮次"):
        self.runs: list[str] = []
        self.gate: asyncio.Event | None = None
        harness = self
        evaluate = evaluate or (lambda scene: _eval(scene.scene_id))

        class FakeEngine:
            def __init__(self, scene_obj, config, agents, sm, **kwargs):
                self.scene = scene_obj
                self.sm = sm

            def inject_history(self, *args, **kwargs):
                pass

            async def run(self, on_turn=None, on_persist=None, on_after_snapshot=None):
                harness.runs.append(self.scene.scene_id)
                if harness.gate is not None:
                    await harness.gate.wait()
                # 回滚要从真实的前置快照分叉，所以这里建真快照
                if not self.scene.snapshot_id_before:
                    before = await self.sm.create_snapshot(
                        self.scene.scene_id, self.scene.branch_id, {}, label="before"
                    )
                    self.scene.snapshot_id_before = before.snapshot_id
                after = await self.sm.create_snapshot(
                    self.scene.scene_id, self.scene.branch_id, {}, label="after"
                )
                self.scene.snapshot_id_after = after.snapshot_id
                self.scene.turns_completed = self.scene.max_turns
                self.scene.status = "completed"
                return SceneResult(
                    scene_id=self.scene.scene_id,
                    dialogue_log=[],
                    snapshot_id_before=self.scene.snapshot_id_before,
                    snapshot_id_after=after.snapshot_id,
                    turns_completed=self.scene.turns_completed,
                    terminated_reason=terminated_reason,
                )

        class FakeDirector:
            def __init__(self, *args, **kwargs):
                pass

            async def evaluate_scene(self, scene, *args, **kwargs):
                return evaluate(scene)

            # 真实的规则化决策：不调 LLM
            make_decision = DirectorAgent.make_decision

            async def plan_scene(self, branch_id, narrative_goal, cards, **kwargs):
                return SceneConfig(name="下一场", description="自动规划", max_turns=2)

        async def fake_build_agents(pid, cids, states=None, branch_id=""):
            return []

        monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
        monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
        monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)


async def _setup(project_id: str, status: str = "pending") -> Scene:
    await repository.save_project(
        Project(project_id=project_id, name=project_id, narrative_goal="夺回王位")
    )
    scene = Scene(
        scene_id=f"{project_id}-s1",
        project_id=project_id,
        branch_id="branch-main",
        name="第一场",
        max_turns=2,
        status=status,
    )
    await repository.save_scene(scene)
    return scene


async def _wait_stopped(project_id: str, timeout: float = 5.0) -> AutoPilotSession:
    async def _poll():
        while autopilot.is_running(orchestrator.get_autopilot(project_id)):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)
    # 停止后仍可能有收尾的任务（终态帧发布），让它们跑完再断言
    for _ in range(5):
        await asyncio.sleep(0.01)
    return orchestrator.get_autopilot(project_id)


@pytest.mark.asyncio
async def test_chains_next_scenes_until_step_limit(monkeypatch):
    h = _Harness(monkeypatch)
    scene = await _setup("proj-ap-chain")

    await orchestrator.start_autopilot(
        scene.project_id, scene.scene_id, request_id="r1", max_steps=2
    )
    session = await _wait_stopped(scene.project_id)

    assert session.stop_reason == autopilot.MAX_STEPS
    assert session.steps_taken == 2
    # 起点 + 两次自动决策产生的两场，每场都真的跑了
    assert len(h.runs) == 3
    assert len(set(h.runs)) == 3
    # 决策留痕为自动
    first = await repository.get_decision(scene.scene_id)
    assert first.source == "auto"
    assert first.next_scene_id == session.steps[0].next_scene_id
    # 最后一场跑完、评估完，等人来决策
    last = await repository.get_scene(session.current_scene_id)
    assert last.status == "completed"
    assert await repository.get_decision(last.scene_id) is None


@pytest.mark.asyncio
async def test_continue_reruns_same_scene(monkeypatch):
    """continue 是对同一个 scene_id 再起 run_scene：决策若发生在运行锁释放之前，
    新任务会被重复启动守卫静默丢掉。"""
    h = _Harness(monkeypatch, evaluate=lambda s: _eval(s.scene_id, dramatic_tension_score=1))
    scene = await _setup("proj-ap-continue")

    await orchestrator.start_autopilot(
        scene.project_id, scene.scene_id, request_id="r1", max_steps=2
    )
    session = await _wait_stopped(scene.project_id)

    assert session.stop_reason == autopilot.MAX_STEPS
    assert h.runs == [scene.scene_id] * 3
    assert [s.decision_type for s in session.steps] == ["continue", "continue"]
    stored = await repository.get_scene(scene.scene_id)
    assert stored.status == "completed"


@pytest.mark.asyncio
async def test_rollback_limit_stops_before_creating_branch(monkeypatch):
    h = _Harness(monkeypatch, evaluate=lambda s: _eval(s.scene_id, narrative_goal_score=2))
    scene = await _setup("proj-ap-rollback")

    await orchestrator.start_autopilot(
        scene.project_id, scene.scene_id, request_id="r1",
        max_steps=5, max_consecutive_rollbacks=1,
    )
    session = await _wait_stopped(scene.project_id)

    assert session.stop_reason == autopilot.ROLLBACK_LIMIT
    assert session.steps_taken == 1
    assert session.steps[0].decision_type == "rollback"
    # 只执行了一次回滚：多出恰好一条分支；第二次在执行前就拦下了
    assert session.steps[0].next_branch_id != "branch-main"
    all_scenes = await repository.list_scenes(scene.project_id)
    assert len({s.branch_id for s in all_scenes}) == 2
    assert len(h.runs) == 2
    assert await repository.get_decision(session.current_scene_id) is None


def test_rollback_counter_resets_after_other_decision():
    session = AutoPilotSession(max_steps=10, max_consecutive_rollbacks=2)
    rollback = DirectorDecision(decision_type="rollback", next_scene_id="x")
    nxt = DirectorDecision(decision_type="next_scene", next_scene_id="y")

    autopilot.record_step(session, "a", rollback, "b1")
    autopilot.record_step(session, "x", rollback, "b2")
    assert autopilot.stop_reason_for_decision(session, rollback) == autopilot.ROLLBACK_LIMIT
    autopilot.record_step(session, "x", nxt, "b2")
    assert session.consecutive_rollbacks == 0
    assert autopilot.stop_reason_for_decision(session, rollback) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"terminated_reason": INTERRUPTED_REASON}, autopilot.INTERRUPTED),
        ({"evaluate": lambda s: _eval(s.scene_id, is_ending_reached=True)}, autopilot.ENDING_REACHED),
        (
            {"evaluate": lambda s: _eval(s.scene_id, narrative_goal_score=-1)},
            autopilot.EVALUATION_UNAVAILABLE,
        ),
    ],
)
async def test_stop_conditions_hand_back_without_deciding(monkeypatch, kwargs, reason):
    h = _Harness(monkeypatch, **kwargs)
    scene = await _setup(f"proj-ap-stop-{reason}")

    await orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r1")
    session = await _wait_stopped(scene.project_id)

    assert session.stop_reason == reason
    assert session.steps_taken == 0
    assert h.runs == [scene.scene_id]
    assert await repository.get_decision(scene.scene_id) is None


@pytest.mark.asyncio
async def test_evaluation_failure_stops(monkeypatch):
    def boom(scene):
        raise RuntimeError("评估模型挂了")

    _Harness(monkeypatch, evaluate=boom)
    scene = await _setup("proj-ap-eval-fail")

    await orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r1")
    session = await _wait_stopped(scene.project_id)

    assert session.stop_reason == autopilot.EVALUATION_UNAVAILABLE
    assert (await repository.get_scene(scene.scene_id)).status == "completed"


@pytest.mark.asyncio
async def test_start_is_idempotent_and_exclusive(monkeypatch):
    h = _Harness(monkeypatch)
    h.gate = asyncio.Event()
    scene = await _setup("proj-ap-idem")

    first = await orchestrator.start_autopilot(
        scene.project_id, scene.scene_id, request_id="r1", max_steps=1
    )
    replay = await orchestrator.start_autopilot(
        scene.project_id, scene.scene_id, request_id="r1", max_steps=1
    )
    assert replay is first
    with pytest.raises(ConflictError):
        await orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r2")

    h.gate.set()
    session = await _wait_stopped(scene.project_id)
    # 重放没有重复开演
    assert h.runs.count(scene.scene_id) == 1
    assert session.stop_reason == autopilot.MAX_STEPS


@pytest.mark.asyncio
async def test_concurrent_starts_with_same_request_id_share_one_session(monkeypatch):
    """检查已有会话与登记新会话之间隔着 await（读项目、读场景）：不加锁的话两个并发
    重放都会通过检查，各开一个会话、各起一次开演。"""
    h = _Harness(monkeypatch)
    h.gate = asyncio.Event()
    scene = await _setup("proj-ap-race-same")

    first, second = await asyncio.gather(
        orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r1", max_steps=1),
        orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r1", max_steps=1),
    )
    assert first is second

    h.gate.set()
    await _wait_stopped(scene.project_id)
    assert h.runs.count(scene.scene_id) == 1


@pytest.mark.asyncio
async def test_concurrent_starts_on_different_scenes_admit_only_one(monkeypatch):
    """不同起点并发开启：只能有一个成功。否则两场都开演，项目只记得后登记的那个会话，
    另一场跑完没人接手，脱离 AutoPilot 管理。"""
    h = _Harness(monkeypatch)
    h.gate = asyncio.Event()
    scene = await _setup("proj-ap-race-diff")
    other = Scene(
        scene_id=f"{scene.project_id}-s2", project_id=scene.project_id,
        branch_id="branch-main", name="另一场", max_turns=2,
    )
    await repository.save_scene(other)

    results = await asyncio.gather(
        orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r1"),
        orchestrator.start_autopilot(scene.project_id, other.scene_id, request_id="r2"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ConflictError) for r in results) == 1
    winner = next(r for r in results if isinstance(r, AutoPilotSession))
    assert orchestrator.get_autopilot(scene.project_id) is winner

    await asyncio.sleep(0.05)
    # 只有会话所在的那一场被开演
    assert h.runs == [winner.current_scene_id]
    await orchestrator.stop_autopilot(scene.project_id)
    h.gate.set()
    # 等开演的那一场连同尾部任务跑完，别让它越过本测试的事件循环
    async def _drain():
        while orchestrator._background_tasks or orchestrator.is_scene_active(winner.current_scene_id):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_drain(), 5)


@pytest.mark.asyncio
async def test_user_stop_takes_effect_before_next_decision(monkeypatch):
    h = _Harness(monkeypatch)
    h.gate = asyncio.Event()
    scene = await _setup("proj-ap-user-stop")

    await orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r1")
    await asyncio.sleep(0.05)  # 让这一场开跑并卡在 gate 上
    stopped = await orchestrator.stop_autopilot(scene.project_id)
    assert stopped.stop_reason == autopilot.USER_STOPPED

    h.gate.set()
    for _ in range(20):
        await asyncio.sleep(0.01)
    # 这一场照常跑完，但不再往下接
    assert (await repository.get_scene(scene.scene_id)).status == "completed"
    assert await repository.get_decision(scene.scene_id) is None
    assert h.runs == [scene.scene_id]


@pytest.mark.asyncio
async def test_stop_during_startup_is_not_lost(monkeypatch):
    """开启在登记会话前要过好几次 await：停止若不拿同一把锁，会读到"还没有会话"
    就返回，随后会话照样登记、往下接。"""
    h = _Harness(monkeypatch)
    h.gate = asyncio.Event()
    scene = await _setup("proj-ap-stop-startup")

    entered = asyncio.Event()
    release = asyncio.Event()
    real_get_scene = repository.get_scene

    async def slow_get_scene(scene_id):
        if not release.is_set():
            entered.set()
            await release.wait()
        return await real_get_scene(scene_id)

    monkeypatch.setattr(repository, "get_scene", slow_get_scene)
    start = asyncio.create_task(
        orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r1")
    )
    await asyncio.wait_for(entered.wait(), 5)  # 开启已持锁、卡在登记之前
    stop = asyncio.create_task(orchestrator.stop_autopilot(scene.project_id))
    await asyncio.sleep(0.02)
    assert not stop.done()  # 停止在等开启的锁，而不是读到旧值直接返回

    release.set()
    await start
    stopped = await stop
    assert stopped is not None
    assert stopped.stop_reason == autopilot.USER_STOPPED

    # 已开演的起点照常演完，但不再往下接
    h.gate.set()
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert await repository.get_decision(scene.scene_id) is None
    assert h.runs == [scene.scene_id]


@pytest.mark.asyncio
async def test_stop_while_deciding_continue_does_not_rerun(monkeypatch):
    """continue 的续跑若由 apply_decision 自己起，决策期间的停止事后拦不住，
    会白烧一整轮。停止后决策照常生效（场景加长轮次、回到 pending），但不开演。"""
    h = _Harness(monkeypatch, evaluate=lambda s: _eval(s.scene_id, dramatic_tension_score=1))
    scene = await _setup("proj-ap-stop-continue")

    real_save_scene = repository.save_scene

    async def save_and_stop(obj):
        await real_save_scene(obj)
        # continue 分支把场景改回 pending 落库的那一刻：决策正在执行
        if obj.scene_id == scene.scene_id and obj.status == "pending" and h.runs:
            await orchestrator.stop_autopilot(scene.project_id)

    monkeypatch.setattr(repository, "save_scene", save_and_stop)
    q = events.subscribe(scene.scene_id)
    try:
        await orchestrator.start_autopilot(
            scene.project_id, scene.scene_id, request_id="r1", max_steps=3
        )
        session = await _wait_stopped(scene.project_id)

        # 停止发生在决策中途：等决策的后半段（记账、终态帧）跑完再断言
        async def _drain():
            while orchestrator._background_tasks:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_drain(), 5)
        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
    finally:
        events.unsubscribe(scene.scene_id, q)

    assert session.stop_reason == autopilot.USER_STOPPED
    assert [s.decision_type for s in session.steps] == ["continue"]
    assert h.runs == [scene.scene_id]
    stored = await repository.get_scene(scene.scene_id)
    assert stored.status == "pending"
    assert stored.max_turns > stored.turns_completed
    # 没有续跑就要照常推终态帧，否则前端的流一直挂在"模拟中"
    assert any(e["event"] == "status" and e["data"]["status"] == "completed" for e in seen)


@pytest.mark.asyncio
async def test_start_on_completed_scene_decides_immediately(monkeypatch):
    h = _Harness(monkeypatch)
    scene = await _setup("proj-ap-completed", status="completed")
    await repository.save_evaluation(_eval(scene.scene_id))

    await orchestrator.start_autopilot(
        scene.project_id, scene.scene_id, request_id="r1", max_steps=1
    )
    session = await _wait_stopped(scene.project_id)

    assert session.steps_taken == 1
    # 起点不重跑，只跑决策产生的下一场
    assert h.runs == [session.current_scene_id]


@pytest.mark.asyncio
async def test_start_rejects_decided_or_unevaluated_scene(monkeypatch):
    _Harness(monkeypatch)
    scene = await _setup("proj-ap-reject", status="completed")

    # 没有评估：一开就会停，当场拒绝
    with pytest.raises(InvalidRequestError):
        await orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r1")

    await repository.save_evaluation(_eval(scene.scene_id))
    await repository.save_decision(
        scene.scene_id, DirectorDecision(decision_type="next_scene", next_scene_id="other")
    )
    with pytest.raises(InvalidRequestError):
        await orchestrator.start_autopilot(scene.project_id, scene.scene_id, request_id="r2")
    assert not autopilot.is_running(orchestrator.get_autopilot(scene.project_id))


@pytest.mark.asyncio
async def test_autopilot_event_precedes_terminal_status(monkeypatch):
    """前端的流收到 completed 就关了，下一场是哪个必须在那之前告诉它。"""
    _Harness(monkeypatch)
    scene = await _setup("proj-ap-events")
    q = events.subscribe(scene.scene_id)
    try:
        await orchestrator.start_autopilot(
            scene.project_id, scene.scene_id, request_id="r1", max_steps=1
        )
        await _wait_stopped(scene.project_id)
        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
    finally:
        events.unsubscribe(scene.scene_id, q)

    decided = next(
        i for i, e in enumerate(seen)
        if e["event"] == "autopilot" and e["data"]["steps_taken"] == 1
    )
    completed = next(
        i for i, e in enumerate(seen)
        if e["event"] == "status" and e["data"]["status"] == "completed"
    )
    assert decided < completed
    assert seen[decided]["data"]["current_scene_id"] != scene.scene_id
