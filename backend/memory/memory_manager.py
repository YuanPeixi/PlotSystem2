"""统一记忆管理接口。每个 CharacterAgent 持有一个实例。

整合三层记忆：短期缓冲、长期向量库、事件摘要。
"""

from __future__ import annotations

from pathlib import Path

from backend.config import settings
from backend.memory.episodic import EpisodicMemory
from backend.memory.long_term import LongTermMemory
from backend.memory.short_term import ShortTermMemory
from backend.models import DialogueTurn, MemoryChunk, MemorySnapshot
from backend.utils.logger import get_logger

logger = get_logger("memory.manager")


def _turn_to_text(turn: DialogueTurn, include_inner_thought: bool = True) -> str:
    """将一轮对话渲染为记忆文本。

    include_inner_thought=False 用于写入"他人轮次"（在场感知）：
    必须剥离内心独白，否则会把该角色的私有内心泄露进旁观者的记忆库
    （CLAUDE.md 第7节“契约1”，工单15）。
    """
    parts = []
    if turn.action:
        parts.append(f"*{turn.action}*")
    if turn.dialogue:
        parts.append(turn.dialogue)
    if include_inner_thought and turn.inner_thought:
        parts.append(f"[{turn.inner_thought}]")
    return f"{turn.character_name}: {' '.join(parts)}"


class MemoryManager:
    """角色记忆的统一门面。"""

    def __init__(self, character_id: str, project_id: str):
        self.character_id = character_id
        self.project_id = project_id
        self.short_term = ShortTermMemory()
        self.long_term = LongTermMemory(character_id, project_id)
        self.episodic = EpisodicMemory(character_id)
        self._connected = False

    async def connect(self) -> None:
        if not self._connected:
            await self.long_term.connect()
            self._connected = True

    async def add_experience(self, turn: DialogueTurn, *, from_self: bool = True) -> None:
        """记录一轮新对话（唯一写入点，由 SceneEngine 对本场全部参演角色调用）。

        from_self=False 表示记录"在场感知"到的他人轮次：剥离内心独白，
        并在 metadata 打上 speaker/self 标记供未来分层检索使用（工单15/09）。
        """
        text = _turn_to_text(turn, include_inner_thought=from_self)
        important = self.episodic.record(turn, include_inner_thought=from_self)
        self.short_term.add(
            text,
            important=important,
            is_self=from_self,
            speaker=turn.character_name,
        )
        if self.short_term.is_full():
            await self.consolidate()

    async def retrieve(self, query: str, top_k: int | None = None) -> list[MemoryChunk]:
        """从长期记忆检索相关片段。"""
        await self.connect()
        return await self.long_term.retrieve(query, top_k or settings.MEMORY_TOP_K)

    async def consolidate(self, force: bool = False) -> None:
        """将短期缓冲转存至长期记忆。

        每条记录只写一次（工单15去重）：重要事件不再额外落一条 episodic 正文副本，
        而是复用同一条文本，仅把 metadata["type"] 标记为 episodic。
        """
        await self.connect()
        items = self.short_term.dump_with_meta()
        if not items:
            return
        if not force and not self.short_term.is_full():
            return
        for text, meta in items:
            await self.long_term.add(
                text,
                {
                    "type": "episodic" if meta.get("important") else "dialogue",
                    "speaker": meta.get("speaker", ""),
                    "self": meta.get("is_self", True),
                },
            )
        self.short_term.clear()
        self.episodic.build_summary()
        logger.debug("角色 %s 记忆固化 %d 条", self.character_id, len(items))

    # ---- 快照 ----
    def prime(self, short_term_buffer: list[str] | None, episodic_summary: str = "") -> None:
        """用快照中的运行时记忆回填本实例（工单14）。

        短期缓冲与事件摘要是纯内存态，续跑/回滚时 MemoryManager 会被重新构造，
        若不回填就会从零开始（只有 ChromaDB 长期记忆是连续的）。
        """
        if short_term_buffer:
            self.short_term.load(list(short_term_buffer))
        if episodic_summary:
            self.episodic.load(episodic_summary)

    async def snapshot(self, dest_dir: Path | None = None) -> MemorySnapshot:
        """序列化当前记忆状态。dest_dir 给出时导出 ChromaDB 副本。

        注意：不在此处强制 consolidate。短期缓冲此时尚未写入长期记忆，
        原样连同 metadata（重要性/发言者）一起存入快照；若在此处
        consolidate(force=True) 后再读取，缓冲区已被清空为空列表（曾是
        工单14修复的 bug）；但反过来若先读取缓冲副本再 consolidate，这批
        文本会同时存在于"已写入长期记忆"和"快照的 short_term_buffer"两处，
        一旦该快照被 restore() 回填、之后再次触发 consolidate，会被二次
        写入长期记忆。保持"不强制固化"是唯一不产生重复写入的顺序。
        """
        await self.connect()
        buffer_with_meta = self.short_term.dump_with_meta()
        export_path = ""
        if dest_dir is not None:
            export_path = self.long_term.export_to(Path(dest_dir))
        return MemorySnapshot(
            character_id=self.character_id,
            short_term_buffer=[text for text, _ in buffer_with_meta],
            short_term_meta=[meta for _, meta in buffer_with_meta],
            episodic_summary=self.episodic.dump(),
            chroma_export_path=export_path,
        )

    async def restore(self, snap: MemorySnapshot) -> None:
        """从快照恢复记忆状态。"""
        if snap.short_term_meta:
            self.short_term.load_with_meta(
                list(zip(snap.short_term_buffer, snap.short_term_meta))
            )
        else:
            self.short_term.load(snap.short_term_buffer)
        self.episodic.load(snap.episodic_summary)
        if snap.chroma_export_path:
            self.long_term.import_from(Path(snap.chroma_export_path))
        await self.connect()
