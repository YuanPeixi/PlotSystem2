"""GraphRAG 管线测试：mock LLM 输出，验证实体/关系/角色卡解析。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.graphrag_pipeline.entity_extractor import EntityExtractor, _extract_json
from backend.graphrag_pipeline.persona_builder import PersonaBuilder


def test_extract_json_from_fenced():
    raw = '```json\n{"entities": [], "relations": []}\n```'
    data = _extract_json(raw)
    assert data == {"entities": [], "relations": []}


def test_extract_json_robust():
    raw = '前缀文字 {"entities": [{"name": "甲"}], "relations": []} 后缀'
    data = _extract_json(raw)
    assert data["entities"][0]["name"] == "甲"


@pytest.mark.asyncio
async def test_entity_extraction_parses_entities_and_relations():
    mock_output = """
    {
      "entities": [
        {"name": "萧无名", "type": "Character", "description": "剑客"},
        {"name": "客栈", "type": "Location", "description": "雨夜客栈"}
      ],
      "relations": [
        {"source": "萧无名", "target": "客栈", "type": "LOCATED_AT", "description": "在客栈", "strength": 0.8}
      ]
    }
    """
    extractor = EntityExtractor()
    with patch(
        "backend.graphrag_pipeline.entity_extractor.chat_safe",
        new=AsyncMock(return_value=mock_output),
    ):
        entities, relations = await extractor.extract("一些文本")

    assert len(entities) == 2
    names = {e.name for e in entities}
    assert {"萧无名", "客栈"} == names
    assert len(relations) == 1
    # 关系的 source/target 应已映射为实体 id
    ids = {e.entity_id for e in entities}
    assert relations[0].source_id in ids
    assert relations[0].target_id in ids


@pytest.mark.asyncio
async def test_persona_builder_parses_card():
    from backend.models import Entity

    mock_output = """
    {
      "persona": "冷峻剑客，背负血仇",
      "appearance": "黑衣断剑",
      "speech_style": "简短",
      "current_emotion": "警惕",
      "current_goal": "复仇",
      "known_facts": ["追查仇人"],
      "unknown_facts": ["仇人就在身边"]
    }
    """
    builder = PersonaBuilder()
    entity = Entity(entity_id="e1", name="萧无名", entity_type="Character", description="剑客")
    with patch(
        "backend.graphrag_pipeline.persona_builder.chat_safe",
        new=AsyncMock(return_value=mock_output),
    ):
        card = await builder.build("proj-1", entity)

    assert card.name == "萧无名"
    assert card.current_goal == "复仇"
    assert "追查仇人" in card.known_facts
    assert "仇人就在身边" in card.unknown_facts
