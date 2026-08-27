"""快照与分支路由。"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import ApiResponse, ForkBranchRequest
from backend.services import orchestrator
from backend.snapshot import SnapshotManager
from backend.utils.serializer import to_dict

project_router = APIRouter(prefix="/projects/{project_id}", tags=["branches"])
snapshot_router = APIRouter(prefix="/snapshots", tags=["snapshots"])


def _serialize_tree_node(node) -> dict:
    return {
        "branch": to_dict(node.branch),
        "children": [_serialize_tree_node(c) for c in node.children],
    }


@project_router.get("/branches")
async def get_branches(project_id: str) -> ApiResponse:
    sm = SnapshotManager(project_id)
    tree = await sm.get_branch_tree(project_id)
    return ApiResponse.ok(
        {
            "project_id": tree.project_id,
            "roots": [_serialize_tree_node(n) for n in tree.roots],
        }
    )


@project_router.get("/snapshots")
async def list_snapshots(project_id: str) -> ApiResponse:
    """列出快照元信息（不含角色状态明细，避免列表接口返回整份快照数据）。"""
    sm = SnapshotManager(project_id)
    snaps = await sm.list_snapshots()
    return ApiResponse.ok(
        [
            {
                "snapshot_id": s.get("snapshot_id", ""),
                "scene_id": s.get("scene_id", ""),
                "branch_id": s.get("branch_id", ""),
                "label": s.get("label", ""),
                "created_at": s.get("created_at", ""),
                "character_count": len(s.get("character_states") or {}),
            }
            for s in snaps
        ]
    )


@snapshot_router.post("/{snapshot_id}/fork")
async def fork_branch(snapshot_id: str, project_id: str, req: ForkBranchRequest) -> ApiResponse:
    """从快照分叉：新建分支并在其上创建一个 pending 首场（不自动开跑）。"""
    branch, scene = await orchestrator.fork_from_snapshot(
        project_id, snapshot_id, req.branch_name, req.new_conditions, req.director_notes
    )
    return ApiResponse.ok({"branch": to_dict(branch), "scene": to_dict(scene)})


@snapshot_router.delete("/{snapshot_id}")
async def delete_snapshot(snapshot_id: str, project_id: str) -> ApiResponse:
    sm = SnapshotManager(project_id)
    await sm.delete_snapshot(snapshot_id)
    return ApiResponse.ok({"deleted": snapshot_id})
