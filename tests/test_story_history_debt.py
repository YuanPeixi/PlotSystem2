"""工单18 §3.0：导演历史的三笔技术债（D1 类型化 / D2 N+1 / D3 快照列表）。

D0 的等价回归先于重构写成、在旧实现上跑绿后才开始改代码：期望值是旧实现的
实际输出，不是按新实现推出来的。谱系里刻意同时包含分叉、continue、手建场景、
目标版本切换与旧格式副本 —— 任何一条路径的回溯规则被改动都会在这里变红。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from backend.agents import CharacterAgent, DirectorAgent
from backend.memory import MemoryManager
from backend.models import (
    CharacterCard,
    CharacterState,
    DialogueTurn,
    DirectorDecision,
    Project,
    Scene,
    SceneEvaluation,
    StoryRecord,
    goal_revision,
)
from backend.services import orchestrator, repository
from backend.snapshot import SnapshotManager
from backend.snapshot.snapshot_manager import _snapshots_dir
from backend.utils import db

GOAL = "揭露叛徒"
REV = goal_revision(GOAL)

# 场景名 → (推进度, 线索, 梗概, 目标版本)。续跑前改写其中一项即可让同一场产出新评估。
OUTCOMES: dict[str, tuple[float, list[str], str, str]] = {}


def _suppress_rerun(monkeypatch) -> None:
    real_create_task = asyncio.create_task

    def fake(coro, *args, **kwargs):
        coro.close()
        return real_create_task(asyncio.sleep(0))

    monkeypatch.setattr(asyncio, "create_task", fake)


async def _insert_raw_scene(scene_id: str, project_id: str, branch_id: str, data: dict) -> None:
    """绕过 save_scene 写一行旧格式场景：data_json 原样落库，不经过任何类型转换。"""
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO scenes "
            "(scene_id, project_id, branch_id, parent_scene_id, name, status, created_at, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scene_id,
                project_id,
                branch_id,
                data.get("parent_scene_id"),
                data.get("name", ""),
                data.get("status", "pending"),
                "2099-01-01T00:00:00+00:00",
                json.dumps(data, ensure_ascii=False),
            ),
        )
        await conn.commit()


@pytest.fixture
async def lineage(monkeypatch, request):
    """跑出一张含分叉、continue、手建场景的真实谱系，返回各场景。"""
    pid = request.node.name
    await repository.save_project(Project(project_id=pid, name=pid, narrative_goal=GOAL))
    await repository.save_character(CharacterCard(project_id=pid, character_id="c", name="甲"))
    monkeypatch.setattr(MemoryManager, "connect", AsyncMock())
    monkeypatch.setattr(MemoryManager, "add_experience", AsyncMock())
    monkeypatch.setattr(MemoryManager, "consolidate", AsyncMock())
    monkeypatch.setattr(CharacterAgent, "respond", AsyncMock(return_value="正在调查。"))

    OUTCOMES.clear()
    OUTCOMES.update({
        "A": (0.2, ["T1"], "sA", REV),
        "B": (0.5, ["T1", "T2"], "sB", REV),
        # 手建场景的推进度对照的是旧目标：不得参与继承
        "M": (0.95, [], "sM", goal_revision("旧目标")),
        "B（IF）": (0.3, ["T3"], "sIF", REV),
    })

    async def evaluate(self, scene, *args, **kwargs):
        progress, threads, synopsis, rev = OUTCOMES[scene.name]
        return SceneEvaluation(
            scene_id=scene.scene_id,
            story_progress=progress,
            goal_revision=rev,
            synopsis=synopsis,
            unresolved_threads=list(threads),
        )

    monkeypatch.setattr(DirectorAgent, "evaluate_scene", evaluate)

    async def run(scene: Scene) -> Scene:
        await repository.save_scene(scene)
        await orchestrator.run_scene(scene.scene_id)
        return await repository.get_scene(scene.scene_id)

    main = "main"
    a = await run(Scene(project_id=pid, branch_id=main, name="A",
                        participating_characters=["c"], max_turns=1))
    b = await run(Scene(project_id=pid, branch_id=main, name="B", parent_scene_id=a.scene_id,
                        participating_characters=["c"], max_turns=1))
    # 手建场景：没有 parent_scene_id，只能按本分支创建顺序回溯前情
    m = await run(Scene(project_id=pid, branch_id=main, name="M",
                        participating_characters=["c"], max_turns=1))

    # continue 续跑 B：同一场产出新评估与新的后置快照
    _suppress_rerun(monkeypatch)
    OUTCOMES["B"] = (0.7, ["T2"], "sB2", REV)
    await orchestrator.apply_decision(
        b.scene_id, DirectorDecision(decision_type="continue", extra_turns=1)
    )
    b = await run(await repository.get_scene(b.scene_id))

    # 从续跑后的结束态分叉（不开跑）
    _, after_fork = await orchestrator.fork_from_snapshot(pid, b.snapshot_id_after, "后置分叉")
    # 从 B 的前置快照分叉并开跑，再在新分支上手建一场下一场（不开跑）
    _, if0 = await orchestrator.fork_from_snapshot(pid, b.snapshot_id_before, "IF")
    if0 = await run(if0)
    if1 = Scene(project_id=pid, branch_id=if0.branch_id, parent_scene_id=if0.scene_id, name="IF1")
    await repository.save_scene(if1)

    # 旧格式场景：副本里最近一条缺 unresolved_threads，再上一条带线索
    legacy_id = f"{pid}-legacy"
    await _insert_raw_scene(legacy_id, pid, "legacy", {
        "scene_id": legacy_id,
        "project_id": pid,
        "branch_id": "legacy",
        "name": "L",
        "inherited_story_history": [
            {"scene_id": "old-a", "name": "旧A",
             "evaluation": {"story_progress": 0.2, "goal_revision": REV,
                            "unresolved_threads": ["旧线索"], "synopsis": "旧梗概A"}},
            {"scene_id": "old-x", "name": "旧X",
             "evaluation": {"story_progress": 0.4, "goal_revision": REV, "synopsis": "旧梗概X"}},
        ],
    })
    legacy = await repository.get_scene(legacy_id)

    return {"A": a, "B": b, "M": m, "after_fork": after_fork,
            "IF0": if0, "IF1": if1, "legacy": legacy}


# 旧实现的实际输出（先在未改动的代码上跑出来，再抄进这里）
EXPECTED = {
    "A": (0.2, ["T1"], ["【A】sA"]),
    # continue 后本场评估从 evaluations 表现读，取到的是新值
    "B": (0.7, ["T2"], ["【A】sA", "【B】sB2"]),
    # M 首次运行时冻结了 [A, B(旧)]；B 后来续跑不得改写它继承的过去。
    # M 自身的推进度对照旧目标，跳过；线索取 M 的显式空表
    "M": (0.5, [], ["【A】sA", "【B】sB", "【M】sM"]),
    "after_fork": (0.7, ["T2"], ["【A】sA", "【B】sB2"]),
    "IF0": (0.3, ["T3"], ["【A】sA", "【B（IF）】sIF"]),
    "IF1": (0.3, ["T3"], ["【A】sA", "【B（IF）】sIF"]),
    # 最近一条缺线索键 → 继续向前找到"旧线索"；推进度取最近一条
    "legacy": (0.4, ["旧线索"], ["【旧A】旧梗概A", "【旧X】旧梗概X"]),
}


async def test_d0_story_context_equivalent_across_lineage(lineage):
    actual = {
        key: await orchestrator._story_context(scene, GOAL) for key, scene in lineage.items()
    }
    assert actual == EXPECTED


# ---------------------------------------------------------------------------
# D1 类型化：旧数据兼容 + 字段存在性
# ---------------------------------------------------------------------------


def _legacy_record(scene_id: str, **ev) -> dict:
    return {"scene_id": scene_id, "name": scene_id.upper(), "evaluation": ev}


async def _raw_scene_with_history(request, history) -> str:
    pid = request.node.name
    await repository.save_project(Project(project_id=pid, name=pid))
    sid = f"{pid}-raw"
    data = {"scene_id": sid, "project_id": pid, "branch_id": "b", "name": "R"}
    if history is not _MISSING:
        data["inherited_story_history"] = history
    await _insert_raw_scene(sid, pid, "b", data)
    return sid


_MISSING = object()


async def test_d1_legacy_scene_history_reads_back_typed(request):
    sid = await _raw_scene_with_history(
        request, [_legacy_record("a", story_progress=0.3, synopsis="梗概", unresolved_threads=["X"])]
    )
    scene = await repository.get_scene(sid)
    [record] = scene.inherited_story_history
    assert isinstance(record, StoryRecord)
    assert (record.scene_id, record.name) == ("a", "A")
    assert record.evaluation.story_progress == 0.3
    assert record.evaluation.unresolved_threads == ["X"]
    assert record.threads_known is True


@pytest.mark.parametrize(("raw", "expected"), [(_MISSING, None), (None, None), ([], [])])
async def test_d1_none_and_empty_stay_distinguishable(request, raw, expected):
    """None = 旧数据请回溯；[] = 权威空历史。读回后不得混同。"""
    sid = await _raw_scene_with_history(request, raw)
    assert (await repository.get_scene(sid)).inherited_story_history == expected


async def test_d1_corrupt_entry_is_skipped_not_fatal(request, caplog):
    """一条坏记录只丢它自己：list_scenes 不得五百，其余记录照常读回。"""
    sid = await _raw_scene_with_history(request, [
        "不是对象",
        {"scene_id": "x", "name": "X", "evaluation": "不是对象"},
        {"scene_id": "y", "name": "Y", "evaluation": {"world_state_delta": ["不是字典"]}},
        _legacy_record("ok", synopsis="好的"),
    ])
    [scene] = await repository.list_scenes(request.node.name)
    assert scene.scene_id == sid
    assert [r.scene_id for r in scene.inherited_story_history] == ["ok"]
    assert caplog.text.count("导演历史第") == 3


async def test_d1_non_list_container_degrades_to_authoritative_empty(request, caplog):
    """整个容器坏了不能退成 None：那会触发回溯，去读当前来源快照、越过分叉边界。"""
    sid = await _raw_scene_with_history(request, {"不是": "列表"})
    assert (await repository.get_scene(sid)).inherited_story_history == []
    assert "不是列表" in caplog.text


async def test_d1_invalid_progress_degrades_instead_of_crashing(request, caplog):
    """手编的字符串进度原先会在 _story_context 的比较处抛 TypeError。"""
    sid = await _raw_scene_with_history(request, [
        _legacy_record("a", story_progress=0.3, goal_revision=REV, synopsis="甲"),
        _legacy_record("b", story_progress="0.9", goal_revision=REV, synopsis="乙"),
        _legacy_record("c", story_progress=float("nan"), goal_revision=REV, synopsis="丙"),
    ])
    scene = await repository.get_scene(sid)
    progress, _, synopses = await orchestrator._story_context(scene, GOAL)
    assert progress == 0.3
    assert synopses == ["【A】甲", "【B】乙", "【C】丙"]
    assert "推进度无效" in caplog.text


@pytest.mark.parametrize("latest", [
    {},  # 缺键
    {"unresolved_threads": None},
    {"unresolved_threads": "不是列表"},
])
async def test_d1_missing_threads_in_frozen_copy_falls_back(request, latest):
    """验收"D1 字段存在性"：最近一条缺线索（或值非列表）→ 取上一条的线索。"""
    sid = await _raw_scene_with_history(request, [
        _legacy_record("a", unresolved_threads=["线索X"]),
        _legacy_record("b", **latest),
    ])
    scene = await repository.get_scene(sid)
    assert scene.inherited_story_history[-1].threads_known is False
    assert scene.inherited_story_history[-1].evaluation.unresolved_threads == []
    _, threads, _ = await orchestrator._story_context(scene, GOAL)
    assert threads == ["线索X"]


async def test_d1_explicit_empty_threads_in_frozen_copy_is_authoritative(request):
    sid = await _raw_scene_with_history(request, [
        _legacy_record("a", unresolved_threads=["线索X"]),
        _legacy_record("b", unresolved_threads=[]),
    ])
    _, threads, _ = await orchestrator._story_context(await repository.get_scene(sid), GOAL)
    assert threads == []


async def test_d1_unknown_threads_survive_save_roundtrip(request):
    """存在性要能跨一次"读出来再存回去"：序列化后 unresolved_threads 的键是 []，
    只看键在不在的话，第二次读取就会把它误判成"线索已清空"。"""
    sid = await _raw_scene_with_history(request, [
        _legacy_record("a", unresolved_threads=["线索X"]),
        _legacy_record("b"),
    ])
    scene = await repository.get_scene(sid)
    await repository.save_scene(scene)
    reloaded = await repository.get_scene(sid)
    assert [r.threads_known for r in reloaded.inherited_story_history] == [True, False]
    _, threads, _ = await orchestrator._story_context(reloaded, GOAL)
    assert threads == ["线索X"]


async def test_d1_legacy_snapshot_history_reads_back_typed(request):
    pid = request.node.name
    await repository.save_project(Project(project_id=pid, name=pid))
    sm = SnapshotManager(pid)
    snap = await sm.create_snapshot("s", "b", {}, label="after:legacy")
    meta = _snapshots_dir(pid) / snap.snapshot_id / "meta.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["story_history"] = [_legacy_record("a", synopsis="旧"), _legacy_record("b")]
    meta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    loaded = await sm.get_snapshot(snap.snapshot_id)
    assert [type(r) for r in loaded.story_history] == [StoryRecord, StoryRecord]
    assert [r.threads_known for r in loaded.story_history] == [False, False]


# ---------------------------------------------------------------------------
# D2 谱系回溯的查询次数与谱系长度无关
# ---------------------------------------------------------------------------


async def _long_chain(project_id: str, length: int) -> Scene:
    """建一条 parent_scene_id 相连、每场都有评估和一段对白的主线，返回最末一场。"""
    await repository.save_project(Project(project_id=project_id, name=project_id))
    parent: str | None = None
    scene: Scene | None = None
    for i in range(length):
        scene = Scene(
            scene_id=f"{project_id}-s{i}",
            project_id=project_id,
            branch_id="main",
            parent_scene_id=parent,
            name=f"S{i}",
            status="completed",
            dialogue_log=[DialogueTurn(turn_number=1, dialogue="台词" * 50)],
        )
        await repository.save_scene(scene)
        await repository.save_evaluation(SceneEvaluation(
            scene_id=scene.scene_id, story_progress=i / length, goal_revision=REV,
            synopsis=f"梗概{i}",
        ))
        parent = scene.scene_id
    assert scene is not None
    return scene


async def _count_queries(monkeypatch, tail: Scene) -> dict[str, int]:
    counts = {"get_evaluation": 0, "get_evaluations": 0, "deserialize_scene": 0, "list_scenes": 0}

    def counting(name, real):
        if asyncio.iscoroutinefunction(real):
            async def wrapper(*a, **k):
                counts[name] += 1
                return await real(*a, **k)
        else:
            def wrapper(*a, **k):
                counts[name] += 1
                return real(*a, **k)
        return wrapper

    monkeypatch.setattr(repository, "get_evaluation",
                        counting("get_evaluation", repository.get_evaluation))
    monkeypatch.setattr(repository, "get_evaluations",
                        counting("get_evaluations", repository.get_evaluations))
    monkeypatch.setattr(repository, "list_scenes",
                        counting("list_scenes", repository.list_scenes))
    monkeypatch.setattr(repository, "_deserialize_scene",
                        counting("deserialize_scene", repository._deserialize_scene))
    _, _, synopses = await orchestrator._story_context(tail, GOAL, synopsis_limit=1000)
    assert len(synopses) == int(tail.name[1:]) + 1  # 确实走完了整条谱系
    return counts


@pytest.mark.parametrize("length", [3, 30])
async def test_d2_lineage_queries_are_constant(monkeypatch, request, length):
    """谱系 N 场时评估查询是一次批量读，且不反序列化任何一场的 dialogue_log。"""
    tail = await _long_chain(request.node.name, length)
    counts = await _count_queries(monkeypatch, tail)
    assert counts == {
        "get_evaluation": 0, "get_evaluations": 1, "deserialize_scene": 0, "list_scenes": 0,
    }


async def test_d2_batch_evaluations_chunk_large_id_lists(request):
    """IN 列表要分批：老版本 SQLite 的绑定参数上限是 999。"""
    pid = request.node.name
    ids = [f"{pid}-{i}" for i in range(repository._IN_CHUNK * 2 + 7)]
    for sid in ids[::97]:
        await repository.save_evaluation(SceneEvaluation(scene_id=sid, synopsis=sid))
    found = await repository.get_evaluations([*ids, ids[0]])
    assert sorted(found) == sorted(ids[::97])
    assert all(found[sid].synopsis == sid for sid in found)


async def test_d2_lineage_projection_tolerates_scalar_history(request):
    """被手改成标量的副本：json_extract 返回裸值，不能当 JSON 文本去解析。"""
    sid = await _raw_scene_with_history(request, "被手改坏了")
    [row] = [s for s in await repository.list_scene_lineage(request.node.name) if s.scene_id == sid]
    assert row.inherited_story_history == []


# ---------------------------------------------------------------------------
# D3 快照列表只搬投影
# ---------------------------------------------------------------------------


async def _insert_legacy_snapshot_row(project_id: str, snapshot_id: str, created_at: str) -> None:
    """本功能上线前的索引行：data_json 是整份快照，含随谱系增长的导演历史副本。"""
    big_history = [
        _legacy_record(f"s{i}", synopsis="很长的梗概" * 200, story_progress=0.1)
        for i in range(50)
    ]
    data = {
        "snapshot_id": snapshot_id,
        "scene_id": "legacy-scene",
        "branch_id": "main",
        "label": "after:旧",
        "created_at": created_at,
        "character_states": {"c1": {"character_id": "c1", "short_term_buffer": ["x" * 500]},
                             "c2": {"character_id": "c2"}},
        "story_history": big_history,
    }
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(snapshot_id, project_id, scene_id, branch_id, label, created_at, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, project_id, "legacy-scene", "main", "after:旧", created_at,
             json.dumps(data, ensure_ascii=False)),
        )
        await conn.commit()


class _LoadsSpy:
    """包住 json 模块，记录应用层实际解析过的最大文本长度。"""

    def __init__(self) -> None:
        self.max_len = 0

    def loads(self, text, *a, **k):
        self.max_len = max(self.max_len, len(text))
        return json.loads(text, *a, **k)

    def __getattr__(self, name):
        return getattr(json, name)


async def test_d3_new_index_row_stores_projection_only(request):
    pid = request.node.name
    sm = SnapshotManager(pid)
    history = [StoryRecord(scene_id="a", name="A", evaluation=SceneEvaluation(synopsis="梗概"))]
    snap = await sm.create_snapshot(
        "s", "main", {"c1": CharacterState(character_id="c1")}, label="after:新",
        story_history=history,
    )
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM snapshots WHERE snapshot_id = ?", (snap.snapshot_id,)
        )
        stored = json.loads((await cur.fetchone())[0])
    assert "story_history" not in stored and "character_states" not in stored
    assert stored["character_ids"] == ["c1"]
    [row] = await sm.list_snapshots()
    assert row == {
        "snapshot_id": snap.snapshot_id, "scene_id": "s", "branch_id": "main",
        "label": "after:新", "created_at": snap.created_at.isoformat(), "character_ids": ["c1"],
    }
    # meta.json 仍是完整真相源
    assert (await sm.get_snapshot(snap.snapshot_id)).story_history == history


async def test_d3_legacy_index_rows_are_projected_in_sql(monkeypatch, request):
    """验收"D3 存量索引"：旧格式大索引行不得整行搬进应用层。"""
    from backend.snapshot import snapshot_manager

    pid = request.node.name
    await _insert_legacy_snapshot_row(pid, f"{pid}-old", "2026-01-01T00:00:00+00:00")
    sm = SnapshotManager(pid)
    new = await sm.create_snapshot("s", "main", {"c3": CharacterState(character_id="c3")})

    spy = _LoadsSpy()
    monkeypatch.setattr(snapshot_manager, "json", spy)
    rows = await sm.list_snapshots()

    assert [r["snapshot_id"] for r in rows] == [new.snapshot_id, f"{pid}-old"]
    old = rows[1]
    assert "story_history" not in old and "character_states" not in old
    assert sorted(old["character_ids"]) == ["c1", "c2"]
    assert spy.max_len < 200, "应用层解析的只该是角色 id 列表这类小片段"


async def test_d3_inspection_latest_snapshot_unchanged_for_both_row_formats(request):
    """inspection 靠列表里的角色 id 找"最近出现快照"，新旧两种索引行都要认得。"""
    from backend.services import inspection

    pid = request.node.name
    sm = SnapshotManager(pid)
    await _insert_legacy_snapshot_row(pid, f"{pid}-old", "2000-01-01T00:00:00+00:00")
    new = await sm.create_snapshot("s", "main", {"c1": CharacterState(character_id="c1")})

    assert await inspection._latest_snapshot_id(sm, "c1") == new.snapshot_id
    assert await inspection._latest_snapshot_id(sm, "c2") == f"{pid}-old"
    assert await inspection._latest_snapshot_id(sm, "c2", branch_id="other") == ""
    assert await inspection._latest_snapshot_id(sm, "nobody") == ""


async def test_d3_patch_rewrites_legacy_row_into_projection(request):
    """旧行在下一次补写时自然瘦身，不需要迁移脚本。"""
    pid = request.node.name
    sm = SnapshotManager(pid)
    snap = await sm.create_snapshot("s", "main", {"c1": CharacterState(character_id="c1")})
    await _insert_legacy_snapshot_row(pid, snap.snapshot_id, snap.created_at.isoformat())
    await sm.record_world_state(snap.snapshot_id, {"季节": "冬"})
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM snapshots WHERE snapshot_id = ?", (snap.snapshot_id,)
        )
        stored = json.loads((await cur.fetchone())[0])
    assert "story_history" not in stored
    assert stored["character_ids"] == ["c1"]
