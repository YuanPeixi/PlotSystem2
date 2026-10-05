"""工单24 PR-1（设计单 A12）：本场在场物件 `objects_present` 的选择与搬运。

record 档的预过滤只认本场的 `objects_present`，没有它就什么都命中不了。
新建场景的每条路径都要搬运它（同陷阱 3 / 13：只有一条路径对的 bug 前端发现不了）：
手建场景、next_scene 决策、分叉、回滚。
"""

from __future__ import annotations

import pytest

from backend.exceptions import InvalidRequestError
from backend.models import (
    CharacterCard,
    CharacterState,
    DirectorDecision,
    Project,
    Scene,
    SceneConfig,
    WorldObject,
)
from backend.services import orchestrator, repository
from backend.services.objects import MAX_OBJECTS_PRESENT
from backend.snapshot import SnapshotManager

CROWN, NICHE = "o-crown", "o-niche"


async def _project(pid: str) -> Scene:
    await repository.save_project(Project(project_id=pid, name=pid))
    await repository.save_character(CharacterCard(character_id="c1", project_id=pid, name="伊莎贝尔"))
    for oid, name in [(CROWN, "玻璃王冠"), (NICHE, "暗格")]:
        await repository.save_object(WorldObject(object_id=oid, project_id=pid, name=name))
    scene = Scene(
        scene_id=f"{pid}-s1",
        project_id=pid,
        branch_id="branch-main",
        name="加冕前夜",
        participating_characters=["c1"],
        objects_present=[CROWN, NICHE],
        status="completed",
    )
    await repository.save_scene(scene)
    return scene


async def _snapshot(scene: Scene):
    sm = SnapshotManager(scene.project_id)
    return await sm.create_snapshot(
        scene.scene_id, scene.branch_id, {"c1": CharacterState(character_id="c1")}, label="before"
    )


class _Director:
    """不调 LLM：规划结果故意不带物件，用来区分"导演选的"与"人工覆盖的"。"""

    def __init__(self, *args, **kwargs):
        pass

    async def make_decision(self, evaluation, human_override):
        return human_override

    async def plan_scene(self, branch_id, narrative_goal, cards, **kwargs):
        return SceneConfig(name="下一场", participating_characters=["c1"])


# ---------------------------------------------------------------------------
# 持久化与校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_and_read_side_clamp():
    scene = await _project("p-op-roundtrip")
    assert (await repository.get_scene(scene.scene_id)).objects_present == [CROWN, NICHE]

    # 手改库里的 data_json：重复、空白、超长名单在读取侧压回，不报错
    scene.objects_present = [CROWN, CROWN, " ", *[f"o{i}" for i in range(MAX_OBJECTS_PRESENT + 5)]]
    await repository.save_scene(scene)
    loaded = await repository.get_scene(scene.scene_id)
    assert loaded.objects_present[0] == CROWN
    assert len(loaded.objects_present) == MAX_OBJECTS_PRESENT
    assert len(set(loaded.objects_present)) == MAX_OBJECTS_PRESENT


@pytest.mark.asyncio
async def test_check_rejects_unknown_and_too_many_objects():
    scene = await _project("p-op-check")
    pid = scene.project_id
    assert await orchestrator.check_objects_present(pid, [NICHE, CROWN, NICHE, " "]) == [NICHE, CROWN]
    with pytest.raises(InvalidRequestError, match="物件不存在"):
        await orchestrator.check_objects_present(pid, ["o-ghost"])
    with pytest.raises(InvalidRequestError, match="最多"):
        await orchestrator.check_objects_present(pid, [f"o{i}" for i in range(MAX_OBJECTS_PRESENT + 1)])


@pytest.fixture
async def client():
    import httpx

    from backend.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


async def test_http_create_scene_keeps_objects_and_rejects_unknown(client):
    scene = await _project("p-op-http")
    url = f"/projects/{scene.project_id}/scenes"
    body = {"branch_id": "branch-main", "name": "手建", "participating_characters": ["c1"]}

    ok = await client.post(url, json={**body, "objects_present": [NICHE]})
    assert ok.status_code == 200
    created = await repository.get_scene(ok.json()["data"]["scene_id"])
    assert created.objects_present == [NICHE]

    bad = await client.post(url, json={**body, "objects_present": ["o-ghost"]})
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# 建场景路径的搬运
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_scene_override_carries_objects(monkeypatch):
    scene = await _project("p-op-next")
    monkeypatch.setattr(orchestrator, "DirectorAgent", _Director)
    decision = await orchestrator.apply_decision(
        scene.scene_id,
        DirectorDecision(decision_type="next_scene", next_objects_present=[NICHE]),
    )
    new_scene = await repository.get_scene(decision.next_scene_id)
    assert new_scene.objects_present == [NICHE]
    # 决策重放时覆盖字段也要读得回来（决策表是 data_json 的唯一真相源）
    assert (await repository.get_decision(scene.scene_id)).next_objects_present == [NICHE]


@pytest.mark.asyncio
async def test_next_scene_without_override_keeps_director_choice(monkeypatch):
    """覆盖字段为 None = 保持导演规划（陷阱 6），不是"清空物件"。"""
    scene = await _project("p-op-next-none")
    monkeypatch.setattr(orchestrator, "DirectorAgent", _Director)
    decision = await orchestrator.apply_decision(scene.scene_id, DirectorDecision(decision_type="next_scene"))
    assert (await repository.get_scene(decision.next_scene_id)).objects_present == []


@pytest.mark.asyncio
async def test_next_scene_with_unknown_object_fails_before_planning(monkeypatch):
    scene = await _project("p-op-next-bad")
    planned = []

    class Director(_Director):
        async def plan_scene(self, *args, **kwargs):
            planned.append(1)
            return await super().plan_scene(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "DirectorAgent", Director)
    with pytest.raises(InvalidRequestError):
        await orchestrator.apply_decision(
            scene.scene_id, DirectorDecision(decision_type="next_scene", next_objects_present=["o-ghost"])
        )
    assert planned == [], "非法覆盖不该白付一次规划"
    # CAS 守卫已释放，修正后可以重试
    assert (await repository.get_scene(scene.scene_id)).status == "completed"


@pytest.mark.asyncio
async def test_fork_carries_objects():
    scene = await _project("p-op-fork")
    snap = await _snapshot(scene)
    _, new_scene = await orchestrator.fork_from_snapshot(scene.project_id, snap.snapshot_id, "IF线")
    assert new_scene.objects_present == [CROWN, NICHE]


@pytest.mark.asyncio
async def test_rollback_carries_objects():
    scene = await _project("p-op-rollback")
    snap = await _snapshot(scene)
    decision = await orchestrator.apply_decision(
        scene.scene_id,
        DirectorDecision(decision_type="rollback", rollback_to_snapshot_id=snap.snapshot_id),
    )
    assert (await repository.get_scene(decision.next_scene_id)).objects_present == [CROWN, NICHE]


@pytest.mark.asyncio
async def test_create_scene_from_config_carries_objects():
    scene = await _project("p-op-config")
    created = await orchestrator.create_scene_from_config(
        scene.project_id, "branch-main", SceneConfig(name="规划", objects_present=[CROWN])
    )
    assert (await repository.get_scene(created.scene_id)).objects_present == [CROWN]
