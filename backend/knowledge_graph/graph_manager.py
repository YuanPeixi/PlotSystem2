"""Kuzu 图操作统一接口。

封装 Kuzu 嵌入式数据库。Kuzu 的 Python API 是同步的，
本类用 asyncio.to_thread 包装为异步接口，避免阻塞事件循环。

注意：禁止将 Kuzu 连接对象直接暴露给其他模块，必须通过本接口访问。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.exceptions import PlotSystemError
from backend.knowledge_graph.schema import ALL_DDL, ENTITY_TYPE_TO_TABLE
from backend.models import Entity, Relation
from backend.utils.logger import get_logger

logger = get_logger("graph")

try:  # pragma: no cover - 依赖可能未安装时也能 import 本模块
    import kuzu

    _KUZU_AVAILABLE = True
except Exception:  # noqa: BLE001
    kuzu = None  # type: ignore[assignment]
    _KUZU_AVAILABLE = False


class GraphManager:
    """每个项目持有一个 GraphManager，对应一个 Kuzu 数据库目录。"""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.db_dir: Path = settings.project_dir(project_id) / "kuzu_db"
        self._db: Any = None
        self._conn: Any = None

    # ---- 生命周期 ----
    async def connect(self) -> None:
        """打开数据库连接并确保 schema 存在。"""
        if not _KUZU_AVAILABLE:
            raise PlotSystemError("kuzu 未安装，无法使用知识图谱功能。请 pip install kuzu")
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        self.db_dir.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(self.db_dir))
        self._conn = kuzu.Connection(self._db)
        for ddl in ALL_DDL:
            self._conn.execute(ddl)

    async def close(self) -> None:
        """关闭连接。"""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        self._conn = None
        self._db = None

    # ---- 写入 ----
    async def add_entity(self, entity: Entity) -> None:
        """插入或合并一个实体节点。"""
        table = ENTITY_TYPE_TO_TABLE.get(entity.entity_type, "Concept")
        await asyncio.to_thread(self._add_entity_sync, table, entity)

    def _add_entity_sync(self, table: str, entity: Entity) -> None:
        if table == "Character":
            cypher = (
                "MERGE (n:Character {id: $id}) "
                "SET n.name = $name, n.persona = $desc"
            )
        elif table == "Event":
            cypher = (
                "MERGE (n:Event {id: $id}) "
                "SET n.name = $name, n.description = $desc, n.timestamp_in_story = ''"
            )
        else:
            cypher = (
                f"MERGE (n:{table} {{id: $id}}) "
                "SET n.name = $name, n.description = $desc"
            )
        self._conn.execute(
            cypher,
            {"id": entity.entity_id, "name": entity.name, "desc": entity.description},
        )

    async def add_relation(self, relation: Relation, *, source_type: str, target_type: str) -> None:
        """插入一条关系（按类型选择关系表）。"""
        await asyncio.to_thread(self._add_relation_sync, relation, source_type, target_type)

    def _add_relation_sync(self, rel: Relation, source_type: str, target_type: str) -> None:
        st = ENTITY_TYPE_TO_TABLE.get(source_type, "Concept")
        tt = ENTITY_TYPE_TO_TABLE.get(target_type, "Concept")
        # 选择合适的关系表
        if st == "Character" and tt == "Character":
            cypher = (
                "MATCH (a:Character {id: $s}), (b:Character {id: $t}) "
                "MERGE (a)-[r:KNOWS]->(b) "
                "SET r.relation_type = $rt, r.strength = $strength"
            )
            params = {"s": rel.source_id, "t": rel.target_id, "rt": rel.relation_type, "strength": rel.strength}
        elif st == "Character" and tt == "Location":
            cypher = (
                "MATCH (a:Character {id: $s}), (b:Location {id: $t}) "
                "MERGE (a)-[r:LOCATED_AT]->(b) SET r.time_context = $ctx"
            )
            params = {"s": rel.source_id, "t": rel.target_id, "ctx": rel.description}
        elif st == "Character" and tt == "Event":
            cypher = (
                "MATCH (a:Character {id: $s}), (b:Event {id: $t}) "
                "MERGE (a)-[r:PARTICIPATED_IN]->(b) SET r.role = $role"
            )
            params = {"s": rel.source_id, "t": rel.target_id, "role": rel.relation_type}
        elif st == "Character" and tt == "Concept":
            cypher = (
                "MATCH (a:Character {id: $s}), (b:Concept {id: $t}) "
                "MERGE (a)-[r:MENTIONED_IN]->(b) SET r.context = $ctx"
            )
            params = {"s": rel.source_id, "t": rel.target_id, "ctx": rel.description}
        else:
            cypher = (
                "MATCH (a:Concept {id: $s}), (b:Concept {id: $t}) "
                "MERGE (a)-[r:RELATED_TO]->(b) SET r.relation = $rt"
            )
            params = {"s": rel.source_id, "t": rel.target_id, "rt": rel.relation_type}
        try:
            self._conn.execute(cypher, params)
        except Exception as exc:  # noqa: BLE001
            logger.debug("跳过无法建立的关系 %s->%s: %s", rel.source_id, rel.target_id, exc)

    # ---- 查询 ----
    async def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """执行任意 Cypher 查询，返回行字典列表。"""
        return await asyncio.to_thread(self._query_sync, cypher, params or {})

    def _query_sync(self, cypher: str, params: dict) -> list[dict]:
        result = self._conn.execute(cypher, params)
        rows: list[dict] = []
        columns = result.get_column_names()
        while result.has_next():
            values = result.get_next()
            rows.append(dict(zip(columns, values)))
        return rows

    async def export_graph_for_viz(self) -> dict:
        """导出图谱为前端 G6 可用的 {nodes, edges} 结构。"""
        return await asyncio.to_thread(self._export_viz_sync)

    def _export_viz_sync(self) -> dict:
        nodes: list[dict] = []
        edges: list[dict] = []
        for label in ("Character", "Location", "Event", "Concept"):
            try:
                res = self._conn.execute(f"MATCH (n:{label}) RETURN n.id, n.name")
                while res.has_next():
                    nid, name = res.get_next()
                    nodes.append({"id": nid, "label": name or nid, "nodeType": label})
            except Exception:  # noqa: BLE001
                continue
        rel_specs = [
            ("KNOWS", "Character", "Character"),
            ("LOCATED_AT", "Character", "Location"),
            ("PARTICIPATED_IN", "Character", "Event"),
            ("MENTIONED_IN", "Character", "Concept"),
            ("RELATED_TO", "Concept", "Concept"),
        ]
        for rel, a, b in rel_specs:
            try:
                res = self._conn.execute(
                    f"MATCH (x:{a})-[r:{rel}]->(y:{b}) RETURN x.id, y.id"
                )
                while res.has_next():
                    sid, tid = res.get_next()
                    edges.append({"source": sid, "target": tid, "relType": rel})
            except Exception:  # noqa: BLE001
                continue
        return {"nodes": nodes, "edges": edges}

    # ---- 快照支持 ----
    def checkpoint_to(self, dest_dir: Path) -> str:
        """将整个 Kuzu 数据库目录复制到 dest_dir（用于快照）。返回目标路径。"""
        dest_dir = Path(dest_dir)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        if self.db_dir.exists():
            shutil.copytree(self.db_dir, dest_dir)
        return str(dest_dir)

    def restore_from(self, src_dir: Path) -> None:
        """从快照目录恢复 Kuzu 数据库（覆盖当前）。需先 close。"""
        src_dir = Path(src_dir)
        if not src_dir.exists():
            raise PlotSystemError(f"快照图谱目录不存在: {src_dir}")
        if self.db_dir.exists():
            shutil.rmtree(self.db_dir)
        shutil.copytree(src_dir, self.db_dir)
