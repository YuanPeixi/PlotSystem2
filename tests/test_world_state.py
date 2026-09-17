"""工单07：分支级世界变量（WorldState）。

覆盖五条关键行为，每条都对应一个"不测就会静默退化"的点：

1. 合并优先级与**不写回** `Scene.initial_conditions`（偏离工单原文的 B2）；
2. 世界变量真的进了角色的 system prompt，且整场不变（契约3 补充条款）；
3. delta 的预算、淘汰与删除语义（C1，`unresolved_threads` 踩过的同一坑）；
4. 分叉继承与无副作用（I3 / I2），以及回滚丢弃本场 delta；
5. 后置快照的补写（B3）与老项目的降级路径。
"""

from __future__ import annotations

import json

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.agents.director_agent import (
    describe_world_state,
    merge_world_variables,
    normalize_world_delta,
)
from backend.config import settings
from backend.memory import MemoryManager
from backend.models import (
    MAX_WORLD_VARIABLES,
    CharacterCard,
    CharacterState,
    Project,
    Scene,
    SceneConfig,
    SceneEvaluation,
    SceneResult,
    WorldState,
)
from backend.scene_engine import SceneEngine
from backend.services import orchestrator, repository
from backend.snapshot import SnapshotManager


def _world_state_file(project_id: str, branch_id: str):
    return settings.project_dir(project_id) / "world_state" / f"{branch_id}.json"


# ---------------------------------------------------------------------------
# 1. 合并优先级 / 不写回
# ---------------------------------------------------------------------------


def _engine(scene: Scene, conditions: dict, world: dict) -> SceneEngine:
    config = SceneConfig(
        name="试炼",
        description="描述",
        location="王城",
        initial_conditions=dict(conditions),
    )
    return SceneEngine(scene, config, [], SnapshotManager(scene.project_id), world_variables=world)


def test_scene_conditions_override_world_variables():
    """场景局部覆盖全局默认（工单07 §3.3 指定的优先级）。"""
    scene = Scene(scene_id="s", project_id="p", branch_id="b")
    ctx = _engine(scene, {"天气": "晴"}, {"天气": "暴雨", "季节": "隆冬"})._scene_context()

    assert ctx["天气"] == "晴"   # 导演为这一场专门设的条件更贴近当下
    assert ctx["季节"] == "隆冬"  # 没被覆盖的世界变量照常生效


def test_merge_does_not_write_back_into_scene_initial_conditions():
    """B2 回归守卫：合并只发生在运行时上下文里。

    写回并落库会让分叉不变量 I5 把此刻的世界快照当成**场景局部条件**永久带下去，
    之后这条分支上的同名世界变量再也改不动了。
    """
    scene = Scene(scene_id="s", project_id="p", branch_id="b", initial_conditions={"天气": "晴"})
    engine = _engine(scene, scene.initial_conditions, {"季节": "隆冬"})

    engine._scene_context()
    engine._scene_context()  # 多跑一次，确认不是"只污染一次"

    assert scene.initial_conditions == {"天气": "晴"}


@pytest.mark.asyncio
async def test_run_scene_injects_world_and_keeps_scene_conditions_clean(monkeypatch):
    """端到端：run_scene 读分支世界变量喂给引擎，且不污染落库的场景条件。"""
    project_id = "proj-world-inject"
    await repository.save_project(Project(project_id=project_id, name="世界变量注入"))
    scene = Scene(
        scene_id="scene-world-inject",
        project_id=project_id,
        branch_id="branch-world",
        name="注入场景",
        initial_conditions={"天气": "晴"},
    )
    await repository.save_scene(scene)
    await repository.save_world_state(
        WorldState(project_id=project_id, branch_id="branch-world", variables={"季节": "隆冬"})
    )

    seen: dict = {}

    class FakeEngine:
        def __init__(self, scene_obj, config, agents, sm, world_variables=None):
            self.scene = scene_obj
            seen["world"] = dict(world_variables or {})
            seen["conditions"] = dict(config.initial_conditions)

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None, on_persist=None):
            self.scene.status = "completed"
            self.scene.snapshot_id_after = ""
            return SceneResult(
                scene_id=self.scene.scene_id,
                dialogue_log=[],
                snapshot_id_before="",
                snapshot_id_after="",
                turns_completed=0,
            )

    class FakeDirector:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate_scene(self, *args, **kwargs):
            seen["eval_world"] = dict(kwargs.get("world_state") or {})
            return SceneEvaluation(scene_id=scene.scene_id)

    async def fake_build_agents(pid, cids, states=None, branch_id=""):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    await orchestrator.run_scene(scene.scene_id)

    assert seen["world"] == {"季节": "隆冬"}
    assert seen["eval_world"] == {"季节": "隆冬"}  # 导演也要看得见，否则无从给 delta
    stored = await repository.get_scene(scene.scene_id)
    assert stored.initial_conditions == {"天气": "晴"}


