"""工单07：分支级世界变量（WorldState）。

覆盖九条关键行为，每条都对应一个"不测就会静默退化"的点：

1. 合并优先级与**不写回** `Scene.initial_conditions`（偏离工单原文的 B2）；
2. 世界变量真的进了角色的 system prompt，且整场不变（契约3 补充条款）；
3. delta 的预算、淘汰与删除语义（C1，`unresolved_threads` 踩过的同一坑）；
4. 分叉继承与无副作用（I3 / I2），以及回滚丢弃本场 delta；
5. 后置快照的补写（B3）与老项目的降级路径；
6. 同分支并发下的读-改-写：后完成的那场不得抹掉先完成的那场的更新；
7. 保留字：世界变量只能补充场景上下文，不能改写场景固有字段；
8. 读取侧闸门：人工编辑的文件同样要受预算约束；
9. 评估/补写窗口内拒绝分叉：分叉只拷一次快照里的世界变量，补写完成前分叉
   会让新分支永久缺失本场对世界的改动。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.agents.director_agent import (
    describe_world_state,
    merge_world_variables,
    normalize_world_delta,
)
from backend.config import settings
from backend.exceptions import ConflictError
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
from backend.services.world_state import WORLD_BUDGET_TOKENS, WORLD_VALUE_TOKENS
from backend.snapshot import SnapshotManager
from backend.utils.llm import estimate_tokens


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

        async def run(self, on_turn=None, on_persist=None, on_after_snapshot=None):
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


def test_normalize_world_delta_squashes_multiline_keys():
    """**键**同样要塌单行：只做在值上是做不全的。"一行一条"是渲染出来的行的
    不变量，而键值同在 `f"- {k}：{v}"` 一行里 —— 键里留一个换行，就能在导演提示词
    与每个在场角色的 system prompt 里凭空多出一条看起来合法的世界变量。
    """
    delta = normalize_world_delta({"季节\n- 势力：敌对": "隆冬"})

    assert all("\n" not in k for k in delta)
    # 被测行为是渲染结果：一条 delta 只准渲染出一行
    assert len(describe_world_state(delta).splitlines()) == 1


def test_normalize_world_delta_warns_when_truncating(caplog):
    """超条数上限时不得静默丢：世界事实被吃掉不会报错，只表现为下一场角色
    忽然不知道某件事（与 `merge_world_variables` 的淘汰同一口径）。
    """
    oversized = {f"变量{i}": f"值{i}" for i in range(MAX_WORLD_VARIABLES + 5)}
    with caplog.at_level("WARNING"):
        delta = normalize_world_delta(oversized)

    assert len(delta) == MAX_WORLD_VARIABLES
    assert any("截断" in r.getMessage() for r in caplog.records)


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

        async def run(self, on_turn=None, on_persist=None, on_after_snapshot=None):
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

        async def run(self, on_turn=None, on_persist=None, on_after_snapshot=None):
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


# ---------------------------------------------------------------------------
# 6. 同分支并发：世界状态的读-改-写
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_world_delta_rereads_instead_of_using_the_opening_copy():
    """`_apply_world_delta` 必须自己重读，不得沿用 run_scene 开场读到的副本。

    那份副本与这里之间隔着整整一场 LLM，期间同分支上另一场完全可能已经跑完并
    更新过世界。拿旧副本合并再整份覆盖写，等于把对方的更新抹掉。
    """
    project_id = "proj-world-reread"
    sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "冬季"})
    scene_a = Scene(scene_id="scene-a", project_id=project_id, branch_id="branch-src")
    scene_b = Scene(scene_id="scene-b", project_id=project_id, branch_id="branch-src")

    # A 先完成，写下一条新的世界事实
    await orchestrator._apply_world_delta(
        scene_a,
        SceneEvaluation(scene_id="scene-a", world_state_delta={"城池": "已沦陷"}),
        snap.snapshot_id,
        sm,
    )
    # B 随后以空 delta 收尾 —— 它开场读到的世界里还没有"城池"
    await orchestrator._apply_world_delta(
        scene_b, SceneEvaluation(scene_id="scene-b"), snap.snapshot_id, sm
    )

    live = await repository.get_world_state(project_id, "branch-src")
    assert live.variables == {"季节": "冬季", "城池": "已沦陷"}


@pytest.mark.asyncio
async def test_concurrent_scenes_on_one_branch_do_not_lose_world_updates(monkeypatch):
    """同分支两场真并发：谁都不该把对方的世界更新吃掉。

    `_active_scenes` 只挡得住同一个场景被启动两次，同一分支上的两个不同场景照样
    可以并发跑完。**只给写加锁是不够的**：锁到了也只是把过时副本安全地写了进去，
    临界区必须从"重读"开始。
    """
    project_id = "proj-world-concurrent"
    sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "冬季"})
    real_get = repository.get_world_state

    async def yielding_get(project_id_: str, branch_id_: str):
        state = await real_get(project_id_, branch_id_)
        # 强行在读与写之间让出控制权：真实路径上这中间是 LLM 调用与文件 IO，
        # 不制造让出点的话单线程事件循环会把两次读写排成串行，并发窗口就测不出来。
        await asyncio.sleep(0)
        return state

    monkeypatch.setattr(repository, "get_world_state", yielding_get)

    await asyncio.gather(
        orchestrator._apply_world_delta(
            Scene(scene_id="scene-a", project_id=project_id, branch_id="branch-src"),
            SceneEvaluation(scene_id="scene-a", world_state_delta={"城池": "已沦陷"}),
            snap.snapshot_id,
            sm,
        ),
        orchestrator._apply_world_delta(
            Scene(scene_id="scene-b", project_id=project_id, branch_id="branch-src"),
            SceneEvaluation(scene_id="scene-b", world_state_delta={"粮草": "告罄"}),
            snap.snapshot_id,
            sm,
        ),
    )

    live = await real_get(project_id, "branch-src")
    assert live.variables == {"季节": "冬季", "城池": "已沦陷", "粮草": "告罄"}
    # 补写进快照的那份也必须是合并后的完整状态，否则从它分叉会带出一个中间态
    assert (await sm.get_snapshot(snap.snapshot_id)).world_state_variables == live.variables


@pytest.mark.asyncio
async def test_world_state_locks_are_per_branch():
    """锁按 (project_id, branch_id) 分桶：两条分支互不阻塞。"""
    assert orchestrator._world_state_lock("p", "b1") is orchestrator._world_state_lock("p", "b1")
    assert orchestrator._world_state_lock("p", "b1") is not orchestrator._world_state_lock("p", "b2")
    assert orchestrator._world_state_lock("p1", "b") is not orchestrator._world_state_lock("p2", "b")


# ---------------------------------------------------------------------------
# 7. 保留字：世界变量不得改写场景本身
# ---------------------------------------------------------------------------


def test_world_variables_cannot_shadow_scene_fields():
    """世界变量垫在 name/location/... 之下又摊在同一个 dict 里，同名就会顶掉本场设定。

    场景设在王城、世界里存着 `location=首都`，角色与导演就双双读到"地点：首都"——
    一条跨场次沿用的世界层默认值，改掉了导演为这一场明确指定的地点。
    """
    scene = Scene(scene_id="s", project_id="p", branch_id="b")
    engine = _engine(scene, {}, {"location": "首都", "name": "另一场", "季节": "隆冬"})
    ctx = engine._scene_context()

    assert ctx["location"] == "王城"   # 场景固有字段不可被世界变量改写
    assert ctx["name"] == "试炼"
    assert ctx["季节"] == "隆冬"        # 普通世界变量照常生效
    assert "首都" not in _agent().build_system_prompt(ctx)


def test_reserved_keys_never_enter_the_snapshot():
    """保留字在构造时就被摘掉，不进 `world_variables`，也就不会被快照带下去。"""
    scene = Scene(scene_id="s", project_id="p", branch_id="b")
    engine = _engine(scene, {}, {"description": "另一段描述", "季节": "隆冬"})

    assert engine.world_variables == {"季节": "隆冬"}


def test_normalize_world_delta_rejects_reserved_keys():
    """写入侧闸门：delta 会落进 evaluations 表并被快照复制，留着它就是一条假记录。"""
    delta = normalize_world_delta({"location": "首都", "季节": "隆冬"})
    assert delta == {"季节": "隆冬"}


def test_merge_evicts_reserved_keys_already_in_store():
    """库里已经存着保留字（本次修复之前写入的）也要清掉，并计入 dropped 以便 warning。"""
    merged, dropped = merge_world_variables({"opening_narration": "旧开场", "季节": "隆冬"}, None)
    assert merged == {"季节": "隆冬"}
    assert dropped == ["opening_narration"]


# ---------------------------------------------------------------------------
# 8. 读取侧闸门：人工编辑的文件同样要受预算约束
# ---------------------------------------------------------------------------


def _world_tokens(variables: dict[str, str]) -> int:
    return sum(estimate_tokens(f"- {k}：{v}") for k, v in variables.items())


@pytest.mark.asyncio
async def test_hand_edited_world_state_is_clamped_on_read():
    """`merge_world_variables` 只拦得住导演那条路径。文件摆在项目目录里、明确支持
    人工编辑，手写三百条变量会直接进**每一场、每个角色、每一轮**的 system prompt。
    """
    project_id = "proj-world-handedit"
    path = _world_state_file(project_id, "b")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"variables": {f"变量{i}": f"值{i}" for i in range(300)}}),
        encoding="utf-8",
    )

    variables = (await repository.get_world_state(project_id, "b")).variables
    assert len(variables) <= MAX_WORLD_VARIABLES
    assert _world_tokens(variables) <= WORLD_BUDGET_TOKENS


@pytest.mark.asyncio
async def test_hand_edited_oversized_value_is_clamped_on_read():
    """单条超长同样致命：30 条的上限拦不住一条五千字的变量。"""
    project_id = "proj-world-hugevalue"
    path = _world_state_file(project_id, "b")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"variables": {"战况": "长" * 5000}}), encoding="utf-8"
    )

    variables = (await repository.get_world_state(project_id, "b")).variables
    assert estimate_tokens(variables["战况"]) <= WORLD_VALUE_TOKENS


@pytest.mark.asyncio
async def test_hand_edited_multiline_value_is_squashed_on_read():
    """世界变量按"一行一条"渲染，带换行的值会让同一条变量看起来像两条
    （与 episodic 条目的单行不变量同一道理）。
    """
    project_id = "proj-world-multiline"
    path = _world_state_file(project_id, "b")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"variables": {"战况": "北境失守\n南境仍在坚守"}}), encoding="utf-8"
    )

    assert "\n" not in (await repository.get_world_state(project_id, "b")).variables["战况"]


@pytest.mark.asyncio
async def test_hand_edited_multiline_key_is_squashed_on_read():
    """读取侧同样要收键的形状：手写一个带换行的键，等于在每个角色的 system prompt
    里凭空插一行伪造的世界变量，且绕过了写入侧那道闸门。
    """
    project_id = "proj-world-multiline-key"
    path = _world_state_file(project_id, "b")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"variables": {"季节\n- 势力：敌对": "隆冬"}}), encoding="utf-8"
    )

    variables = (await repository.get_world_state(project_id, "b")).variables
    assert len(describe_world_state(variables).splitlines()) == 1


@pytest.mark.asyncio
async def test_hand_edited_reserved_key_is_ignored_on_read():
    project_id = "proj-world-handreserved"
    path = _world_state_file(project_id, "b")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"variables": {"location": "首都", "季节": "隆冬"}}), encoding="utf-8"
    )

    assert (await repository.get_world_state(project_id, "b")).variables == {"季节": "隆冬"}


@pytest.mark.asyncio
async def test_clamping_does_not_rewrite_the_file():
    """只压不写回：这是读路径，不该因为一次读取就改掉用户手编的文件。
    下一次合并落盘时超限的内容自然收敛。
    """
    project_id = "proj-world-noclobber"
    path = _world_state_file(project_id, "b")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"variables": {f"变量{i}": f"值{i}" for i in range(300)}}),
        encoding="utf-8",
    )
    before = path.read_bytes()

    await repository.get_world_state(project_id, "b")

    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# 9. 评估/补写窗口内拒绝分叉（评审修复）
# ---------------------------------------------------------------------------
#
# `fork_from_snapshot` 只在分叉那一刻读一次 `snap.world_state_variables` 并整份
# 拷进新分支文件，之后 `record_world_state` 才补写完成的本场 delta 不会再传播过去。
# 若允许在"后置快照已存在、评估还没跑完"的窗口内分叉，新分支就会**永久**缺失本场
# 对世界的改动，且不像超预算淘汰那样有 warning 可查。


@pytest.mark.asyncio
async def test_fork_rejected_while_snapshot_pending_world_patch():
    """直接操纵守卫标记：不依赖真实评估耗时，验证 fork 入口本身的拦截逻辑。"""
    project_id = "proj-world-pending-fork"
    sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "隆冬"})

    orchestrator._pending_world_patch.add(snap.snapshot_id)
    try:
        with pytest.raises(ConflictError):
            await orchestrator.fork_from_snapshot(project_id, snap.snapshot_id, "过早分叉")
    finally:
        orchestrator._pending_world_patch.discard(snap.snapshot_id)

    # 标记摘除后，同一份快照恢复可分叉
    branch, _scene = await orchestrator.fork_from_snapshot(project_id, snap.snapshot_id, "补写完成后")
    assert branch is not None


@pytest.mark.asyncio
async def test_pending_mark_is_set_before_snapshot_visible_and_cleared_on_success(monkeypatch):
    """端到端：`run_scene` 必须把守卫回调接到引擎上 —— 引擎在后置快照落库**之前**
    回调，标记就必须已经生效（快照一进表就对 fork 可见，不等 scene 落库、也不等
    run() 返回）；评估与世界状态补写都完成后，标记必须被摘除，分叉才重新放行。
    """
    project_id = "proj-world-pending-e2e"
    sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "隆冬"})
    scene = Scene(
        scene_id="scene-pending",
        project_id=project_id,
        branch_id="branch-src",
        name="场景",
        participating_characters=["c1"],
    )
    await repository.save_scene(scene)

    observed: dict = {}

    class FakeEngine:
        def __init__(self, scene_obj, *args, **kwargs):
            self.scene = scene_obj

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None, on_persist=None, on_after_snapshot=None):
            # 真实引擎在此处才创建后置快照：回调返回后快照立即可见，
            # 因此标记必须在回调内就生效。
            assert on_after_snapshot is not None, "run_scene 必须接上守卫回调"
            on_after_snapshot(snap.snapshot_id)
            observed["pending_at_snapshot"] = orchestrator.is_snapshot_pending_world_patch(
                snap.snapshot_id
            )
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
            # 评估调用发生时，post-snapshot 已经落库，标记必须仍然挂着。
            observed["pending_during_eval"] = orchestrator.is_snapshot_pending_world_patch(
                snap.snapshot_id
            )
            return SceneEvaluation(scene_id=scene.scene_id, world_state_delta={"敌军": "已破城"})

    async def fake_build_agents(pid, cids, states=None, branch_id=""):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    await orchestrator.run_scene(scene.scene_id)

    assert observed["pending_at_snapshot"] is True
    assert observed["pending_during_eval"] is True
    assert not orchestrator.is_snapshot_pending_world_patch(snap.snapshot_id)
    # 摘除后分叉必须放行，且能看到补写完成的世界变量。
    branch, _new_scene = await orchestrator.fork_from_snapshot(
        project_id, snap.snapshot_id, "补写完成后"
    )
    forked = await repository.get_world_state(project_id, branch.branch_id)
    assert forked.variables == {"季节": "隆冬", "敌军": "已破城"}


@pytest.mark.asyncio
async def test_pending_mark_is_cleared_when_engine_itself_fails(monkeypatch):
    """引擎打完后置快照后自己抛错：评估永远不会发起，补写也就永远不会发生。
    若守卫只在评估的 try/finally 里摘除，这份快照将永久不可分叉。
    """
    project_id = "proj-world-pending-engine-fail"
    sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "隆冬"})
    scene = Scene(
        scene_id="scene-pending-engine-fail",
        project_id=project_id,
        branch_id="branch-src",
        name="场景",
        participating_characters=["c1"],
    )
    await repository.save_scene(scene)

    class FakeEngine:
        def __init__(self, scene_obj, *args, **kwargs):
            self.scene = scene_obj

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None, on_persist=None, on_after_snapshot=None):
            on_after_snapshot(snap.snapshot_id)
            raise RuntimeError("快照之后炸了")

    async def fake_build_agents(pid, cids, states=None, branch_id=""):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)

    await orchestrator.run_scene(scene.scene_id)

    assert not orchestrator.is_snapshot_pending_world_patch(snap.snapshot_id)
    branch, _new_scene = await orchestrator.fork_from_snapshot(
        project_id, snap.snapshot_id, "引擎失败后仍可分叉"
    )
    assert branch is not None


@pytest.mark.asyncio
async def test_pending_mark_is_cleared_even_when_evaluation_fails(monkeypatch):
    """评估本身失败（LLM 报错）时，delta 永远不会再补写 —— 无限期挡着分叉才是
    真正的"漏掉本场世界变化"：这里只挡"补写还没完成"的窗口，不挡"补写已确定
    不会再发生"的终态。
    """
    project_id = "proj-world-pending-failure"
    sm, snap = await _project_with_snapshot(project_id, "branch-src", {"季节": "隆冬"})
    scene = Scene(
        scene_id="scene-pending-fail",
        project_id=project_id,
        branch_id="branch-src",
        name="场景",
        participating_characters=["c1"],
    )
    await repository.save_scene(scene)

    class FakeEngine:
        def __init__(self, scene_obj, *args, **kwargs):
            self.scene = scene_obj

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None, on_persist=None, on_after_snapshot=None):
            on_after_snapshot(snap.snapshot_id)
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
            raise RuntimeError("LLM 挂了")

    async def fake_build_agents(pid, cids, states=None, branch_id=""):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)

    await orchestrator.run_scene(scene.scene_id)

    assert not orchestrator.is_snapshot_pending_world_patch(snap.snapshot_id)
    branch, _new_scene = await orchestrator.fork_from_snapshot(
        project_id, snap.snapshot_id, "评估失败后仍可分叉"
    )
    assert branch is not None
