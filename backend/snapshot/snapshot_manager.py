"""快照与分支管理。

快照在文件层面复制 Kuzu 数据库目录与 ChromaDB 集合（Kuzu 无事务回滚）。
元数据索引存于 SQLite。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from backend.config import settings
from backend.exceptions import MemoryError as MemoryCopyError
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
            # 不能降级为 debug：图谱进不了快照意味着回滚时图谱不会被恢复
            logger.warning("图谱快照失败，本次快照不含知识图谱：%s", exc)

        # ChromaDB 集合（全量目录拷贝）。拷贝发生在各 MemoryManager 正持有 chroma
        # 连接时，失败不能往上抛：否则一次拷贝失败就会把整场推演拖成 paused。
        chroma_src = settings.project_dir(self.project_id) / "chroma_db"
        if chroma_src.exists():
            import shutil

            try:
                dest = snap_dir / "chroma_collections"
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(chroma_src, dest)
                snap.chroma_checkpoint = str(dest)
            except Exception as exc:  # noqa: BLE001
                logger.warning("长期记忆快照失败，本次快照不含向量库：%s", exc)

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
        """就地恢复项目级图谱与向量库，并返回快照里的角色状态。

        ⚠️ **破坏性操作**：kuzu_db 与 chroma_db 按项目共享、不随分支隔离，
        调用它等于抹掉快照之后所有分支已积累的长期记忆，不可逆。
        只想让某一场从快照接上运行时记忆的，走 `Scene.restore_snapshot_id`
        的懒承接（契约4，`inspection.resolve_scene_states`），不要调本方法。
        目前无生产调用方。
        """
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
                logger.warning("图谱恢复失败，图谱保持当前状态：%s", exc)

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

    async def clone_collections_for_branch(
        self, snapshot_id: str, new_branch_id: str
    ) -> int:
        """把快照时点的长期记忆复制到新分支的集合（工单08 I1 + I3）。

        新分支必须继承来源分支在该时点之前的记忆，否则角色一分叉就失忆；又不能
        直接共用集合，否则两条线互相污染。返回成功复制的集合数。

        契约6：Chroma 不可用或快照不含向量库时只 warning 并跳过，不打断 fork；
        但库可用却复制失败是真错误，抛 `MemoryError` —— 静默失败会让用户拿到一条
        "看上去成功但角色失忆"的新时间线。
        """
        snap = await self.get_snapshot(snapshot_id)
        if snap is None:
            raise SnapshotNotFoundError(f"快照不存在: {snapshot_id}")
        ckpt = Path(snap.chroma_checkpoint) if snap.chroma_checkpoint else None
        # 认 chroma.sqlite3：快照可能是在 Chroma 不可用时打的，此时目录里没有真正的库，
        # 强行 PersistentClient 会往快照目录里写出一个空库（违反 I2 的只读要求）。
        if ckpt is None or not (ckpt / "chroma.sqlite3").exists():
            logger.warning(
                "快照 %s 不含向量库副本，分支 %s 将从空长期记忆开始",
                snapshot_id,
                new_branch_id,
            )
            return 0
        return await asyncio.to_thread(
            self._clone_collections_sync,
            ckpt,
            snap.branch_id,
            new_branch_id,
        )

    def _clone_collections_sync(
        self,
        checkpoint_dir: Path,
        src_branch_id: str,
        new_branch_id: str,
    ) -> int:
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            from backend.memory.embeddings import RemoteEmbeddingFunction
            from backend.memory.long_term import (
                branch_suffix,
                copy_collection,
                max_batch_size,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chroma 不可用，跳过分支记忆复制：%s", exc)
            return 0

        chroma_settings = ChromaSettings(anonymized_telemetry=False, allow_reset=True)
        live_dir = settings.project_dir(self.project_id) / "chroma_db"
        live_dir.mkdir(parents=True, exist_ok=True)
        try:
            src_client = chromadb.PersistentClient(
                path=str(checkpoint_dir), settings=chroma_settings
            )
            dst_client = chromadb.PersistentClient(
                path=str(live_dir), settings=chroma_settings
            )
            available = {c.name for c in src_client.list_collections()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("打开向量库失败，跳过分支记忆复制：%s", exc)
            return 0

        plan = self._plan_clone(
            available, branch_suffix(src_branch_id), branch_suffix(new_branch_id)
        )
        embed_fn = RemoteEmbeddingFunction()
        batch = max_batch_size(dst_client)
        copied = 0
        failed: list[str] = []
        for src_name, dst_name in plan.items():
            try:
                src_col = src_client.get_collection(src_name, embedding_function=embed_fn)
                dst_col = dst_client.get_or_create_collection(
                    dst_name, embedding_function=embed_fn
                )
                # 带着原向量一起搬，避免为历史记忆重新调用 embedding 服务
                copy_collection(src_col, dst_col, batch)
                copied += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("集合 %s 复制到分支 %s 失败：%s", src_name, new_branch_id, exc)
                failed.append(src_name)
        if failed:
            raise MemoryCopyError(
                f"分支 {new_branch_id} 的长期记忆继承失败：{', '.join(failed)}"
            )
        logger.info("分支 %s 继承了 %d 个集合的长期记忆", new_branch_id, copied)
        return copied

    @staticmethod
    def _plan_clone(available: set[str], src_suffix: str, dst_suffix: str) -> dict[str, str]:
        """算出快照里属于来源分支的集合 → 新分支集合名的映射。

        不能只看快照的 `character_states`：那里只有本场参演者，没出场的角色一分叉
        就会拿到空集合。同一角色同时存在分支集合与改造前的项目级集合时取前者。
        """
        plan: dict[str, str] = {}
        for name in available:
            if not name.startswith("char_") or "__" in name:
                continue
            if src_suffix and f"{name}{src_suffix}" in available:
                continue
            plan[name] = f"{name}{dst_suffix}"
        if src_suffix:
            for name in available:
                if name.startswith("char_") and name.endswith(src_suffix):
                    plan[name] = f"{name[: -len(src_suffix)]}{dst_suffix}"
        return plan

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
        async with db.connect() as conn:
            # 必须同时约束 project_id：文件删除本就按项目目录走，只按 snapshot_id 删索引
            # 会让传错 project_id 的请求抹掉别的项目的记录并留下孤儿文件。
            cur = await conn.execute(
                "DELETE FROM snapshots WHERE snapshot_id = ? AND project_id = ?",
                (snapshot_id, self.project_id),
            )
            await conn.commit()
            indexed = cur.rowcount > 0
        if not indexed and not snap_dir.exists():
            raise SnapshotNotFoundError(f"快照不存在: {snapshot_id}")
        if snap_dir.exists():
            shutil.rmtree(snap_dir)

    # ---- 分支 ----
    async def fork_branch(
        self,
        from_snapshot_id: str,
        new_conditions: dict,
        branch_name: str,
        director_notes: str = "",
        branch_id: str = "",
    ) -> Branch:
        snap = await self.get_snapshot(from_snapshot_id)
        if snap is None:
            raise SnapshotNotFoundError(f"快照不存在: {from_snapshot_id}")
        # 只登记来源快照，**不**在这里 restore：恢复会就地覆盖整个项目的图谱与
        # Chroma 长期记忆（两者按项目共享、不随分支隔离），“开一条新 IF 线”不应该当场
        # 把主线的运行态回滚掉。分支首场如何承接该快照归工单 08。

        branch = Branch(
            branch_id=branch_id or new_id(),
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
