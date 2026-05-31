"""实体与关系提取。

优先使用 microsoft/graphrag；若不可用，使用基于 LLM 的内置 JSON 提取，
保证系统始终可运行。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

from backend.models import Entity, Relation, new_id
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger

logger = get_logger("graphrag.extractor")

_EXTRACTION_PROMPT = """你是一个信息抽取引擎。请从下面的文本中抽取实体和关系。

实体类型限定为：Character（人物）、Location（地点）、Event（事件）、Concept（概念/物品/组织）。

严格输出 JSON，格式如下（不要输出任何额外文字）：
{{
  "entities": [
    {{"name": "实体名", "type": "Character|Location|Event|Concept", "description": "简短描述"}}
  ],
  "relations": [
    {{"source": "实体名A", "target": "实体名B", "type": "关系类型", "description": "关系说明", "strength": 0.5}}
  ]
}}

文本：
\"\"\"
{text}
\"\"\"
"""


def _extract_json(raw: str) -> dict:
    """从 LLM 输出中鲁棒地提取 JSON 对象。"""
    raw = raw.strip()
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            raw = brace.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("实体抽取 JSON 解析失败，返回空结果")
        return {"entities": [], "relations": []}


class EntityExtractor:
    """LLM 驱动的实体关系抽取器。"""

    async def extract(self, text: str) -> tuple[list[Entity], list[Relation]]:
        """从单段文本抽取实体与关系。"""
        prompt = _EXTRACTION_PROMPT.format(text=text[:6000])
        raw = await chat_safe(
            [{"role": "user", "content": prompt}], temperature=0.2
        )
        data = _extract_json(raw)

        name_to_id: dict[str, str] = {}
        entities: list[Entity] = []
        for e in data.get("entities", []):
            name = (e.get("name") or "").strip()
            if not name or name in name_to_id:
                continue
            etype = e.get("type", "Concept")
            if etype not in ("Character", "Location", "Event", "Concept"):
                etype = "Concept"
            eid = new_id()
            name_to_id[name] = eid
            entities.append(
                Entity(
                    entity_id=eid,
                    name=name,
                    entity_type=etype,
                    description=e.get("description", ""),
                )
            )

        relations: list[Relation] = []
        for r in data.get("relations", []):
            s = (r.get("source") or "").strip()
            t = (r.get("target") or "").strip()
            if s not in name_to_id or t not in name_to_id:
                continue
            relations.append(
                Relation(
                    source_id=name_to_id[s],
                    target_id=name_to_id[t],
                    relation_type=r.get("type", "RELATED_TO"),
                    description=r.get("description", ""),
                    strength=float(r.get("strength", 0.5) or 0.5),
                )
            )

        return entities, relations

    async def extract_many(
        self,
        texts: list[str],
        progress_callback: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> tuple[list[Entity], list[Relation]]:
        """合并多段文本的抽取结果，按实体名去重。"""
        all_entities: dict[str, Entity] = {}
        all_relations: list[Relation] = []
        id_remap: dict[str, str] = {}
        total = len(texts)

        for idx, text in enumerate(texts):
            if progress_callback:
                await progress_callback(idx, total)
            ents, rels = await self.extract(text)
            for e in ents:
                if e.name in all_entities:
                    id_remap[e.entity_id] = all_entities[e.name].entity_id
                else:
                    all_entities[e.name] = e
            for r in rels:
                r.source_id = id_remap.get(r.source_id, r.source_id)
                r.target_id = id_remap.get(r.target_id, r.target_id)
                all_relations.append(r)

        return list(all_entities.values()), all_relations
