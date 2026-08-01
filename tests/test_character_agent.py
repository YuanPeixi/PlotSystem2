"""CharacterAgent 测试：核心验证信息不对称原则与上下文窗口策略。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.config import settings
from backend.memory import MemoryManager
from backend.models import CharacterCard, DialogueTurn, LoreEntry, RelationshipState


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


def _make_agent() -> CharacterAgent:
    card = _make_card()
    return CharacterAgent(card, MemoryManager(card.character_id, card.project_id))


def test_system_prompt_omits_dynamic_memory_by_default():
    """工单14：检索到的记忆每轮都变，默认不得进 system（否则 prefix cache 每轮失效）。"""
    agent = _make_agent()
    prompt = agent.build_system_prompt({"description": "雨夜"})
    assert "相关记忆" not in prompt

    # AutoGen 路径仍可显式注入
    prompt_with_mem = agent.build_system_prompt({"description": "雨夜"}, ["三年前的火光"])
    assert "三年前的火光" in prompt_with_mem


def test_system_prompt_contains_static_scene_brief():
    """场景信息整场不变，应放入 system 而非每轮重建的 user 消息。"""
    agent = _make_agent()
    prompt = agent.build_system_prompt(
        {"name": "客栈对峙", "location": "悦来客栈", "description": "雨夜", "opening_narration": "雷声炸响"}
    )
    assert "客栈对峙" in prompt
    assert "悦来客栈" in prompt
    assert "雷声炸响" in prompt


def test_recent_transcript_keeps_history_beyond_old_12_line_limit():
    """工单14：预算充足时不再按固定 12 行截断，整场对话应完整可见。"""
    agent = _make_agent()
    transcript = [f"甲: 第{i}句台词" for i in range(30)]
    text = agent._recent_transcript(transcript)
    assert "第0句台词" in text
    assert "第29句台词" in text
    assert "已省略" not in text


def test_recent_transcript_trims_in_blocks_and_keeps_start_stable(monkeypatch):
    """超预算时成块丢弃最早内容，并在后续轮次沿用同一起点，保持 prompt 前缀稳定。"""
    monkeypatch.setattr(settings, "TRANSCRIPT_TOKEN_BUDGET", 1000)
    agent = _make_agent()
    transcript = ["甲: " + "字" * 60 for _ in range(20)]

    first = agent._recent_transcript(transcript)
    start = agent._transcript_start
    assert start > 0  # 已触发块式丢弃
    assert "已省略" in first

    # 新增一轮：仍在预算内，起点不应继续前移（否则前缀每轮都变）
    second = agent._recent_transcript([*transcript, "乙: 新的一句"])
    assert agent._transcript_start == start
    assert second.startswith(first)  # 新内容只在末尾追加


@pytest.mark.asyncio
async def test_update_state_after_scene_does_not_duplicate_write():
    """工单15：写入已在 SceneEngine 每轮实时完成，update_state_after_scene 只应
    固化缓冲，不应再对 scene_log 重新调用 add_experience（否则同一句台词会入库两次）。"""
    agent = _make_agent()
    turns = [
        DialogueTurn(turn_number=1, character_id="char-1", character_name="萧无名", dialogue="你好")
    ]
    with (
        patch.object(agent.memory, "add_experience", new=AsyncMock()) as mock_add,
        patch.object(agent.memory, "consolidate", new=AsyncMock()) as mock_consolidate,
    ):
        await agent.update_state_after_scene(turns)

    mock_add.assert_not_called()
    mock_consolidate.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_memory_manager_consolidate_writes_each_entry_once():
    """工单15：重要事件不再另外写一份正文副本，consolidate 按条写入一次，
    仅通过 metadata['type'] 标记重要程度。"""
    mem = MemoryManager("char-dedup", "proj-dedup")
    turn = DialogueTurn(
        turn_number=1, character_id="char-dedup", character_name="甲", dialogue="我发誓要复仇"
    )
    await mem.add_experience(turn)  # 命中“发誓”重要关键词

    with patch.object(mem.long_term, "add", new=AsyncMock()) as mock_add:
        await mem.consolidate(force=True)

    mock_add.assert_awaited_once()
    _, meta = mock_add.call_args.args
    assert meta["type"] == "episodic"


def test_recent_transcript_respects_line_window_override(monkeypatch):
    """RECENT_TRANSCRIPT_WINDOW > 0 时退回按行数限制（可配置，不改代码即可调整）。"""
    monkeypatch.setattr(settings, "RECENT_TRANSCRIPT_WINDOW", 3)
    agent = _make_agent()
    transcript = [f"甲: 第{i}句" for i in range(10)]
    text = agent._recent_transcript(transcript)
    assert "第9句" in text
    assert "第6句" not in text
