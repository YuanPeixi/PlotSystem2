"""角色管理路由。

注意：列表/详情接口返回的角色数据会移除 unknown_facts 之外的内容由前端使用，
但 unknown_facts 是导演专属信息——这里默认返回完整卡（供创作者/导演视图编辑）。
若需对玩家视图隐藏，应在前端按角色权限过滤。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from backend.api.schemas import ApiResponse, UpdateCharacterRequest
from backend.services import inspection, repository
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
async def get_character_memory(
    project_id: str,
    char_id: str,
    scene_id: str = Query("", description="指定场景时点；留空则取该角色最近一次快照"),
    snapshot_id: str = Query("", description="直接指定快照，优先级最高"),
) -> ApiResponse:
    """角色的运行时记忆（短期缓冲 + 事件摘要）。

    这两层是纯内存态，只存在于快照里；旧实现每次新建 MemoryManager，
    因而恒返回空数据（工单17 修复）。
    """
    state, source = await inspection.load_character_state(
        project_id, char_id, scene_id=scene_id, snapshot_id=snapshot_id
    )
    return ApiResponse.ok(
        {
            "short_term": state.short_term_buffer,
            "episodic_summary": state.episodic_summary,
            "source_snapshot_id": source,
        }
    )


@router.get("/{char_id}/inspect")
async def inspect_character(
    project_id: str,
    char_id: str,
    scene_id: str = Query("", description="指定场景时点；留空则取该角色最近一次快照"),
    snapshot_id: str = Query("", description="直接指定快照，优先级最高"),
    branch_id: str = Query("", description="限定分支后再取最近快照"),
    query: str = Query("", description="给出时附带长期记忆检索结果（会触发 embedding）"),
    top_k: int | None = Query(None, ge=1, le=50),
    include_private: bool = Query(True, description="False 时抹掉 unknown_facts"),
) -> ApiResponse:
    """导演视角的角色内部视图：人设 + 时点状态 + 三层记忆。"""
    result = await inspection.inspect_character(
        project_id,
        char_id,
        scene_id=scene_id,
        snapshot_id=snapshot_id,
        branch_id=branch_id,
        memory_query=query,
        top_k=top_k,
        include_private=include_private,
    )
    return ApiResponse.ok(to_dict(result))
