"""场景控制路由（含 SSE 实时流）。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, BackgroundTasks
from sse_starlette.sse import EventSourceResponse

from backend.api.schemas import ApiResponse, CreateSceneRequest, PlanSceneRequest
from backend.config import settings
from backend.exceptions import SceneNotFoundError
from backend.models import Scene, SceneStatus, new_id
from backend.services import events, orchestrator, repository
from backend.utils.serializer import to_dict

# 场景创建/规划挂在项目下，控制类接口用独立前缀
project_router = APIRouter(prefix="/projects/{project_id}/scenes", tags=["scenes"])
scene_router = APIRouter(prefix="/scenes", tags=["scenes"])


def _scene_response(scene: Scene) -> dict:
    """输出前投影进程内运行态。

    已启动但第一轮还没落盘的瞬间，数据库里可能仍是 pending；
    前端据此判断该重连还是启动，不投影就会误触发第二次模拟。
    """
    data = to_dict(scene)
    if orchestrator.is_scene_active(scene.scene_id):
        data["status"] = SceneStatus.RUNNING.value
    return data


async def _get_project_scene(project_id: str, scene_id: str) -> Scene:
    """取场景并校验它确实属于路径里的项目（否则跨项目 ID 也能读到）。"""
    scene = await repository.get_scene(scene_id)
    if scene.project_id != project_id:
        raise SceneNotFoundError(f"场景 {scene_id} 不属于项目 {project_id}")
    return scene


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
        speaker_mode=req.speaker_mode or settings.DEFAULT_SPEAKER_MODE,
        status=SceneStatus.PENDING.value,
    )
    await repository.save_scene(scene)
    return ApiResponse.ok(to_dict(scene))


@project_router.get("")
async def list_scenes(project_id: str, branch_id: str | None = None) -> ApiResponse:
    """列出项目下的场景（可按分支过滤），供前端刷新后恢复导航。"""
    scenes = await repository.list_scenes(project_id, branch_id)
    return ApiResponse.ok([_scene_response(s) for s in scenes])


@project_router.get("/{scene_id}")
async def get_scene(project_id: str, scene_id: str) -> ApiResponse:
    scene = await _get_project_scene(project_id, scene_id)
    return ApiResponse.ok(_scene_response(scene))


@scene_router.get("/{scene_id}")
async def get_scene_by_id(scene_id: str) -> ApiResponse:
    """通过 scene_id 直接获取场景详情（不需要 project_id）。"""
    scene = await repository.get_scene(scene_id)
    return ApiResponse.ok(_scene_response(scene))


@scene_router.post("/{scene_id}/start")
async def start_scene(scene_id: str, background: BackgroundTasks) -> ApiResponse:
    """开始模拟（后台运行，进度经 SSE 推送）。"""
    scene = await repository.get_scene(scene_id)
    # 前置检查：若场景已在运行，直接告知前端，避免误以为又启动了一次新模拟。
    # 真正的并发安全保证在 orchestrator.run_scene 内部的原子检查，这里只是让重复点击
    # 在常见场景下能拿到更及时的响应。
    if orchestrator.is_scene_active(scene_id):
        return ApiResponse.ok({"status": "already_running"})
    # 已完成的场景不得被“重连”重新触发：刷新页面后前端可能对历史场景调
    # 本接口，若放行会把已完结的一场重跑一遍（白烧 LLM 且覆盖快照/评估）。
    # 续跑请走导演决策 continue，它会先把状态重置为 pending。
    if scene.status == SceneStatus.COMPLETED.value:
        return ApiResponse.ok({"status": "already_completed"})
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
    """SSE 实时流。事件类型：turn / status / snapshot / evaluation / scene_error。

    业务失败事件叫 `scene_error` 而不是 `error`：后者是 EventSource 原生的
    连接错误事件名，同名会让前端把两者混在一个处理器里、丢掉失败原因。
    """
    # 场景不存在要在建流之前报错：响应头一旦发出就没法再返回 404 了
    scene = await repository.get_scene(scene_id)

    async def event_gen():
        q = events.subscribe(scene_id)
        try:
            # 首帧回放当前状态：重连的客户端（以及"刚好在订阅前跑完"的竞态）
            # 据此立刻知道该继续等还是该去拉完整日志，而不是干等 ping。
            initial = (
                SceneStatus.RUNNING.value
                if orchestrator.is_scene_active(scene_id)
                else scene.status
            )
            yield {
                "event": "status",
                "data": json.dumps({"status": initial, "initial": True}, ensure_ascii=False),
            }
            # 已经是终态就没什么可等了，直接收流，不要留一个只会 ping 的连接
            if initial in (SceneStatus.COMPLETED.value, SceneStatus.PAUSED.value):
                return
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
                    if item["data"].get("status") in ("completed", "paused"):
                        break
        finally:
            events.unsubscribe(scene_id, q)

    return EventSourceResponse(event_gen())
