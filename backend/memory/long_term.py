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


class LongTermMemory:
    """单个角色的长期记忆库。"""

    def __init__(self, character_id: str, project_id: str):
        self.character_id = character_id
        self.project_id = project_id
        self.db_dir: Path = settings.project_dir(project_id) / "chroma_db"
        self.collection_name = f"char_{character_id.replace('-', '')}"
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
            self._collection = self._client.get_or_create_collection(self.collection_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ChromaDB 初始化失败，使用降级模式：%s", exc)
            self._client = None
            self._collection = None

    async def add(self, text: str, metadata: dict | None = None) -> None:
        meta = metadata or {}
        if self._collection is not None:
            await asyncio.to_thread(self._add_sync, text, meta)
        else:
            self._fallback.append({"text": text, "metadata": meta})

    def _add_sync(self, text: str, meta: dict) -> None:
        try:
            self._collection.add(documents=[text], metadatas=[meta], ids=[new_id()])
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
