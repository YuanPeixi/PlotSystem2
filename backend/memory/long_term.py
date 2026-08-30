"""长期记忆：ChromaDB 向量存储 + LlamaIndex 检索。

设计为优雅降级：
- 优先使用 ChromaDB（持久化向量库）做语义检索；
- 若 ChromaDB / 嵌入服务不可用，退化为基于关键词重叠的内存检索，
  保证系统在离线/无嵌入环境下仍可运行。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from backend.config import settings
from backend.memory.embeddings import RemoteEmbeddingFunction
from backend.models import MemoryChunk, new_id
from backend.utils.logger import get_logger

logger = get_logger("memory.long_term")

try:  # pragma: no cover
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    _CHROMA_AVAILABLE = True
except Exception:  # noqa: BLE001
    chromadb = None  # type: ignore[assignment]
    _CHROMA_AVAILABLE = False


def branch_suffix(branch_id: str) -> str:
    """集合名的分支后缀。空 branch_id 表示项目级共享集合（改造前的老数据）。"""
    return f"__{branch_id.replace('-', '')}" if branch_id else ""


def collection_name_for(character_id: str, branch_id: str = "") -> str:
    """角色在某条分支上的 Chroma 集合名。branch_id 为空表示项目级共享集合。"""
    return f"char_{character_id.replace('-', '')}{branch_suffix(branch_id)}"


def max_batch_size(client, default: int = 1000) -> int:
    """取 Chroma 客户端单次写入的条数上限（不同版本暴露方式不同）。"""
    getter = getattr(client, "get_max_batch_size", None)
    if callable(getter):
        try:
            return max(1, int(getter()))
        except Exception:  # noqa: BLE001
            pass
    try:
        return max(1, int(getattr(client, "max_batch_size", default)))
    except (TypeError, ValueError):
        return default


def copy_collection(src_col, dst_col, batch_size: int) -> int:
    """分页读、分批写地整体搬运一个集合（带原向量），返回搬运条数。

    Chroma 单次 add/upsert 有条数上限（1.5.9 实测 5461），一次性读全量再一次性
    写入会在大集合上整组失败。用源记录的原 id upsert，中断后重跑天然幂等。
    """
    total = 0
    offset = 0
    while True:
        page = src_col.get(
            limit=batch_size,
            offset=offset,
            include=["documents", "metadatas", "embeddings"],
        )
        ids = list(page.get("ids") or [])
        if not ids:
            break
        metas = page.get("metadatas")
        embeddings = page.get("embeddings")
        dst_col.upsert(
            ids=ids,
            documents=list(page.get("documents") or []),
            # chromadb 不接受 None 元素，补一个占位字段与 _add_sync 保持一致
            metadatas=[m or {"source": "unknown"} for m in metas] if metas is not None else None,
            embeddings=embeddings if embeddings is not None else None,
        )
        total += len(ids)
        offset += len(ids)
        if len(ids) < batch_size:
            break
    return total


class LongTermMemory:
    """单个角色的长期记忆库。"""

    def __init__(self, character_id: str, project_id: str, branch_id: str = ""):
        self.character_id = character_id
        self.project_id = project_id
        self.branch_id = branch_id
        self.db_dir: Path = settings.project_dir(project_id) / "chroma_db"
        # 分支后缀让 IF 线与主线各自持有独立向量集合（工单08 不变量 I3）。
        # 留空 = 项目级共享集合，兼容既有数据，无需迁移。
        self.collection_name = collection_name_for(character_id, branch_id)
        self._client = None
        self._collection = None
        # 降级时的内存存储
        self._fallback: list[dict] = []

    async def connect(self) -> None:
        if not _CHROMA_AVAILABLE:
            logger.warning("ChromaDB 不可用，长期记忆退化为关键词检索模式。")
            return
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        self.db_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._client = chromadb.PersistentClient(
                path=str(self.db_dir),
                settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
            )
            embed_fn = RemoteEmbeddingFunction()
            self._adopt_legacy_collection(embed_fn)
            self._collection = self._client.get_or_create_collection(
                self.collection_name,
                embedding_function=embed_fn,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ChromaDB 初始化失败，使用降级模式：%s", exc)
            self._client = None
            self._collection = None

    def _adopt_legacy_collection(self, embed_fn) -> None:
        """把改造前的项目级集合一次性承接到本分支集合。

        集合名加分支后缀（工单08 I3）之后，老项目的记忆全留在无后缀集合里，而生产
        路径一律传分支 ID —— 不承接就等于升级即失忆（数据还在，检索不到）。
        """
        legacy_name = collection_name_for(self.character_id, "")
        if legacy_name == self.collection_name:
            return
        existing = {c.name for c in self._client.list_collections()}
        if legacy_name not in existing:
            return
        if self.collection_name in existing:
            dst = self._client.get_collection(self.collection_name, embedding_function=embed_fn)
            if dst.count() > 0:
                return
        else:
            dst = self._client.get_or_create_collection(
                self.collection_name, embedding_function=embed_fn
            )
        src = self._client.get_collection(legacy_name, embedding_function=embed_fn)
        try:
            moved = copy_collection(src, dst, max_batch_size(self._client))
        except Exception as exc:  # noqa: BLE001
            # 半截的集合会让下次连接误判为“已承接”，删掉让它自愈重来
            logger.warning("角色 %s 承接项目级历史记忆失败：%s", self.character_id, exc)
            try:
                self._client.delete_collection(self.collection_name)
            except Exception:  # noqa: BLE001
                logger.warning("清理半成品集合 %s 失败", self.collection_name)
            return
        if moved:
            logger.info(
                "角色 %s 承接 %d 条项目级历史记忆到分支 %s",
                self.character_id,
                moved,
                self.branch_id,
            )

    async def add(self, text: str, metadata: dict | None = None) -> None:
        meta = metadata or {}
        if self._collection is not None:
            await asyncio.to_thread(self._add_sync, text, meta)
        else:
            self._fallback.append({"text": text, "metadata": meta})

    def _add_sync(self, text: str, meta: dict) -> None:
        try:
            # chromadb 校验 metadata 不允许空 dict，兜底填充一个占位字段
            safe_meta = meta or {"source": "unknown"}
            self._collection.add(documents=[text], metadatas=[safe_meta], ids=[new_id()])
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入 ChromaDB 失败，转入降级：%s", exc)
            self._collection = None
            self._fallback.append({"text": text, "metadata": meta})

    async def retrieve(self, query: str, top_k: int = 5) -> list[MemoryChunk]:
        if self._collection is not None:
            return await asyncio.to_thread(self._retrieve_sync, query, top_k)
        return self._retrieve_fallback(query, top_k)

    def _retrieve_sync(self, query: str, top_k: int) -> list[MemoryChunk]:
        try:
            res = self._collection.query(query_texts=[query], n_results=top_k)
            docs = (res.get("documents") or [[]])[0]
            dists = (res.get("distances") or [[]])[0] or [0.0] * len(docs)
            metas = (res.get("metadatas") or [[]])[0] or [{}] * len(docs)
            return [
                MemoryChunk(text=d, score=1.0 - float(dist), metadata=m or {})
                for d, dist, m in zip(docs, dists, metas)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("ChromaDB 检索失败，使用降级：%s", exc)
            return self._retrieve_fallback(query, top_k)

    def _retrieve_fallback(self, query: str, top_k: int) -> list[MemoryChunk]:
        """关键词重叠打分的简单检索。"""
        q_tokens = set(query.lower())
        scored: list[MemoryChunk] = []
        for item in self._fallback:
            text = item["text"]
            overlap = len(q_tokens & set(text.lower()))
            scored.append(MemoryChunk(text=text, score=float(overlap), metadata=item["metadata"]))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:top_k]

    # ---- 快照 ----
    def export_to(self, dest_dir: Path) -> str:
        """导出 ChromaDB 目录副本（用于快照）。"""
        dest_dir = Path(dest_dir)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        if self.db_dir.exists():
            shutil.copytree(self.db_dir, dest_dir)
            return str(dest_dir)
        return ""

    def import_from(self, src_dir: Path) -> None:
        """从快照恢复 ChromaDB 目录。"""
        src_dir = Path(src_dir)
        if not src_dir.exists():
            return
        if self.db_dir.exists():
            shutil.rmtree(self.db_dir)
        shutil.copytree(src_dir, self.db_dir)
