"""长期记忆：ChromaDB 向量存储 + LlamaIndex 检索。

设计为优雅降级：
- 优先使用 ChromaDB（持久化向量库）做语义检索；
- 若 ChromaDB / 嵌入服务不可用，退化为基于关键词重叠的内存检索，
  保证系统在离线/无嵌入环境下仍可运行。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from pathlib import Path

from backend.config import settings
from backend.memory.embeddings import RemoteEmbeddingFunction
from backend.models import MemoryChunk
from backend.utils.branch_memory import is_fork_initialized
from backend.utils.logger import get_logger

logger = get_logger("memory.long_term")

try:  # pragma: no cover
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    _CHROMA_AVAILABLE = True
except Exception:  # noqa: BLE001
    chromadb = None  # type: ignore[assignment]
    _CHROMA_AVAILABLE = False


# Chroma 集合名上限 63 字符、只许字母数字/下划线/连字符。"char_" + 32 位角色 hex
# 已占 37，直接拼 32 位分支 hex 会到 71 —— 生产 ID 全是 uuid4，每个带分支的集合都会
# 被拒，长期记忆整体静默降级、分叉必然失败。分支只取摘要前 24 位，恰好凑满 63。
_BRANCH_DIGEST_LEN = 24


def branch_suffix(branch_id: str) -> str:
    """集合名的分支后缀。空 branch_id 表示项目级共享集合（改造前的老数据）。

    用摘要而不是截断原 ID：分支 ID 可以是任意字符串，截断/去连字符都可能撞名或含非法字符。
    """
    if not branch_id:
        return ""
    digest = hashlib.sha256(branch_id.encode("utf-8")).hexdigest()[:_BRANCH_DIGEST_LEN]
    return f"__{digest}"


def collection_name_for(character_id: str, branch_id: str = "") -> str:
    """角色在某条分支上的 Chroma 集合名。branch_id 为空表示项目级共享集合。"""
    return f"char_{character_id.replace('-', '')}{branch_suffix(branch_id)}"


# 集合元数据上的“不需要再从项目级老集合承接”标记。不能用“集合非空”代替：
# 空集合可能是“尚未承接”，也可能是“分叉点本来就没记忆”，两者误判会把
# 分叉点之后的共享记忆灌进历史分支；非空也可能是“承接到一半被杀”。
LEGACY_ADOPTED_KEY = "legacy_adopted"

# 写入/检索失败后暂停访问 Chroma 的时长。失败多半是远程 embedding 抖动，旧实现
# 失败一次就永久断开：本场余下的写入全进内存、场景结束即丢，而水位线照推
# （工单26），续跑也不会重放 —— 一次网络抖动换来半场永久失忆，日志里只有一条 warning。
RETRY_COOLDOWN_SECONDS = 30.0


def is_adopted(col) -> bool:
    return bool((col.metadata or {}).get(LEGACY_ADOPTED_KEY))


def mark_adopted(col) -> None:
    """标记集合已完成初始化，不得再从项目级老集合补数据。"""
    if is_adopted(col):
        return
    col.modify(metadata={**(col.metadata or {}), LEGACY_ADOPTED_KEY: True})


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


def memory_id(text: str) -> str:
    """长期记忆条目的内容寻址 ID（工单26）。

    集合名已经含 character_id + branch_id，所以同一集合内只需按正文寻址：
    同一段文本重复写入落在同一个 ID 上，检索结果自然只有一条。这是与
    `Scene.turns_consolidated` 水位线无关的兜底 —— 未来每新增一个写入点
    （世界变量回写、裁决落档、动态图谱写回）都靠"记得维护水位线"是维持不住的。

    已接受的副作用：同一角色在不同场次说出**完全相同**的一句话会被合并成一条
    （方向与工单15 的同句去重一致）。
    """
    return "mem_" + hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        # 降级时的内存存储（与 Chroma 路径同语义：按 memory_id 去重，契约6）
        self._fallback: list[dict] = []
        self._fallback_ids: set[str] = set()
        # 暂时断开：冷却期内不碰 Chroma，没写进去的条目按序暂存，恢复后先补写。
        # 与 `_collection is None`（Chroma 缺失/初始化失败，整个实例生命周期降级）区分。
        self._retry_at = 0.0
        self._pending: dict[str, tuple[str, dict]] = {}

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

        分叉初始化凭据禁止整条分支从当前共享集合补数据，包括快照时尚不存在的角色集合；
        普通升级分支仍按 `LEGACY_ADOPTED_KEY` 判断每个集合是否完成承接；
        反过来，承接到一半被硬杀也不会因为集合非空而被当成“已完成”，下次连接会
        用原 id 继续 upsert 补齐。
        """
        if is_fork_initialized(self.db_dir, self.branch_id):
            return
        legacy_name = collection_name_for(self.character_id, "")
        if legacy_name == self.collection_name:
            return
        existing = {c.name for c in self._client.list_collections()}
        if legacy_name not in existing:
            return
        dst = self._client.get_or_create_collection(
            self.collection_name, embedding_function=embed_fn
        )
        if is_adopted(dst):
            return
        src = self._client.get_collection(legacy_name, embedding_function=embed_fn)
        try:
            moved = copy_collection(src, dst, max_batch_size(self._client))
            mark_adopted(dst)
        except Exception as exc:  # noqa: BLE001
            # 不删集合：没打标记就不算完成，下次连接会幂等地继续搬完
            logger.warning("角色 %s 承接项目级历史记忆失败，下次连接重试：%s", self.character_id, exc)
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
            self._add_fallback(text, meta)

    def _add_fallback(self, text: str, meta: dict) -> None:
        """降级路径的写入，与 Chroma 路径同样按 memory_id 幂等（契约6 / 工单26）。"""
        mid = memory_id(text)
        if mid in self._fallback_ids:
            return
        self._fallback_ids.add(mid)
        self._fallback.append({"id": mid, "text": text, "metadata": meta})

    def _add_sync(self, text: str, meta: dict) -> None:
        if self._cooling_down():
            self._defer(text, meta)
            return
        try:
            # 先补写暂存的，保持写入顺序；补写失败本条也一并暂存
            self._flush_pending()
            self._write_chroma(text, meta)
        except Exception as exc:  # noqa: BLE001
            self._defer(text, meta)
            self._suspend("写入", exc)

    def _write_chroma(self, text: str, meta: dict) -> None:
        mid = memory_id(text)
        # 先查存在性再写：upsert 会无条件重算 embedding，而 embedding 走远程
        # 计费接口（memory/embeddings.py）。include=[] 只回 ids，不触发 embedding。
        if (self._collection.get(ids=[mid], include=[]).get("ids") or []):
            return
        # chromadb 校验 metadata 不允许空 dict，兜底填充一个占位字段
        safe_meta = meta or {"source": "unknown"}
        # upsert 而非 add：并发/重试下撞同一 ID 时不抛错，结果仍是一条
        self._collection.upsert(documents=[text], metadatas=[safe_meta], ids=[mid])

    def _cooling_down(self) -> bool:
        return time.monotonic() < self._retry_at

    def _defer(self, text: str, meta: dict) -> None:
        """暂存待补写；同时进降级存储，断开期间本场检索仍能看到它。"""
        self._pending[memory_id(text)] = (text, meta)
        self._add_fallback(text, meta)

    def _suspend(self, op: str, exc: Exception) -> None:
        self._retry_at = time.monotonic() + RETRY_COOLDOWN_SECONDS
        logger.warning(
            "ChromaDB %s失败，%.0f 秒内暂用降级模式（角色 %s 待补写 %d 条）：%s",
            op,
            RETRY_COOLDOWN_SECONDS,
            self.character_id,
            len(self._pending),
            exc,
        )

    def _flush_pending(self) -> None:
        """按暂存顺序补写；中途失败则异常上抛，已写入的出队、其余留待下次。"""
        if not self._pending:
            return
        flushed = 0
        for mid, (text, meta) in list(self._pending.items()):
            self._write_chroma(text, meta)
            del self._pending[mid]
            flushed += 1
        self._retry_at = 0.0
        logger.info("ChromaDB 已恢复，角色 %s 补写 %d 条长期记忆", self.character_id, flushed)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def flush(self) -> int:
        """无视冷却补写一次暂存条目，返回仍未写入的条数。

        固化收尾时调用：实例随场景结束被丢弃，这是暂存条目最后的补写机会。
        """
        if self._collection is not None and self._pending:
            await asyncio.to_thread(self._flush_sync)
        return len(self._pending)

    def _flush_sync(self) -> None:
        try:
            self._flush_pending()
        except Exception as exc:  # noqa: BLE001
            self._suspend("补写", exc)

    async def retrieve(self, query: str, top_k: int = 5) -> list[MemoryChunk]:
        if self._collection is not None and not self._cooling_down():
            return await asyncio.to_thread(self._retrieve_sync, query, top_k)
        return self._retrieve_fallback(query, top_k)

    def _retrieve_sync(self, query: str, top_k: int) -> list[MemoryChunk]:
        try:
            # 暂存条目不在向量库里，先补写，否则恢复后的检索看不到断开期间的记忆
            self._flush_pending()
            res = self._collection.query(query_texts=[query], n_results=top_k)
            docs = (res.get("documents") or [[]])[0]
            dists = (res.get("distances") or [[]])[0] or [0.0] * len(docs)
            metas = (res.get("metadatas") or [[]])[0] or [{}] * len(docs)
            return [
                MemoryChunk(text=d, score=1.0 - float(dist), metadata=m or {})
                for d, dist, m in zip(docs, dists, metas)
            ]
        except Exception as exc:  # noqa: BLE001
            self._suspend("检索", exc)
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
