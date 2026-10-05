"""物件路由（工单24）。

响应带 `hidden_rules`：这是面向创作者 / 导演的编辑接口，与角色卡接口返回 `unknown_facts`
同一口径。隐藏规则只是不得进入任何角色可见的上下文（契约1），不是对用户保密。
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import ApiResponse, CreateObjectRequest, UpdateObjectRequest
from backend.services import orchestrator, repository
from backend.services.objects import ObjectFields
from backend.utils.serializer import to_dict

router = APIRouter(prefix="/projects/{project_id}/objects", tags=["objects"])


@router.get("")
async def list_objects(project_id: str) -> ApiResponse:
    objects = await repository.list_objects(project_id)
    return ApiResponse.ok([to_dict(o) for o in objects])


@router.get("/{object_id}")
async def get_object(project_id: str, object_id: str) -> ApiResponse:
    return ApiResponse.ok(to_dict(await repository.get_object(project_id, object_id)))


@router.post("")
async def create_object(project_id: str, req: CreateObjectRequest) -> ApiResponse:
    """新建物件。同一个 `request_id` 重放返回同一个物件（契约5）；超预算 422 不截断。"""
    obj = await orchestrator.create_object(
        project_id,
        ObjectFields(
            name=req.name,
            aliases=req.aliases,
            public_description=req.public_description,
            hidden_rules=req.hidden_rules,
            visibility=req.visibility,
            known_by=req.known_by,
        ),
        request_id=req.request_id,
    )
    return ApiResponse.ok(to_dict(obj))


@router.patch("/{object_id}")
async def update_object(project_id: str, object_id: str, req: UpdateObjectRequest) -> ApiResponse:
    """修改物件。修订号不匹配 409；超预算 422 不截断；`request_id` 命中视为重放。"""
    obj = await orchestrator.update_object(
        project_id,
        object_id,
        ObjectFields(
            name=req.name,
            aliases=req.aliases,
            public_description=req.public_description,
            hidden_rules=req.hidden_rules,
            visibility=req.visibility,
            known_by=req.known_by,
        ),
        base_revision=req.revision,
        request_id=req.request_id,
    )
    return ApiResponse.ok(to_dict(obj))


@router.delete("/{object_id}")
async def delete_object(project_id: str, object_id: str) -> ApiResponse:
    """删除物件。已不存在也返回成功（删除天然幂等）。"""
    deleted = await orchestrator.delete_object(project_id, object_id)
    return ApiResponse.ok({"deleted": object_id, "existed": deleted})
