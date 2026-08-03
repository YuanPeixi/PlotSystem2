"""场景 API 测试。"""

from __future__ import annotations

import pytest

from backend.api.scenes import get_scene_by_id, list_scenes, project_router
from backend.models import Scene
from backend.services import repository


@pytest.mark.asyncio
async def test_list_scenes_filters_by_project_and_branch() -> None:
    scenes = [
        Scene(scene_id="scene-a1", project_id="project-a", branch_id="branch-a"),
        Scene(scene_id="scene-a2", project_id="project-a", branch_id="branch-b"),
        Scene(scene_id="scene-b1", project_id="project-b", branch_id="branch-a"),
    ]
    for scene in scenes:
        await repository.save_scene(scene)

    branch_response = await list_scenes("project-a", "branch-a")
    project_response = await list_scenes("project-a")

    assert any(route.path == "" and "GET" in route.methods for route in project_router.routes)
    assert [scene["scene_id"] for scene in branch_response.data] == ["scene-a1"]
    assert {scene["scene_id"] for scene in project_response.data} == {
        "scene-a1",
        "scene-a2",
    }


@pytest.mark.asyncio
async def test_get_scene_projects_active_runtime_status(monkeypatch: pytest.MonkeyPatch) -> None:
    scene = Scene(scene_id="scene-active", project_id="project-a", branch_id="branch-a")
    await repository.save_scene(scene)
    monkeypatch.setattr(
        "backend.api.scenes.orchestrator.is_scene_active",
        lambda scene_id: scene_id == "scene-active",
    )

    response = await get_scene_by_id("scene-active")

    assert response.data["status"] == "running"
