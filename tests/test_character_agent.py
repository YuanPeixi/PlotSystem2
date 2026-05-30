"""CharacterAgent 测试：核心验证信息不对称原则。"""

from __future__ import annotations

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.memory import MemoryManager
from backend.models import CharacterCard, LoreEntry, RelationshipState


def _make_card() -> CharacterCard:
    return CharacterCard(
        character_id="char-1",
        project_id="proj-1",
        name="萧无名",
        persona="冷峻的剑客",
        speech_style="简短有力",
        known_facts=["我在追查灭门仇人"],
        unknown_facts=["柳如烟认识我的仇人"],  # 不应出现在 prompt
        current_emotion="警惕",
        current_goal="找到真凶",
        world_lore_entries=[
            LoreEntry(content="江湖讲究快意恩仇", keywords=["江湖"], scope="global"),
            LoreEntry(content="毒手判官行踪诡秘", keywords=["毒"], scope="character:other"),
        ],
        relationships={
            "char-2": RelationshipState(target_character_id="char-2", relation_type="敌对", strength=-0.6)
        },
    )


@pytest.mark.asyncio
async def test_system_prompt_excludes_unknown_facts():
    card = _make_card()
    mem = MemoryManager(card.character_id, card.project_id)
    agent = CharacterAgent(card, mem)
    prompt = agent.build_system_prompt({"name": "客栈", "location": "客栈", "description": "雨夜"})

    # 已知事实应包含
    assert "我在追查灭门仇人" in prompt
    # 绝对禁止：unknown_facts 不得出现
    assert "柳如烟认识我的仇人" not in prompt
    # 角色名应包含
    assert "萧无名" in prompt


@pytest.mark.asyncio
async def test_lore_scope_filtering():
    card = _make_card()
    mem = MemoryManager(card.character_id, card.project_id)
    agent = CharacterAgent(card, mem)
    prompt = agent.build_system_prompt({"description": "江湖恩怨"})

    # global lore 注入
    assert "快意恩仇" in prompt
    # 属于其他角色的 lore 不应注入
    assert "毒手判官行踪诡秘" not in prompt


def test_safe_agent_name():
    card = _make_card()
    mem = MemoryManager(card.character_id, card.project_id)
    agent = CharacterAgent(card, mem)
    name = agent._safe_agent_name()
    # AutoGen agent 名须为 ASCII 标识符（字母开头、仅含字母数字下划线）
    assert name.replace("_", "").isalnum()
    assert name.isascii()
    assert name[0].isalpha()
