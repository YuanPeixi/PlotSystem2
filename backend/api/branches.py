"""快照与分支路由。"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import ApiResponse, ForkBranchRequest, UpdateStoryboardRequest
from backend.models import StoryBeat
from backend.services import orchestrator, repository
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


@project_router.get("/branches/{branch_id}/world-state")
async def get_world_state(project_id: str, branch_id: str) -> ApiResponse:
    """读取分支的世界变量（工单07）。分支没有记录时返回空变量，不是 404 ——
    "这条分支还没积累世界层事实"是正常状态，不是资源不存在。
    """
    state = await repository.get_world_state(project_id, branch_id)
    return ApiResponse.ok(to_dict(state))


def _storyboard_payload(board, goal_stale: bool) -> dict:
    return {**to_dict(board), "goal_stale": goal_stale}


@project_router.get("/branches/{branch_id}/storyboard")
async def get_storyboard(project_id: str, branch_id: str) -> ApiResponse:
    """读取分支的导演分镜稿（工单18）。没有记录时返回空分镜稿，不是 404。

    `goal_stale` = 路线图基于旧版主线目标，前端据此显示"已按当前目标重排"的确认入口。
    """
    board, stale = await orchestrator.get_storyboard_view(project_id, branch_id)
    return ApiResponse.ok(_storyboard_payload(board, stale))


@project_router.put("/branches/{branch_id}/storyboard")
async def put_storyboard(
    project_id: str, branch_id: str, req: UpdateStoryboardRequest
) -> ApiResponse:
    """用户整份替换路线图与备忘。修订号不匹配 409、超预算或引用不存在的节拍 422；
    与当前内容完全相同视为重放（响应丢失后的重试），返回 200 且不记 changelog。
    """
    board, stale = await orchestrator.update_storyboard(
        project_id,
        branch_id,
        [
            StoryBeat(
                beat_id=b.beat_id, title=b.title, description=b.description, status=b.status
            )
            for b in req.outline
        ],
        req.memo,
        base_revision=req.revision,
        confirm_goal=req.confirm_goal,
    )
    return ApiResponse.ok(_storyboard_payload(board, stale))


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
                "character_count": len(s.get("character_ids") or []),
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
