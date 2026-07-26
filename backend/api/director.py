"""导演决策路由。"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import ApiResponse, DecisionRequest
from backend.models import DirectorDecision
from backend.services import orchestrator, repository
from backend.utils.serializer import to_dict

router = APIRouter(prefix="/scenes", tags=["director"])


@router.get("/{scene_id}/evaluation")
async def get_evaluation(scene_id: str) -> ApiResponse:
    evaluation = await repository.get_evaluation(scene_id)
    if evaluation is None:
        return ApiResponse.ok(None)
    return ApiResponse.ok(to_dict(evaluation))


@router.post("/{scene_id}/decision")
async def submit_decision(scene_id: str, req: DecisionRequest) -> ApiResponse:
    """提交导演决策（可覆盖 AI 建议）。"""
    override = DirectorDecision(
        decision_type=req.decision_type,
        extra_turns=req.extra_turns,
        rollback_to_snapshot_id=req.rollback_snapshot_id,
        new_initial_conditions=req.new_initial_conditions,
        rollback_notes=req.rollback_notes,
        next_scene_description=req.next_scene_description,
        next_participating_characters=req.next_participating_characters,
        next_location=req.next_location,
        next_initial_conditions=req.next_initial_conditions,
    )
    # 重复提交保护：同一场景的决策正在处理时，orchestrator 会抛出 ConflictError，
    # 由 main.py 的全局异常处理器转为 409 响应，无需在此处额外捕获。
    decision = await orchestrator.apply_decision(scene_id, override)
    return ApiResponse.ok(to_dict(decision))
