"""知识图谱可视化数据路由。"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import ApiResponse
from backend.knowledge_graph import GraphManager

router = APIRouter(prefix="/projects/{project_id}", tags=["graph"])


@router.get("/graph")
async def get_graph(project_id: str) -> ApiResponse:
    """返回 G6 可用的 {nodes, edges}。图谱不可用时返回空。"""
    gm = GraphManager(project_id)
    try:
        await gm.connect()
        data = await gm.export_graph_for_viz()
    except Exception:  # noqa: BLE001
        data = {"nodes": [], "edges": []}
    return ApiResponse.ok(data)
