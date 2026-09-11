"""CharacterAgent 测试：核心验证信息不对称原则与上下文窗口策略。"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.config import settings
from backend.memory import MemoryManager
from backend.memory.long_term import LongTermMemory, memory_id
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
async def test_consolidate_memory_does_not_duplicate_write():
    """工单15：写入已在 SceneEngine 每轮实时完成，consolidate_memory 只应
    固化缓冲，不应再对轮次重新调用 add_experience（否则同一句台词会入库两次）。"""
    agent = _make_agent()
    with (
        patch.object(agent.memory, "add_experience", new=AsyncMock()) as mock_add,
        patch.object(agent.memory, "consolidate", new=AsyncMock()) as mock_consolidate,
    ):
        await agent.consolidate_memory()

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


# ---------------------------------------------------------------------------
# 工单26：长期记忆写入幂等（与水位线无关的兜底）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_experience_does_not_auto_consolidate():
    """缓冲写满不得再自行固化：触发权已收归 SceneEngine（工单26）。

    旧实现在这里 consolidate 会绕过 Scene.turns_consolidated 水位线，
    崩溃续跑就把已入库的轮次二次写入长期记忆。
    """
    mem = MemoryManager("char-noauto", "proj-noauto")
    mem.short_term.capacity = 2
    mem.short_term._buffer = deque(maxlen=2)
    mem.short_term._meta = deque(maxlen=2)

    turns = [
        DialogueTurn(turn_number=i, character_id="char-noauto", character_name="甲", dialogue=f"第{i}句")
        for i in range(3)
    ]
    with patch.object(mem, "consolidate", new=AsyncMock()) as mock_consolidate:
        for turn in turns:
            await mem.add_experience(turn)

    mock_consolidate.assert_not_called()


@pytest.mark.asyncio
async def test_long_term_add_is_idempotent():
    """同一角色写入同一段文本两次，库里只应有一条（工单26 §3.2）。

    用替身集合而非真 Chroma：真集合的写入要调远程 embedding，离线/CI 跑不通
    （契约6）。这里校验的正是我们自己那段逻辑——按内容寻址 + 先查后写。
    """

    class _FakeCollection:
        def __init__(self):
            self.docs: dict[str, str] = {}
            self.upserts = 0

        def get(self, ids=None, include=None, **_kwargs):
            hit = [i for i in (ids or []) if i in self.docs]
            return {"ids": hit}

        def upsert(self, ids, documents, metadatas=None, **_kwargs):
            self.upserts += 1
            for i, doc in zip(ids, documents):
                self.docs[i] = doc

    mem = LongTermMemory("char-idem", "proj-idem", "b-idem")
    fake = _FakeCollection()
    mem._collection = fake

    await mem.add("甲: 我发誓要复仇", {"type": "episodic"})
    await mem.add("甲: 我发誓要复仇", {"type": "episodic"})
    await mem.add("乙: 我不信", {"type": "dialogue"})

    assert list(fake.docs) == [memory_id("甲: 我发誓要复仇"), memory_id("乙: 我不信")]
    # 重复的那次必须在 get 处就短路，不能进 upsert——upsert 会重算 embedding（计费）
    assert fake.upserts == 2


@pytest.mark.asyncio
async def test_long_term_add_is_idempotent_in_fallback():
    """降级路径必须与 Chroma 路径同语义，否则契约6 的两条路会分叉（红线 R2）。"""
    mem = LongTermMemory("char-idem-fb", "proj-idem-fb", "b-idem-fb")
    mem._collection = None  # 强制走降级内存存储

    await mem.add("甲: 我发誓要复仇", {"type": "episodic"})
    await mem.add("甲: 我发誓要复仇", {"type": "episodic"})
    await mem.add("乙: 我不信", {"type": "dialogue"})

    hits = await mem.retrieve("复仇", top_k=5)
    assert sum(1 for h in hits if h.text == "甲: 我发誓要复仇") == 1
    assert len(mem._fallback) == 2


@pytest.mark.asyncio
async def test_long_term_add_falls_back_without_duplicating():
    """Chroma 写入失败转入降级时也走同一条幂等路径，不得绕开去重。"""
    mem = LongTermMemory("char-idem-mix", "proj-idem-mix", "b-idem-mix")

    class _Boom:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    mem._collection = _Boom()
    await mem.add("甲: 我发誓要复仇", {"type": "episodic"})
    assert mem._collection is None  # 已转入降级
    await mem.add("甲: 我发誓要复仇", {"type": "episodic"})

    assert len(mem._fallback) == 1
