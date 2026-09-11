"""PR review 修复的回归测试（#6 / #2 / #3 / #5 与 MAX_PATH）。

与 test_story_snapshot_boundary.py 分开：那份覆盖"冻结副本的语义边界"，
这份覆盖"实现层的降级与并发安全"，失败时指向的修复点不同。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
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
from backend.services.repository import _parse_created_at
from backend.snapshot import SnapshotManager
from backend.snapshot.snapshot_manager import _atomic_write_json, _snapshots_dir
from backend.utils import db
from backend.utils.branch_memory import (
    _marker_path,
    _pending_path,
    is_fork_initialized,
    mark_fork_initialized,
)

GOAL = "揭露叛徒"


def _suppress_rerun(monkeypatch) -> None:
    """continue 会 create_task(run_scene) 异步重跑；测试只验证决策落下的状态。"""
    real_create_task = asyncio.create_task

    def fake(coro, *args, **kwargs):
        coro.close()
        return real_create_task(asyncio.sleep(0))

    monkeypatch.setattr(asyncio, "create_task", fake)


# ---------------------------------------------------------------------------
# #6 created_at 降级
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["not-a-date", "2026-13-45T99:99:99", 12345, [], {}])
def test_created_at_degrades_instead_of_raising(raw):
    """损坏的创建时间只该让这一场排序退化，不该让整个 list_scenes 五百。"""
    assert isinstance(_parse_created_at(raw), datetime)


def test_created_at_roundtrips_valid_value():
    assert _parse_created_at("2026-09-09T10:01:19") == datetime(2026, 9, 9, 10, 1, 19)


async def test_list_scenes_survives_corrupted_created_at():
    pid = "corrupt_created_at"
    await repository.save_project(Project(project_id=pid, name=pid))
    scene = Scene(project_id=pid, branch_id="main", name="坏时间")
    await repository.save_scene(scene)
    # 绕过 save_scene 直接把 data_json 改坏，模拟手工编辑过的历史行
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM scenes WHERE scene_id = ?", (scene.scene_id,)
        )
        data = json.loads((await cur.fetchone())[0])
        data["created_at"] = "definitely-not-iso"
        await conn.execute(
            "UPDATE scenes SET data_json = ? WHERE scene_id = ?",
            (json.dumps(data), scene.scene_id),
        )
        await conn.commit()

    scenes = await repository.list_scenes(pid)
    assert [s.scene_id for s in scenes] == [scene.scene_id]
    assert isinstance((await repository.get_scene(scene.scene_id)).created_at, datetime)


# ---------------------------------------------------------------------------
# #2 冻结副本与回溯之间的去重
# ---------------------------------------------------------------------------


def _rec(scene_id: str, name: str, progress: float) -> dict:
    return {
        "scene_id": scene_id,
        "name": name,
        "evaluation": {"story_progress": progress, "synopsis": f"{name}-{progress}"},
    }


def test_merge_prefers_fresher_record_over_frozen_copy():
    """续跑后重新评估的那份胜出，同一场不得在历史里出现两次。"""
    inherited = [_rec("x", "X", 0.2), _rec("y", "Y", 0.3)]
    tail = [_rec("x", "X", 0.8)]
    merged = orchestrator._merge_story_records(inherited, tail)
    assert [r["scene_id"] for r in merged] == ["y", "x"]
    assert merged[-1]["evaluation"]["story_progress"] == 0.8


def test_merge_keeps_inherited_order_when_disjoint():
    inherited = [_rec("a", "A", 0.1), _rec("b", "B", 0.2)]
    tail = [_rec("c", "C", 0.3)]
    merged = orchestrator._merge_story_records(inherited, tail)
    assert [r["scene_id"] for r in merged] == ["a", "b", "c"]


def test_merge_dedupes_within_tail():
    tail = [_rec("a", "A", 0.1), _rec("a", "A", 0.9)]
    merged = orchestrator._merge_story_records([], tail)
    assert len(merged) == 1


def test_merge_tolerates_records_without_scene_id():
    """旧数据可能缺 scene_id；不能因此互相吞掉。"""
    inherited = [{"name": "旧", "evaluation": {}}]
    tail = [{"name": "新", "evaluation": {}}]
    assert len(orchestrator._merge_story_records(inherited, tail)) == 2


# ---------------------------------------------------------------------------
# #5 快照 meta 的原子写
# ---------------------------------------------------------------------------


def test_atomic_write_uses_unique_short_temp_name(tmp_path):
    target = tmp_path / "meta.json"
    pending = target.with_name(f".{target.stem[:16]}.deadbeef.tmp")
    # 临时名必须与目标区分开：meta.with_suffix('.tmp') 会得到 meta.tmp，
    # 两次并发补写争用同一个名字。
    assert pending.name != "meta.tmp"
    _atomic_write_json(target, '{"ok": true}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_write_leaves_no_temp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "meta.json"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(
        "backend.snapshot.snapshot_manager.os.replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(OSError):
        _atomic_write_json(target, "new")
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_concurrent_atomic_writes_do_not_corrupt(tmp_path):
    """并发补写只能产生某一个完整值，不能是半截内容。

    Windows 上并发 os.replace 指向同一目标可能撞共享冲突（原实现同样如此），
    因此这里只要求"要么成功、要么抛错"，不要求全部成功 —— 关键是文件内容
    永远是某一次写入的完整值。
    """
    target = tmp_path / "meta.json"
    payloads = [json.dumps({"n": i, "pad": "x" * 5000}) for i in range(12)]

    def attempt(payload: str) -> None:
        try:
            _atomic_write_json(target, payload)
        except OSError:
            pass  # 共享冲突可接受，损坏不可接受

    async def run() -> None:
        await asyncio.gather(*(asyncio.to_thread(attempt, p) for p in payloads))

    asyncio.run(run())
    assert json.loads(target.read_text(encoding="utf-8"))["n"] in range(12)
    assert list(tmp_path.glob(".*.tmp")) == []


async def test_record_story_history_uses_atomic_write(monkeypatch, request):
    """补写必须走原子助手，而不是 meta.with_suffix('.tmp') 直写。

    只测助手本身不够：record_story_history 改回直写时助手测试依然全绿，
    这条断言把调用点钉住。
    """
    pid = request.node.name
    await repository.save_project(Project(project_id=pid, name=pid))
    sm = SnapshotManager(pid)
    scene = Scene(project_id=pid, branch_id="main", name="S")
    await repository.save_scene(scene)
    snap = await sm.create_snapshot(scene.scene_id, "main", {}, label="after:t")

    calls: list[Path] = []
    real = _atomic_write_json

    def spy(target: Path, payload: str) -> None:
        calls.append(target)
        real(target, payload)

    monkeypatch.setattr("backend.snapshot.snapshot_manager._atomic_write_json", spy)
    await sm.record_story_history(snap.snapshot_id, [_rec("s", "S", 0.5)])

    assert [p.name for p in calls] == ["meta.json"]
    meta = _snapshots_dir(pid) / snap.snapshot_id / "meta.json"
    assert json.loads(meta.read_text(encoding="utf-8"))["story_history"][0]["scene_id"] == "s"
    # 不留下临时文件，且不生成 meta.tmp 这种与目标同 stem 的名字
    assert not (meta.parent / "meta.tmp").exists()
    assert list(meta.parent.glob(".*.tmp")) == []


async def test_record_story_history_preserves_created_at(request):
    """补写不能把快照重新排到时间线末尾。"""
    pid = request.node.name
    await repository.save_project(Project(project_id=pid, name=pid))
    sm = SnapshotManager(pid)
    scene = Scene(project_id=pid, branch_id="main", name="S")
    await repository.save_scene(scene)
    snap = await sm.create_snapshot(scene.scene_id, "main", {}, label="after:t")
    original = (await sm.get_snapshot(snap.snapshot_id)).created_at

    await sm.record_story_history(snap.snapshot_id, [_rec("s", "S", 0.5)])
    assert (await sm.get_snapshot(snap.snapshot_id)).created_at == original


# ---------------------------------------------------------------------------
# MAX_PATH：临时名必须短于目标名
# ---------------------------------------------------------------------------


def test_fork_marker_temp_name_is_shorter_than_marker(tmp_path):
    """临时名叠加完整目标名会净增 38 字符，长项目名下越过 Windows MAX_PATH。"""
    marker = tmp_path / "branch_initialization" / f"{'a' * 64}.initialized"
    pending = _pending_path(marker)
    assert len(str(pending)) < len(str(marker))
    # 旧实现的命名方式（叠加完整 name + 32 位 uuid）净增 38 字符
    legacy = marker.with_name(f".{marker.name}.{'0' * 32}.tmp")
    assert len(str(legacy)) - len(str(marker)) == 38


def test_mark_fork_initialized_survives_path_that_broke_legacy_naming(tmp_path):
    """构造一条 marker 放得下、但旧命名的临时文件放不下的路径。

    这正是 6 个测试变红的真实形态：marker 227 字符可写，pending 265 字符超
    MAX_PATH(260)，write_text 抛出伪装成 FileNotFoundError 的错误。
    """
    branch = "b" * 36
    probe = _marker_path(tmp_path, branch)
    # 目标：marker 全路径落在 250 附近 —— 低于 260，但 +38 会越界
    padding = max(1, 250 - len(str(probe)))
    deep = tmp_path / ("d" * padding)
    marker = _marker_path(deep, branch)
    legacy = marker.with_name(f".{marker.name}.{'0' * 32}.tmp")
    assert len(str(marker)) < 260 <= len(str(legacy)), "构造失败，未复现长度边界"

    mark_fork_initialized(deep, branch)
    assert is_fork_initialized(deep, branch)
    # 幂等：重复调用不抛错，也不留下临时文件
    mark_fork_initialized(deep, branch)
    assert list((deep / "branch_initialization").glob(".*.tmp")) == []


# ---------------------------------------------------------------------------
# #7 线索的"沿用上一场" vs "空列表是权威值"
# ---------------------------------------------------------------------------


def _ev_rec(scene_id: str, ev: dict) -> dict:
    return {"scene_id": scene_id, "name": scene_id.upper(), "evaluation": ev}


async def _threads_of(records: list[dict], monkeypatch) -> list[str]:
    async def fake(scene, **kw):
        return records

    monkeypatch.setattr(orchestrator, "_story_records", fake)
    _, threads, _ = await orchestrator._story_context(object(), "")
    return threads


async def test_missing_threads_key_falls_back_to_earlier_scene(monkeypatch):
    """LLM 漏返回该键时继续往前找，不能当成"线索全部收束了"。

    写入侧 director_agent._normalize_threads 一直是这么做的；读取侧原先用
    ev.get(key, []) 把"漏键"和"空列表"压成同一个值。
    """
    records = [
        _ev_rec("a", {"unresolved_threads": ["线索X"], "synopsis": "s1"}),
        _ev_rec("b", {"synopsis": "s2"}),  # 漏键
    ]
    assert await _threads_of(records, monkeypatch) == ["线索X"]


async def test_empty_threads_list_is_authoritative(monkeypatch):
    """空列表表示"上一场把线索都收束了"，必须就此打住，不得复活旧线索。"""
    records = [
        _ev_rec("a", {"unresolved_threads": ["线索X"], "synopsis": "s1"}),
        _ev_rec("b", {"unresolved_threads": [], "synopsis": "s2"}),
    ]
    assert await _threads_of(records, monkeypatch) == []


async def test_threads_stop_at_first_present_key(monkeypatch):
    """取最近一份带该键的评估，不继续向前合并。"""
    records = [
        _ev_rec("a", {"unresolved_threads": ["旧线索"]}),
        _ev_rec("b", {"unresolved_threads": ["新线索"]}),
    ]
    assert await _threads_of(records, monkeypatch) == ["新线索"]


async def test_threads_ignore_non_list_values(monkeypatch):
    """损坏值（字符串/None）不是权威空列表，应继续回溯。"""
    records = [
        _ev_rec("a", {"unresolved_threads": ["线索X"]}),
        _ev_rec("b", {"unresolved_threads": None}),
        _ev_rec("c", {"unresolved_threads": "不是列表"}),
    ]
    assert await _threads_of(records, monkeypatch) == ["线索X"]


async def test_no_evaluation_at_all_yields_empty(monkeypatch):
    assert await _threads_of([], monkeypatch) == []


# ---------------------------------------------------------------------------
# #15 复用 SnapshotManager
# ---------------------------------------------------------------------------


async def test_story_records_reuses_passed_snapshot_manager(monkeypatch, request):
    """传入的 sm 必须被复用，不得在循环里各自新建。"""
    pid = request.node.name
    await repository.save_project(Project(project_id=pid, name=pid))
    scene = Scene(project_id=pid, branch_id="main", name="S")
    await repository.save_scene(scene)

    built = []
    real = orchestrator.SnapshotManager

    def spy(project_id):
        built.append(project_id)
        return real(project_id)

    monkeypatch.setattr(orchestrator, "SnapshotManager", spy)
    sm = real(pid)
    await orchestrator._story_records(scene, sm=sm)
    assert built == [], "传入 sm 后不应再构造 SnapshotManager"


# ---------------------------------------------------------------------------
# #3 continue 续跑不得改写继承的过去
#
# 曾经把 inherited_story_history 置 None 让 run_scene "重新冻结"，方向是错的：
# 副本是分叉那一刻的既成事实，None 是"旧数据请回溯推断"的哨兵而非"请重算"的
# 指令。置 None 会把有权威边界的分支降格成旧数据，重新去读当前的
# restore_snapshot_id —— 下面两场景就是由此打开的两个洞。
# ---------------------------------------------------------------------------


@pytest.fixture
async def forked(monkeypatch, request):
    """跑完来源场景并从其后置快照分叉出一条 IF 线，IF 首场也已跑完。"""
    pid = request.node.name
    await repository.save_project(Project(project_id=pid, name=pid, narrative_goal=GOAL))
    await repository.save_character(CharacterCard(project_id=pid, character_id="c", name="甲"))
    monkeypatch.setattr(MemoryManager, "connect", AsyncMock())
    monkeypatch.setattr(MemoryManager, "add_experience", AsyncMock())
    monkeypatch.setattr(MemoryManager, "consolidate", AsyncMock())
    monkeypatch.setattr(CharacterAgent, "respond", AsyncMock(return_value="正在调查。"))

    async def evaluate(self, scene, *args, **kwargs):
        # 必须回填 scene_id：save_evaluation 以它为键，留空会让所有场景
        # 共用一条空 id 的评估，历史里什么也读不到。
        return SceneEvaluation(
            scene_id=scene.scene_id,
            story_progress=0.4,
            synopsis=f"梗概-{scene.name}",
            goal_revision=goal_revision(GOAL),
        )

    monkeypatch.setattr(DirectorAgent, "evaluate_scene", evaluate)
    src = Scene(
        project_id=pid, branch_id="main", name="P", participating_characters=["c"], max_turns=1
    )
    await repository.save_scene(src)
    await orchestrator.run_scene(src.scene_id)
    return pid, await repository.get_scene(src.scene_id)


async def _continue_and_read(scene: Scene, monkeypatch) -> list[str]:
    _suppress_rerun(monkeypatch)
    await orchestrator.apply_decision(
        scene.scene_id, DirectorDecision(decision_type="continue", extra_turns=2)
    )
    reloaded = await repository.get_scene(scene.scene_id)
    records = await orchestrator._story_records(reloaded, include_current=False)
    return [r["name"] for r in records]


async def test_continue_keeps_frozen_history_after_source_snapshot_deleted(forked, monkeypatch):
    """来源快照被删不影响已冻结的分支：历史是既成事实，不随来源消失而归零。"""
    pid, src = forked
    sm = SnapshotManager(pid)
    _, if0 = await orchestrator.fork_from_snapshot(pid, src.snapshot_id_after, "IF")
    await orchestrator.run_scene(if0.scene_id)
    if0 = await repository.get_scene(if0.scene_id)
    assert [r["name"] for r in if0.inherited_story_history] == ["P"]

    await sm.delete_snapshot(src.snapshot_id_after)
    assert await _continue_and_read(if0, monkeypatch) == ["P"]


async def test_continue_does_not_absorb_post_fork_ancestor_evaluation(forked, monkeypatch):
    """分叉后来源才补写的评估不得越过边界渗入 IF 线。"""
    pid, src = forked
    sm = SnapshotManager(pid)
    # 分叉发生在来源评估补写进快照之前
    await sm.record_story_history(src.snapshot_id_after, [])
    _, if0 = await orchestrator.fork_from_snapshot(pid, src.snapshot_id_after, "IF")
    await orchestrator.run_scene(if0.scene_id)
    if0 = await repository.get_scene(if0.scene_id)
    assert if0.inherited_story_history == []

    await sm.record_story_history(
        src.snapshot_id_after,
        [{"scene_id": src.scene_id, "name": "P", "evaluation": {"story_progress": 0.9}}],
    )
    assert await _continue_and_read(if0, monkeypatch) == []


async def test_continue_refreshes_own_evaluation_without_clearing_copy(forked, monkeypatch):
    """本场重新评估后取到的是新值——无需作废副本，副本本就不含本场。"""
    pid, src = forked
    assert src.inherited_story_history == []
    before = await orchestrator._story_records(src)
    assert [r["evaluation"]["story_progress"] for r in before] == [0.4]

    # 续跑后的重新评估走 INSERT OR REPLACE，回溯时现读即为新值
    await repository.save_evaluation(
        SceneEvaluation(
            scene_id=src.scene_id, story_progress=0.8, goal_revision=goal_revision(GOAL)
        )
    )
    _suppress_rerun(monkeypatch)
    await orchestrator.apply_decision(
        src.scene_id, DirectorDecision(decision_type="continue", extra_turns=2)
    )
    reloaded = await repository.get_scene(src.scene_id)
    assert reloaded.status == "pending"
    assert reloaded.inherited_story_history == []  # 副本原样保留
    after = await orchestrator._story_records(reloaded)
    assert [r["evaluation"]["story_progress"] for r in after] == [0.8]
