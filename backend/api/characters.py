"""角色管理路由。

注意：列表/详情接口返回的角色数据会移除 unknown_facts 之外的内容由前端使用，
但 unknown_facts 是导演专属信息——这里默认返回完整卡（供创作者/导演视图编辑）。
若需对玩家视图隐藏，应在前端按角色权限过滤。
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import ApiResponse, UpdateCharacterRequest
from backend.memory import MemoryManager
from backend.services import repository
from backend.utils.serializer import to_dict

router = APIRouter(prefix="/projects/{project_id}/characters", tags=["characters"])


@router.get("")
async def list_characters(project_id: str) -> ApiResponse:
    cards = await repository.list_characters(project_id)
    return ApiResponse.ok([to_dict(c) for c in cards])


@router.get("/{char_id}")
async def get_character(project_id: str, char_id: str) -> ApiResponse:
    card = await repository.get_character(project_id, char_id)
    return ApiResponse.ok(to_dict(card))


@router.patch("/{char_id}")
async def update_character(
    project_id: str, char_id: str, req: UpdateCharacterRequest
) -> ApiResponse:
    card = await repository.get_character(project_id, char_id)
    updates = req.model_dump(exclude_none=True)
    for key, value in updates.items():
        setattr(card, key, value)
    await repository.save_character(card)
    return ApiResponse.ok(to_dict(card))


@router.get("/{char_id}/memory")
async def get_character_memory(project_id: str, char_id: str) -> ApiResponse:
    mem = MemoryManager(char_id, project_id)
    await mem.connect()
    return ApiResponse.ok(
        {
            "short_term": mem.short_term.dump(),
            "episodic_summary": mem.episodic.dump(),
        }
    )