# ---------------------------------------------------------------------------
# 2. 角色可见性与契约3
# ---------------------------------------------------------------------------


def _agent() -> CharacterAgent:
    card = CharacterCard(character_id="c1", project_id="p", name="甲", persona="人设")
    return CharacterAgent(card, MemoryManager("c1", "p", "b"))


def test_world_variables_reach_character_system_prompt():
    """旧实现只渲染 name/location/description/opening_narration 四个键，
    世界变量与初始条件只参与 lore 关键词匹配 —— "已入冬"写进了世界状态，
    角色却照旧在雪地里谈论酷暑。
    """
    scene = Scene(scene_id="s", project_id="p", branch_id="b")
    ctx = _engine(scene, {"天气": "暴雨"}, {"季节": "隆冬"})._scene_context()

    prompt = _agent().build_system_prompt(ctx)
    assert "季节：隆冬" in prompt
    assert "天气：暴雨" in prompt


def test_system_prompt_is_stable_within_a_scene():
    """契约3：世界变量是**场景常量**，开场即冻结，因此可以进 system。

    它一旦在场景内变化就会每轮击穿 prefix cache —— 场景内变量归工单20，
    那条通道必须走 user 消息。
    """
    scene = Scene(scene_id="s", project_id="p", branch_id="b")
    ctx = _engine(scene, {}, {"季节": "隆冬"})._scene_context()
    agent = _agent()

    assert agent.build_system_prompt(ctx) == agent.build_system_prompt(ctx)


def test_scene_brief_skips_blank_context_values():
    """空值不该在角色设定里留下一行"xxx："。"""
    scene = Scene(scene_id="s", project_id="p", branch_id="b")
    ctx = _engine(scene, {"空条目": "  "}, {})._scene_context()

    assert "空条目" not in _agent().build_system_prompt(ctx)


# ---------------------------------------------------------------------------
# 3. delta 的解析、预算与删除语义
# ---------------------------------------------------------------------------


def test_normalize_world_delta_rejects_non_dict():
    """LLM 把 world_state_delta 写成字符串/数组时不得当成一次真实更新。"""
    assert normalize_world_delta("敌对") == {}
    assert normalize_world_delta(["a", "b"]) == {}
    assert normalize_world_delta(None) == {}


def test_normalize_world_delta_keeps_none_as_deletion_marker():
    """None 是唯一的删除表达；空串同义。压成空串会让删除变成"改成空值"。"""
    delta = normalize_world_delta({"甲": None, "乙": "", "丙": "生效"})
    assert delta == {"甲": None, "乙": None, "丙": "生效"}


def test_normalize_world_delta_squashes_multiline_values():
    """值按"一行一条"渲染进 prompt，带换行的值会让一条变量看起来像两条。"""
    delta = normalize_world_delta({"战况": "北境失守\n南境仍在坚守"})
    assert "\n" not in delta["战况"]


def test_merge_applies_deletion_and_update():
    merged, dropped = merge_world_variables(
        {"季节": "盛夏", "敌军": "逼近"}, {"季节": "隆冬", "敌军": None}
    )
    assert merged == {"季节": "隆冬"}
    assert dropped == []


def test_merge_evicts_least_recently_updated_over_count_budget():
    """**边界才是被测行为本身**（CONVENTIONS §7.5）：必须真的把条数填过上限，
    否则用例照样绿，而线上第 31 条世界事实被静默吃掉。
    """
    current = {f"变量{i}": f"值{i}" for i in range(MAX_WORLD_VARIABLES)}
    merged, dropped = merge_world_variables(current, {"新事实": "刚发生"})

    assert len(merged) == MAX_WORLD_VARIABLES
    assert merged["新事实"] == "刚发生"       # 最新的一定留下
    assert dropped == ["变量0"]                # 淘汰最久未更新的那条
    assert "变量0" not in merged


def test_updating_an_existing_key_refreshes_its_recency():
    """更新等于"最近被确认过"，不该因为它的键很老就先被淘汰。"""
    current = {f"变量{i}": f"值{i}" for i in range(MAX_WORLD_VARIABLES)}
    merged, dropped = merge_world_variables(
        current, {"变量0": "刚刚更新", "新事实": "刚发生"}
    )

    assert merged["变量0"] == "刚刚更新"
    assert dropped == ["变量1"]


