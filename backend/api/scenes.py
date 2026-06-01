"""场景控制路由（含 SSE 实时流）。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, BackgroundTasks
from sse_starlette.sse import EventSourceResponse

from backend.api.schemas import ApiResponse, CreateSceneRequest, PlanSceneRequest
from backend.models import Scene, SceneStatus, new_id
from backend.services import events, orchestrator, repository
from backend.utils.serializer import to_dict

# 场景创建/规划挂在项目下，控制类接口用独立前缀
project_router = APIRouter(prefix="/projects/{project_id}/scenes", tags=["scenes"])
scene_router = APIRouter(prefix="/scenes", tags=["scenes"])


@project_router.post("/plan")
async def plan_scene(project_id: str, req: PlanSceneRequest) -> ApiResponse:
    """导演规划场景，返回 SceneConfig 建议（不创建）。"""
    config = await orchestrator.plan_scene(project_id, req.branch_id, req.narrative_goal)
    return ApiResponse.ok(to_dict(config))


@project_router.post("")
async def create_scene(project_id: str, req: CreateSceneRequest) -> ApiResponse:
    """创建场景。"""
    initial = dict(req.initial_conditions)
    if req.opening_narration:
        initial["opening_narration"] = req.opening_narration
    scene = Scene(
        scene_id=new_id(),
        project_id=project_id,
        branch_id=req.branch_id,
        name=req.name,
        description=req.description,
        participating_characters=req.participating_characters,
        location=req.location,
        initial_conditions=initial,
        max_turns=req.max_turns,
        status=SceneStatus.PENDING.value,
    )
    await repository.save_scene(scene)
    return ApiResponse.ok(to_dict(scene))


@project_router.get("")
async def list_project_scenes(project_id: str, branch_id: str | None = None) -> ApiResponse:
    """列出项目下所有场景（可按分支过滤）。前端用于绘制分支树下的场景节点。"""
    scenes = await repository.list_scenes(project_id, branch_id)
    return ApiResponse.ok([to_dict(s) for s in scenes])


@project_router.get("/{scene_id}")
async def get_scene(project_id: str, scene_id: str) -> ApiResponse:
    scene = await repository.get_scene(scene_id)
    return ApiResponse.ok(to_dict(scene))


@scene_router.get("/{scene_id}")
async def get_scene_by_id(scene_id: str) -> ApiResponse:
    """通过 scene_id 直接获取场景详情（不需要 project_id）。"""
    scene = await repository.get_scene(scene_id)
    return ApiResponse.ok(to_dict(scene))


@scene_router.post("/{scene_id}/start")
async def start_scene(scene_id: str, background: BackgroundTasks) -> ApiResponse:
    """开始模拟（后台运行，进度经 SSE 推送）。"""
    await repository.get_scene(scene_id)  # 校验存在
    background.add_task(orchestrator.run_scene, scene_id)
    return ApiResponse.ok({"status": "started"})


@scene_router.post("/{scene_id}/pause")
async def pause_scene(scene_id: str) -> ApiResponse:
    ok = orchestrator.pause_scene(scene_id)
    return ApiResponse.ok({"paused": ok})


@scene_router.get("/{scene_id}/log")
async def scene_log(scene_id: str) -> ApiResponse:
    scene = await repository.get_scene(scene_id)
    return ApiResponse.ok([to_dict(t) for t in scene.dialogue_log])


@scene_router.get("/{scene_id}/stream")
async def scene_stream(scene_id: str) -> EventSourceResponse:
    """SSE 实时流。事件类型：turn / status / snapshot / evaluation / error。"""

    async def event_gen():
        q = events.subscribe(scene_id)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=30.0)
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {
                    "event": item["event"],
                    "data": json.dumps(item["data"], ensure_ascii=False),
                }
                if item["event"] in ("status",) and isinstance(item["data"], dict):
                    if item["data"].get("status") in ("completed",):
                        break
        finally:
            events.unsubscribe(scene_id, q)

    return EventSourceResponse(event_gen())
