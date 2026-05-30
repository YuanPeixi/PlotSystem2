"""输出导出路由。"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import ApiResponse, OutputRequest
from backend.models import OutputFormat, new_id
from backend.services import orchestrator, repository

router = APIRouter(tags=["output"])


@router.post("/projects/{project_id}/output")
async def generate_output(project_id: str, req: OutputRequest) -> ApiResponse:
    try:
        fmt = OutputFormat(req.format)
    except ValueError:
        return ApiResponse.fail(f"不支持的格式: {req.format}")

    content = await orchestrator.generate_output(
        project_id, fmt, req.branch_id, req.scene_ids or None
    )
    output_id = new_id()
    await repository.save_output(output_id, project_id, fmt.value, content)
    return ApiResponse.ok({"output_id": output_id, "format": fmt.value, "content": content})


@router.get("/output/{output_id}")
async def get_output(output_id: str) -> ApiResponse:
    result = await repository.get_output(output_id)
    if result is None:
        return ApiResponse.fail("输出不存在")
    return ApiResponse.ok(result)