def test_merge_enforces_token_budget_not_just_count():
    """限条数不等于限预算：十几条超长变量同样能把每一轮的 prompt 撑爆。"""
    current = {f"变量{i}": "长" * 400 for i in range(10)}
    merged, dropped = merge_world_variables(current, {"新事实": "刚发生"})

    assert len(merged) < 11        # 远没到 30 条就已经被预算拦下
    assert "新事实" in merged
    assert dropped


def test_merge_keeps_at_least_the_newest_variable():
    """单条就超预算时也不能返回空 —— 那等于世界状态被清零。"""
    merged, _ = merge_world_variables({}, {"巨长": "长" * 5000})
    assert list(merged) == ["巨长"]


def test_describe_world_state_renders_one_line_per_variable():
    assert describe_world_state({}) == "（暂无世界层变量）"
    assert describe_world_state({"季节": "隆冬"}) == "- 季节：隆冬"


# ---------------------------------------------------------------------------
# 4. 分叉继承 / 回滚
# ---------------------------------------------------------------------------


async def _project_with_snapshot(project_id: str, branch_id: str, world: dict):
    await repository.save_project(Project(project_id=project_id, name="分叉世界"))
    await repository.save_character(
        CharacterCard(character_id="c1", project_id=project_id, name="甲")
    )
    await repository.save_world_state(
        WorldState(project_id=project_id, branch_id=branch_id, variables=dict(world))
    )
    sm = SnapshotManager(project_id)
    snap = await sm.create_snapshot(
        scene_id="scene-src",
        branch_id=branch_id,
        character_states={"c1": CharacterState(character_id="c1")},
        label="before:源场景",
        world_state_variables=dict(world),
    )
    scene = Scene(
        scene_id="scene-src",
        project_id=project_id,
        branch_id=branch_id,
        name="源场景",
        participating_characters=["c1"],
        snapshot_id_before=snap.snapshot_id,
        status="completed",
    )
    await repository.save_scene(scene)
    return sm, snap


@pytest.mark.asyncio
async def test_fork_inherits_world_state_without_touching_source():
    """I3：世界变量是分支级文件、不随快照目录走，不搬进来就是"一分叉世界重置"。
    I2：来源分支的文件必须逐字节不变。
    """
    project_id = "proj-world-fork"
    _sm, snap = await _project_with_snapshot(
        project_id, "branch-src", {"季节": "隆冬", "敌军": "逼近"}
    )
    src_before = _world_state_file(project_id, "branch-src").read_bytes()

    branch, _scene = await orchestrator.fork_from_snapshot(project_id, snap.snapshot_id, "IF 线")

    forked = await repository.get_world_state(project_id, branch.branch_id)
    assert forked.variables == {"季节": "隆冬", "敌军": "逼近"}
    assert _world_state_file(project_id, "branch-src").read_bytes() == src_before


@pytest.mark.asyncio
async def test_forked_branch_evolves_independently():
    """两条线各自演化，互不污染（与长期记忆的分支隔离同级）。"""
    project_id = "proj-world-isolated"
    _sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "隆冬"})
    branch, _ = await orchestrator.fork_from_snapshot(project_id, snap.snapshot_id, "IF 线")

    forked = await repository.get_world_state(project_id, branch.branch_id)
    forked.variables["季节"] = "开春"
    await repository.save_world_state(forked)

    source = await repository.get_world_state(project_id, "branch-src")
    assert source.variables == {"季节": "隆冬"}


@pytest.mark.asyncio
async def test_rollback_fork_drops_the_delta_of_the_rolled_back_scene():
    """回滚 = 条件为空的分叉：从**前置**快照分叉，新分支自然不带本场的 delta。

    这正是不走 restore_snapshot() 也能拿到回滚语义的原因。
    """
    project_id = "proj-world-rollback"
    _sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "隆冬"})

    # 本场演完，导演把世界改了
    live = await repository.get_world_state(project_id, "branch-src")
    live.variables["敌军"] = "已破城"
    await repository.save_world_state(live)

    branch, _ = await orchestrator.fork_from_snapshot(project_id, snap.snapshot_id, "回滚重演")

    replay = await repository.get_world_state(project_id, branch.branch_id)
    assert replay.variables == {"季节": "隆冬"}
    assert "敌军" not in replay.variables


# ---------------------------------------------------------------------------
# 5. 快照补写与降级路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_world_state_patches_after_snapshot():
    """B3：delta 出自评估，而后置快照在评估之前就打好了。不补写的话，
    从这个快照分叉出的分支会缺掉本场对世界的改动。
    """
    project_id = "proj-world-patch"
    sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "隆冬"})

    await sm.record_world_state(snap.snapshot_id, {"季节": "隆冬", "敌军": "已破城"})

    reloaded = await sm.get_snapshot(snap.snapshot_id)
    assert reloaded.world_state_variables == {"季节": "隆冬", "敌军": "已破城"}
    assert reloaded.created_at == snap.created_at  # 补写不得把快照重排到时间线末尾


