"""快照与分支路由。"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import ApiResponse, ForkBranchRequest
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
    sm = SnapshotManager(project_id)
    return ApiResponse.ok(await sm.list_snapshots())


@snapshot_router.post("/{snapshot_id}/fork")
async def fork_branch(snapshot_id: str, project_id: str, req: ForkBranchRequest) -> ApiResponse:
    sm = SnapshotManager(project_id)
    branch = await sm.fork_branch(
        snapshot_id, req.new_conditions, req.branch_name, req.director_notes
    )
    return ApiResponse.ok(to_dict(branch))


@snapshot_router.delete("/{snapshot_id}")
async def delete_snapshot(snapshot_id: str, project_id: str) -> ApiResponse:
    sm = SnapshotManager(project_id)
    await sm.delete_snapshot(snapshot_id)
    return ApiResponse.ok({"deleted": snapshot_id})
