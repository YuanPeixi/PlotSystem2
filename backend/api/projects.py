"""项目管理路由。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, UploadFile

from backend.api.schemas import ApiResponse, CreateProjectRequest
from backend.config import settings
from backend.models import Project
from backend.services import orchestrator, repository
from backend.utils.serializer import to_dict

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("")
async def create_project(req: CreateProjectRequest) -> ApiResponse:
    project = Project(name=req.name, description=req.description)
    await repository.save_project(project)
    return ApiResponse.ok(to_dict(project))


@router.get("")
async def list_projects() -> ApiResponse:
    projects = await repository.list_projects()
    return ApiResponse.ok([to_dict(p) for p in projects])


@router.get("/{project_id}")
async def get_project(project_id: str) -> ApiResponse:
    project = await repository.get_project(project_id)
    return ApiResponse.ok(to_dict(project))


@router.delete("/{project_id}")
async def delete_project(project_id: str) -> ApiResponse:
    await repository.delete_project(project_id)
    return ApiResponse.ok({"deleted": project_id})


@router.post("/{project_id}/seed")
async def upload_seed(project_id: str, file: UploadFile) -> ApiResponse:
    """上传种子文本文件。"""
    project = await repository.get_project(project_id)
    seed_dir = settings.project_dir(project_id) / "seed_texts"
    seed_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "seed.txt"
    dest = seed_dir / filename
    content = await file.read()
    dest.write_bytes(content)
    if str(dest) not in project.seed_texts:
        project.seed_texts.append(str(dest))
    await repository.save_project(project)
    return ApiResponse.ok({"path": str(dest), "size": len(content)})


@router.post("/{project_id}/build")
async def build_project(project_id: str, background: BackgroundTasks) -> ApiResponse:
    """触发 GraphRAG 处理（后台任务）。"""
    await repository.get_project(project_id)  # 校验存在
    background.add_task(orchestrator.run_graphrag, project_id)
    return ApiResponse.ok({"status": "started"})


@router.get("/{project_id}/build/status")
async def build_status(project_id: str) -> ApiResponse:
    return ApiResponse.ok(orchestrator.get_build_status(project_id))
