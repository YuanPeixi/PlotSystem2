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
    DirectorDecision,
    Project,
    Scene,
    SceneEvaluation,
    goal_revision,
)
from backend.services import orchestrator, repository
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
