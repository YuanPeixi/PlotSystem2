"""快照与分支管理。

快照在文件层面复制 Kuzu 数据库目录与 ChromaDB 集合（Kuzu 无事务回滚）。
元数据索引存于 SQLite。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.config import settings
from backend.exceptions import SnapshotNotFoundError
from backend.knowledge_graph import GraphManager
from backend.models import (
    Branch,
    BranchTree,
    CharacterState,
    RelationshipState,
    Snapshot,
    new_id,
)
from backend.snapshot.branch_tree import build_branch_tree
from backend.utils import db
from backend.utils.logger import get_logger
from backend.utils.serializer import to_json

logger = get_logger("snapshot")


def _snapshots_dir(project_id: str) -> Path:
    d = settings.project_dir(project_id) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deserialize_character_state(data: dict) -> CharacterState:
    rels = {
        k: RelationshipState(**v) for k, v in (data.get("relationships") or {}).items()
    }
    return CharacterState(
        character_id=data["character_id"],
        current_emotion=data.get("current_emotion", "平静"),
        current_goal=data.get("current_goal", ""),
        current_location=data.get("current_location", ""),
        relationships=rels,
        long_term_memory_snapshot=data.get("long_term_memory_snapshot", ""),
        episodic_summary=data.get("episodic_summary", ""),
        short_term_buffer=list(data.get("short_term_buffer", []) or []),
    )


class SnapshotManager:
    """快照与分支管理器。"""

    def __init__(self, project_id: str):
        self.project_id = project_id

    # ---- 创建 ----
    async def create_snapshot(
        self,
        scene_id: str,
        branch_id: str,
        character_states: dict[str, CharacterState],
        scene_context: dict | None = None,
        label: str = "",
    ) -> Snapshot:
        snap = Snapshot(
            snapshot_id=new_id(),
            scene_id=scene_id,
            branch_id=branch_id,
            label=label,
            character_states=character_states,
            scene_context=scene_context or {},
        )
        snap_dir = _snapshots_dir(self.project_id) / snap.snapshot_id
        (snap_dir / "character_states").mkdir(parents=True, exist_ok=True)

        # 序列化角色状态
        for cid, state in character_states.items():
            (snap_dir / "character_states" / f"{cid}.json").write_text(
                to_json(state), encoding="utf-8"
            )

        # 复制 Kuzu 图谱
        graph = GraphManager(self.project_id)
        try:
            graph_ckpt = graph.checkpoint_to(snap_dir / "kuzu_checkpoint")
            snap.graph_checkpoint = graph_ckpt
        except Exception as exc:  # noqa: BLE001
            logger.debug("图谱快照跳过：%s", exc)

        # ChromaDB 集合
        chroma_src = settings.project_dir(self.project_id) / "chroma_db"
        if chroma_src.exists():
            import shutil

            dest = snap_dir / "chroma_collections"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(chroma_src, dest)
            snap.chroma_checkpoint = str(dest)

        # 写元数据
        (snap_dir / "meta.json").write_text(to_json(snap), encoding="utf-8")
        await self._index_snapshot(snap)
        logger.info("创建快照 %s（场景 %s）", snap.snapshot_id, scene_id)
        return snap

    async def _index_snapshot(self, snap: Snapshot) -> None:
        async with db.connect() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(snapshot_id, project_id, scene_id, branch_id, label, created_at, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snap.snapshot_id,
                    self.project_id,
                    snap.scene_id,
                    snap.branch_id,
                    snap.label,
                    snap.created_at.isoformat(),
                    to_json(snap),
                ),
            )
            await conn.commit()

    # ---- 恢复 ----
    async def restore_snapshot(self, snapshot_id: str) -> dict[str, CharacterState]:
        snap_dir = _snapshots_dir(self.project_id) / snapshot_id
        if not snap_dir.exists():
            raise SnapshotNotFoundError(f"快照不存在: {snapshot_id}")

        # 恢复 Kuzu
        kuzu_ckpt = snap_dir / "kuzu_checkpoint"
        if kuzu_ckpt.exists():
            graph = GraphManager(self.project_id)
            try:
                graph.restore_from(kuzu_ckpt)
            except Exception as exc:  # noqa: BLE001
                logger.debug("图谱恢复跳过：%s", exc)

        # 恢复 ChromaDB
        chroma_ckpt = snap_dir / "chroma_collections"
        if chroma_ckpt.exists():
            import shutil

            dest = settings.project_dir(self.project_id) / "chroma_db"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(chroma_ckpt, dest)

        # 反序列化角色状态
        states: dict[str, CharacterState] = {}
        cs_dir = snap_dir / "character_states"
        if cs_dir.exists():
            for f in cs_dir.glob("*.json"):
                data = json.loads(f.read_text(encoding="utf-8"))
                states[data["character_id"]] = _deserialize_character_state(data)
        logger.info("恢复快照 %s，角色数 %d", snapshot_id, len(states))
        return states

    async def get_snapshot(self, snapshot_id: str) -> Snapshot | None:
        meta = _snapshots_dir(self.project_id) / snapshot_id / "meta.json"
        if not meta.exists():
            return None
        data = json.loads(meta.read_text(encoding="utf-8"))
        states = {
            cid: _deserialize_character_state(s)
            for cid, s in (data.get("character_states") or {}).items()
        }
        return Snapshot(
            snapshot_id=data["snapshot_id"],
            scene_id=data.get("scene_id", ""),
            branch_id=data.get("branch_id", ""),
            label=data.get("label", ""),
            character_states=states,
            scene_context=data.get("scene_context", {}),
            graph_checkpoint=data.get("graph_checkpoint", ""),
            chroma_checkpoint=data.get("chroma_checkpoint", ""),
        )

    async def list_snapshots(self) -> list[dict]:
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT data_json FROM snapshots WHERE project_id = ? ORDER BY created_at DESC",
                (self.project_id,),
            )
            rows = await cur.fetchall()
        return [json.loads(r[0]) for r in rows]

    async def delete_snapshot(self, snapshot_id: str) -> None:
        import shutil

        snap_dir = _snapshots_dir(self.project_id) / snapshot_id
        if snap_dir.exists():
            shutil.rmtree(snap_dir)
        async with db.connect() as conn:
            await conn.execute(
                "DELETE FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            )
            await conn.commit()

    # ---- 分支 ----
    async def fork_branch(
        self,
        from_snapshot_id: str,
        new_conditions: dict,
        branch_name: str,
        director_notes: str = "",
    ) -> Branch:
        snap = await self.get_snapshot(from_snapshot_id)
        if snap is None:
            raise SnapshotNotFoundError(f"快照不存在: {from_snapshot_id}")
        # 恢复状态（应用初始条件覆盖在使用时进行）
        await self.restore_snapshot(from_snapshot_id)

        branch = Branch(
            branch_id=new_id(),
            project_id=self.project_id,
            parent_branch_id=snap.branch_id or None,
            fork_from_snapshot_id=from_snapshot_id,
            fork_conditions=new_conditions,
            name=branch_name,
            director_notes=director_notes,
        )
        await self.save_branch(branch)
        logger.info("从快照 %s 创建分支 %s（%s）", from_snapshot_id, branch.branch_id, branch_name)
        return branch

    async def save_branch(self, branch: Branch) -> None:
        async with db.connect() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO branches "
                "(branch_id, project_id, parent_branch_id, name, created_at, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    branch.branch_id,
                    self.project_id,
                    branch.parent_branch_id,
                    branch.name,
                    branch.created_at.isoformat(),
                    to_json(branch),
                ),
            )
            await conn.commit()

    async def list_branches(self) -> list[Branch]:
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT data_json FROM branches WHERE project_id = ? ORDER BY created_at",
                (self.project_id,),
            )
            rows = await cur.fetchall()
        branches = []
        for (data_json,) in rows:
            data = json.loads(data_json)
            branches.append(
                Branch(
                    branch_id=data["branch_id"],
                    project_id=data["project_id"],
                    parent_branch_id=data.get("parent_branch_id"),
                    fork_from_snapshot_id=data.get("fork_from_snapshot_id"),
                    fork_conditions=data.get("fork_conditions", {}),
                    name=data.get("name", ""),
                    scenes=list(data.get("scenes", []) or []),
                    director_notes=data.get("director_notes", ""),
                )
            )
        return branches

    async def get_branch_tree(self, project_id: str | None = None) -> BranchTree:
        branches = await self.list_branches()
        return build_branch_tree(project_id or self.project_id, branches)

    async def ensure_main_branch(self) -> Branch:
        """确保项目存在主分支，没有则创建。"""
        branches = await self.list_branches()
        if branches:
            return branches[0]
        main = Branch(
            branch_id=new_id(),
            project_id=self.project_id,
            name="主线",
            director_notes="项目初始主分支",
        )
        await self.save_branch(main)
        return main
