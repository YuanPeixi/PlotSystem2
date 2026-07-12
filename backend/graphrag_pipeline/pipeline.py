"""GraphRAG 主管线：种子文本 → 知识图谱 + 角色卡 + 世界规则。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from backend.graphrag_pipeline.entity_extractor import EntityExtractor
from backend.graphrag_pipeline.persona_builder import PersonaBuilder
from backend.graphrag_pipeline.world_rules import WorldRulesExtractor
from backend.knowledge_graph import GraphManager
from backend.models import CharacterCard, Entity, LoreEntry, Relation
from backend.utils.logger import get_logger

logger = get_logger("graphrag.pipeline")

ProgressCallback = Callable[[str, float], Awaitable[None]] | None
OnCharacterCallback = Callable[[CharacterCard, int, int], Awaitable[None]] | None


@dataclass
class GraphRAGConfig:
    """GraphRAG 管线配置。"""

    chunk_size: int = 4000
    chunk_overlap: int = 200


@dataclass
class PipelineResult:
    """管线产出结果。"""

    entity_ids: list[str] = field(default_factory=list)
    character_cards: list[CharacterCard] = field(default_factory=list)
    lore_entries: list[LoreEntry] = field(default_factory=list)
    entity_count: int = 0
    relation_count: int = 0


# 依次尝试的编码：utf-8 系列优先，其次覆盖国内常见的 GBK/GB18030/Big5，
# 避免种子文本非 UTF-8 编码时用 errors="ignore" 静默产生乱码（进而污染角色名/世界观）。
_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030", "big5")


def _decode_text(raw: bytes, source: str = "") -> str:
    for enc in _ENCODING_CANDIDATES:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    logger.warning("无法可靠识别种子文本编码，使用 utf-8 replace 兜底: %s", source)
    return raw.decode("utf-8", errors="replace")


def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return chunks


class GraphRAGPipeline:
    """种子文本处理主管线。"""

    def __init__(self, project_id: str, config: GraphRAGConfig | None = None):
        self.project_id = project_id
        self.config = config or GraphRAGConfig()
        self.graph = GraphManager(project_id)
        self.extractor = EntityExtractor()
        self.persona_builder = PersonaBuilder()
        self.world_rules = WorldRulesExtractor()
        self._entity_index: dict[str, Entity] = {}

    async def run(
        self,
        seed_text_paths: list[str],
        progress: ProgressCallback = None,
        on_character: OnCharacterCallback = None,
    ) -> PipelineResult:
        """主管线入口。

        on_character: 每生成完一个角色卡时回调，参数 (card, done, total)，
        供上层（orchestrator）实时持久化与前端预览。
        """

        async def _report(stage: str, pct: float) -> None:
            logger.info("[GraphRAG] %s (%.0f%%)", stage, pct * 100)
            if progress:
                await progress(stage, pct)

        await _report("读取种子文本", 0.05)
        texts = self._read_texts(seed_text_paths)
        chunks: list[str] = []
        for t in texts:
            chunks.extend(_chunk_text(t, self.config.chunk_size, self.config.chunk_overlap))

        await _report("提取实体与关系", 0.2)

        async def _chunk_progress(i: int, n: int) -> None:
            await _report(f"提取实体与关系 ({i + 1}/{n} 块)", 0.2 + 0.3 * i / max(n, 1))

        entities, relations = await self.extractor.extract_many(
            chunks,
            progress_callback=_chunk_progress,
        )
        self._entity_index = {e.entity_id: e for e in entities}

        await _report("写入知识图谱", 0.5)
        await self.build_graph(entities, relations)

        # 角色卡阶段：先预报目标角色数量，再逐个生成并实时推进进度
        char_total = sum(1 for e in entities if e.entity_type == "Character")
        await _report(f"生成角色卡 (0/{char_total})", 0.6)
        full_context = "\n\n".join(texts)[:8000]

        async def _card_progress(card: CharacterCard, done: int, total: int) -> None:
            # 角色阶段占用进度区间 0.6 → 0.85
            pct = 0.6 + 0.25 * (done / max(total, 1))
            await _report(f"生成角色卡 ({done}/{total}) - {card.name}", pct)
            if on_character:
                await on_character(card, done, total)

        cards = await self.persona_builder.build_many(
            self.project_id, entities, full_context, on_card=_card_progress
        )

        await _report("提取世界规则", 0.9)
        lore = await self.world_rules.extract(texts)

        await _report("完成", 1.0)
        return PipelineResult(
            entity_ids=[e.entity_id for e in entities],
            character_cards=cards,
            lore_entries=lore,
            entity_count=len(entities),
            relation_count=len(relations),
        )

    def _read_texts(self, paths: list[str]) -> list[str]:
        texts: list[str] = []
        for p in paths:
            path = Path(p)
            if path.exists():
                texts.append(_decode_text(path.read_bytes(), source=str(path)))
            else:
                logger.warning("种子文本不存在: %s", p)
        return texts

    async def extract_entities(self, texts: list[str]) -> list[Entity]:
        entities, _ = await self.extractor.extract_many(texts)
        return entities

    async def build_graph(self, entities: list[Entity], relations: list[Relation]) -> None:
        """将实体关系写入 Kuzu 图。"""
        try:
            await self.graph.connect()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kuzu 不可用，跳过图谱写入：%s", exc)
            return
        for e in entities:
            await self.graph.add_entity(e)
        id_to_type = {e.entity_id: e.entity_type for e in entities}
        for r in relations:
            st = id_to_type.get(r.source_id, "Concept")
            tt = id_to_type.get(r.target_id, "Concept")
            await self.graph.add_relation(r, source_type=st, target_type=tt)

    async def generate_personas(self, entity_ids: list[str]) -> list[CharacterCard]:
        ents = [self._entity_index[i] for i in entity_ids if i in self._entity_index]
        return await self.persona_builder.build_many(self.project_id, ents)

    async def extract_world_rules(self, texts: list[str]) -> list[LoreEntry]:
        return await self.world_rules.extract(texts)