@pytest.mark.asyncio
async def test_run_scene_applies_delta_and_patches_snapshot(monkeypatch):
    """端到端：评估产生的 delta 落到分支世界状态，并补写进后置快照。"""
    project_id = "proj-world-delta"
    sm, snap = await _project_with_snapshot(project_id, "branch-delta", {"季节": "隆冬"})
    scene = Scene(
        scene_id="scene-world-delta",
        project_id=project_id,
        branch_id="branch-delta",
        name="破城",
        participating_characters=["c1"],
    )
    await repository.save_scene(scene)

    class FakeEngine:
        def __init__(self, scene_obj, *args, **kwargs):
            self.scene = scene_obj

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None, on_persist=None):
            self.scene.status = "completed"
            self.scene.snapshot_id_after = snap.snapshot_id
            return SceneResult(
                scene_id=self.scene.scene_id,
                dialogue_log=[],
                snapshot_id_before="",
                snapshot_id_after=snap.snapshot_id,
                turns_completed=0,
            )

    class FakeDirector:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate_scene(self, *args, **kwargs):
            return SceneEvaluation(
                scene_id=scene.scene_id,
                world_state_delta={"敌军": "已破城", "季节": None},
            )

    async def fake_build_agents(pid, cids, states=None, branch_id=""):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    await orchestrator.run_scene(scene.scene_id)

    live = await repository.get_world_state(project_id, "branch-delta")
    assert live.variables == {"敌军": "已破城"}
    assert (await sm.get_snapshot(snap.snapshot_id)).world_state_variables == {"敌军": "已破城"}


@pytest.mark.asyncio
async def test_world_state_failure_keeps_scene_completed(monkeypatch):
    """世界变量是增量演化的附加层，它挂掉不得把场景退回 paused ——
    决策 CAS 只接 completed，退回了用户就再也无法对这场决策（与自动评估同一口径）。
    """
    project_id = "proj-world-failsafe"
    await repository.save_project(Project(project_id=project_id, name="世界更新失败"))
    scene = Scene(
        scene_id="scene-world-failsafe",
        project_id=project_id,
        branch_id="branch-main",
        name="场景",
    )
    await repository.save_scene(scene)

    class FakeEngine:
        def __init__(self, scene_obj, *args, **kwargs):
            self.scene = scene_obj

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None, on_persist=None):
            self.scene.status = "completed"
            self.scene.snapshot_id_after = "snap-missing"
            return SceneResult(
                scene_id=self.scene.scene_id,
                dialogue_log=[],
                snapshot_id_before="",
                snapshot_id_after="snap-missing",  # 快照不存在 → 补写必然失败
                turns_completed=0,
            )

    class FakeDirector:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate_scene(self, *args, **kwargs):
            return SceneEvaluation(scene_id=scene.scene_id, world_state_delta={"季节": "隆冬"})

    async def fake_build_agents(pid, cids, states=None, branch_id=""):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    await orchestrator.run_scene(scene.scene_id)

    assert (await repository.get_scene(scene.scene_id)).status == "completed"


@pytest.mark.asyncio
async def test_missing_world_state_file_degrades_to_empty():
    """老项目/老分支没有这个文件：返回空而不是报错，行为与本功能上线前一致。"""
    state = await repository.get_world_state("proj-never-existed", "branch-never-existed")
    assert state.variables == {}
    assert state.branch_id == "branch-never-existed"


@pytest.mark.asyncio
async def test_corrupted_world_state_file_degrades_to_empty():
    project_id = "proj-world-corrupt"
    await repository.save_world_state(
        WorldState(project_id=project_id, branch_id="b", variables={"季节": "隆冬"})
    )
    _world_state_file(project_id, "b").write_text("{不是 JSON", encoding="utf-8")

    assert (await repository.get_world_state(project_id, "b")).variables == {}


@pytest.mark.asyncio
async def test_save_world_state_requires_branch_id():
    """branch_id 为空会退化成项目级共享文件，两条 IF 线互相污染。"""
    with pytest.raises(ValueError):
        await repository.save_world_state(WorldState(project_id="p", branch_id=""))


@pytest.mark.asyncio
async def test_world_state_round_trip_coerces_values_to_str():
    """文件可被人工编辑：写进去的数字/布尔会原样进角色 prompt，下游按字符串拼接。"""
    project_id = "proj-world-coerce"
    path = _world_state_file(project_id, "b")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"variables": {"年份": 1205, "开战": True, "  ": "无名"}}),
        encoding="utf-8",
    )

    state = await repository.get_world_state(project_id, "b")
    assert state.variables == {"年份": "1205", "开战": "True"}
