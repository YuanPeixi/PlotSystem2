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


def _turn_to_text(turn: DialogueTurn) -> str:
    parts = []
    if turn.action:
        parts.append(f"*{turn.action}*")
    if turn.dialogue:
        parts.append(turn.dialogue)
    if turn.inner_thought:
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

    async def add_experience(self, turn: DialogueTurn) -> None:
        """记录一轮新对话。加入短期缓冲，必要时标记重要事件。"""
        text = _turn_to_text(turn)
        self.short_term.add(text)
        important = self.episodic.record(turn)
        if important:
            await self.long_term.add(important, {"type": "episodic"})
        if self.short_term.is_full():
            await self.consolidate()

    async def retrieve(self, query: str, top_k: int | None = None) -> list[MemoryChunk]:
        """从长期记忆检索相关片段。"""
        await self.connect()
        return await self.long_term.retrieve(query, top_k or settings.MEMORY_TOP_K)

    async def consolidate(self, force: bool = False) -> None:
        """将短期缓冲转存至长期记忆。"""
        await self.connect()
        items = self.short_term.dump()
        if not items:
            return
        if not force and not self.short_term.is_full():
            return
        for text in items:
            await self.long_term.add(text, {"type": "dialogue"})
        self.short_term.clear()
        self.episodic.build_summary()
        logger.debug("角色 %s 记忆固化 %d 条", self.character_id, len(items))

    # ---- 快照 ----
    async def snapshot(self, dest_dir: Path | None = None) -> MemorySnapshot:
        """序列化当前记忆状态。dest_dir 给出时导出 ChromaDB 副本。"""
        await self.connect()
        await self.consolidate(force=True)
        export_path = ""
        if dest_dir is not None:
            export_path = self.long_term.export_to(Path(dest_dir))
        return MemorySnapshot(
            character_id=self.character_id,
            short_term_buffer=self.short_term.dump(),
            episodic_summary=self.episodic.dump(),
            chroma_export_path=export_path,
        )

    async def restore(self, snap: MemorySnapshot) -> None:
        """从快照恢复记忆状态。"""
        self.short_term.load(snap.short_term_buffer)
        self.episodic.load(snap.episodic_summary)
        if snap.chroma_export_path:
            self.long_term.import_from(Path(snap.chroma_export_path))
        await self.connect()
