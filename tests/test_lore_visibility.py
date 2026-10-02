"""工单29：世界设定的可见性判定与分发。

核心断言：种子里只有部分角色知道的秘密，不得进不知情角色的 system prompt；
任何判定失败都按"不发给任何角色"收紧，绝不退回 global。
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.graphrag_pipeline.pipeline import PipelineResult
from backend.graphrag_pipeline.world_rules import (
    HIDDEN,
    PRIVATE,
    PUBLIC,
    WITHHELD,
    LoreVerdict,
    LoreVisibilityClassifier,
    lore_for_character,
)
from backend.memory import MemoryManager
from backend.models import CharacterCard, LoreEntry, Project
from backend.services import orchestrator, repository
from scripts import reclassify_lore

_CLASSIFY_LLM = "backend.graphrag_pipeline.world_rules.chat_safe"

# 《玻璃王冠》的缩写：三条设定分属不同知情范围
_CEREMONY = "北境王国即将举行继承仪式，王女伊莎贝尔是唯一继承人。"
_CROWN = "玻璃王冠不会验证血统，而是读取佩戴者最强烈的记忆并投射给在场所有人。"
_ADOPTED = "伊莎贝尔并非国王的亲生女儿。"


def _cards(project_id: str = "p-lore") -> list[CharacterCard]:
    return [
        CharacterCard(
            character_id="c-isa",
            project_id=project_id,
            name="伊莎贝尔",
            known_facts=["自己是唯一继承人"],
            unknown_facts=["自己并非国王亲生", "王冠读取记忆"],
        ),
        CharacterCard(
            character_id="c-adr",
            project_id=project_id,
            name="阿德里安",
            known_facts=["王冠读取佩戴者记忆"],
            unknown_facts=["婴儿后来成为伊莎贝尔"],
        ),
        CharacterCard(
            character_id="c-sev",
            project_id=project_id,
            name="塞芙拉",
            known_facts=["伊莎贝尔并非国王亲生"],
            unknown_facts=["王冠读取记忆"],
        ),
    ]


def _entries() -> list[LoreEntry]:
    return [
        LoreEntry(lore_id="l-ceremony", content=_CEREMONY, keywords=["继承仪式"]),
        LoreEntry(lore_id="l-crown", content=_CROWN, keywords=["王冠"], priority=8),
        LoreEntry(lore_id="l-adopted", content=_ADOPTED, keywords=["伊莎贝尔"], priority=8),
    ]


def _llm_reply(items: list[dict]) -> AsyncMock:
    return AsyncMock(return_value=json.dumps(items, ensure_ascii=False))


_GOOD_REPLY = [
    {"index": 0, "visibility": "public", "known_by": []},
    {"index": 1, "visibility": "private", "known_by": ["阿德里安"]},
    {"index": 2, "visibility": "private", "known_by": ["塞芙拉"]},
]


def _by_id(verdicts: list[LoreVerdict]) -> dict[str, LoreVerdict]:
    return {v.entry.lore_id: v for v in verdicts}


# ---------------------------------------------------------------------------
# 分类器
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_maps_names_to_holders():
    with patch(_CLASSIFY_LLM, new=_llm_reply(_GOOD_REPLY)):
        verdicts = _by_id(await LoreVisibilityClassifier().classify(_entries(), _cards()))

    assert verdicts["l-ceremony"].visibility == PUBLIC
    assert verdicts["l-crown"].visibility == PRIVATE
    assert verdicts["l-crown"].holders == ["c-adr"]
    assert verdicts["l-adopted"].holders == ["c-sev"]


@pytest.mark.asyncio
async def test_classify_prompt_carries_each_characters_unknown_facts():
    """证据就是每个角色的已知/未知事实；不给的话分类器只能凭种子原文猜。"""
    mock = _llm_reply(_GOOD_REPLY)
    with patch(_CLASSIFY_LLM, new=mock):
        await LoreVisibilityClassifier().classify(_entries(), _cards(), "种子原文")

    prompt = mock.call_args.args[0][0]["content"]
    assert "【伊莎贝尔】" in prompt
    assert "自己并非国王亲生" in prompt  # 伊莎贝尔的未知事实
    assert _CROWN in prompt


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param([], id="漏判"),
        pytest.param([{"index": 0, "visibility": "secret"}], id="取值非法"),
        pytest.param([{"index": 0, "visibility": "private", "known_by": ["王子"]}], id="知情者全对不上"),
        pytest.param(
            [{"index": 0, "visibility": "private", "known_by": ["塞芙拉", "王子"]}],
            id="知情者部分对不上",
        ),
        pytest.param([{"index": 0, "visibility": "private", "known_by": "塞芙拉"}], id="known_by不是列表"),
        pytest.param([{"index": True, "visibility": "public"}], id="index是bool"),
        pytest.param([{"index": "0", "visibility": "public"}], id="index是字符串"),
    ],
)
@pytest.mark.asyncio
async def test_classify_failures_are_withheld_from_everyone(reply):
    entry = LoreEntry(lore_id="l-x", content=_ADOPTED)
    with patch(_CLASSIFY_LLM, new=_llm_reply(reply)):
        [verdict] = await LoreVisibilityClassifier().classify([entry], _cards())

    assert verdict.visibility == WITHHELD
    assert not verdict.distributed
    for card in _cards():
        assert lore_for_character([verdict], card.character_id) == []


@pytest.mark.asyncio
async def test_classify_bool_index_does_not_shadow_real_index_one():
    """`True == 1`：bool 被当成下标的话，会抢走第 1 条的判定。"""
    reply = [
        {"index": 0, "visibility": "hidden"},
        {"index": True, "visibility": "public"},
        {"index": 1, "visibility": "private", "known_by": ["阿德里安"]},
    ]
    entries = _entries()[:2]
    with patch(_CLASSIFY_LLM, new=_llm_reply(reply)):
        verdicts = await LoreVisibilityClassifier().classify(entries, _cards())

    assert [v.visibility for v in verdicts] == [HIDDEN, PRIVATE]


@pytest.mark.asyncio
async def test_classify_call_failure_withholds_whole_batch():
    with patch(_CLASSIFY_LLM, new=AsyncMock(side_effect=RuntimeError("timeout"))):
        verdicts = await LoreVisibilityClassifier().classify(_entries(), _cards())

    assert {v.visibility for v in verdicts} == {WITHHELD}


@pytest.mark.asyncio
async def test_classify_withholds_entire_entry_on_partial_name_match():
    """一个解析不出的知情者，不能靠"其余名字凑巧匹配上"就被采信。

    这不是宽松处理可以接受的噪声：known_by 里任何一个名字对不上，都说明这份判定
    本身信不过，必须整条不发——跟全对不上时的处理一样严格（工单29 §3.2，与调用失败、
    取值非法同属一类失败，不是"部分成功"）。
    """
    reply = [{"index": 0, "visibility": "private", "known_by": ["塞芙拉", "国王"]}]
    with patch(_CLASSIFY_LLM, new=_llm_reply(reply)):
        [verdict] = await LoreVisibilityClassifier().classify(_entries()[2:], _cards())

    assert verdict.visibility == WITHHELD
    assert lore_for_character([verdict], "c-sev") == []


@pytest.mark.asyncio
async def test_classify_batches_large_inputs():
    entries = [LoreEntry(lore_id=f"l-{i}", content=f"设定{i}") for i in range(45)]
    mock = AsyncMock(
        side_effect=lambda messages, **_: json.dumps(
            [{"index": i, "visibility": "public"} for i in range(20)]
        )
    )
    with patch(_CLASSIFY_LLM, new=mock):
        verdicts = await LoreVisibilityClassifier().classify(entries, _cards())

    assert mock.await_count == 3
    # 每批的 index 都从 0 起，不能被错配到别的批次上
    assert all(v.visibility == PUBLIC for v in verdicts)
    assert len(verdicts) == 45


def test_lore_for_character_copies_private_entries_per_holder():
    entry = LoreEntry(lore_id="l-shared", content="两人共知的秘密")
    verdicts = [LoreVerdict(entry, PRIVATE, ["c-adr", "c-sev"])]

    adr = lore_for_character(verdicts, "c-adr")
    sev = lore_for_character(verdicts, "c-sev")
    assert [e.scope for e in adr] == ["character:c-adr"]
    assert [e.scope for e in sev] == ["character:c-sev"]
    assert lore_for_character(verdicts, "c-isa") == []
    assert entry.scope == "global"  # 分发不得改动判定里的原条目


# ---------------------------------------------------------------------------
# 读取侧最后一道
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    ["character:伊莎贝尔", "secret", "character:x-c-isa", ""],
)
def test_select_lore_rejects_any_scope_but_global_or_exact_own(scope):
    card = _cards()[0]
    card.world_lore_entries = [LoreEntry(content=_ADOPTED, keywords=["伊莎贝尔"], scope=scope)]
    agent = CharacterAgent(card, MemoryManager(card.character_id, card.project_id))

    prompt = agent.build_system_prompt({"description": "伊莎贝尔走向王座"})
    assert _ADOPTED not in prompt


# ---------------------------------------------------------------------------
# 端到端：构建分发 → 角色 system prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_keeps_secrets_out_of_uninformed_prompts():
    project_id = "p-lore-build"
    await repository.save_project(Project(project_id=project_id, name="玻璃王冠"))
    cards = _cards(project_id)
    entries = _entries()
    with patch(_CLASSIFY_LLM, new=_llm_reply(_GOOD_REPLY)):
        verdicts = await LoreVisibilityClassifier().classify(entries, cards)
    result = PipelineResult(
        character_cards=cards, lore_entries=entries, lore_verdicts=verdicts
    )

    class _FakePipeline:
        def __init__(self, _project_id):
            pass

        async def run(self, *_args, **_kwargs):
            return result

    with patch.object(orchestrator, "GraphRAGPipeline", _FakePipeline):
        await orchestrator.run_graphrag(project_id)

    # 场景上下文必须命中秘密的关键词，否则 _select_lore 本来就不会选中它
    context = {"name": "继承仪式", "description": "伊莎贝尔戴上王冠"}

    async def prompt_of(cid: str) -> str:
        card = await repository.get_character(project_id, cid)
        return CharacterAgent(card, MemoryManager(cid, project_id)).build_system_prompt(context)

    isa = await prompt_of("c-isa")
    adr = await prompt_of("c-adr")
    sev = await prompt_of("c-sev")

    assert _CEREMONY in isa and _CEREMONY in adr and _CEREMONY in sev
    assert _CROWN in adr
    assert _CROWN not in isa and _CROWN not in sev
    assert _ADOPTED in sev
    assert _ADOPTED not in isa and _ADOPTED not in adr

    status = orchestrator._build_status[project_id]
    assert status["lore_withheld"] == 0


# ---------------------------------------------------------------------------
# 已有项目迁移
# ---------------------------------------------------------------------------


async def _legacy_project(project_id: str) -> None:
    """旧构建的产物：全部设定以 global 复制进每张卡，同一 lore_id。"""
    await repository.save_project(Project(project_id=project_id, name="旧项目"))
    for card in _cards(project_id):
        card.world_lore_entries = _entries()
        if card.character_id == "c-isa":
            card.world_lore_entries.append(
                LoreEntry(lore_id="l-own", content="伊莎贝尔的私人设定", scope="character:c-isa")
            )
        await repository.save_character(card)


@pytest.mark.asyncio
async def test_reclassify_preview_does_not_write(tmp_data_dir):
    project_id = "p-lore-preview"
    await _legacy_project(project_id)
    mock = _llm_reply(_GOOD_REPLY)
    with patch(_CLASSIFY_LLM, new=mock):
        report = await reclassify_lore.reclassify_project(project_id)

    # 三张卡上同一 lore_id 的副本只判一次
    assert len(report.verdicts) == 3
    isa = await repository.get_character(project_id, "c-isa")
    assert len(isa.world_lore_entries) == 4
    assert report.backup_path is None
    assert not list((tmp_data_dir / "projects" / project_id).glob("lore_backup_*.json"))


@pytest.mark.asyncio
async def test_reclassify_apply_redistributes_and_backs_up():
    project_id = "p-lore-apply"
    await _legacy_project(project_id)
    with patch(_CLASSIFY_LLM, new=_llm_reply(_GOOD_REPLY)):
        report = await reclassify_lore.reclassify_project(project_id, apply=True)

    def contents(card: CharacterCard) -> set[str]:
        return {e.content for e in card.world_lore_entries}

    isa = await repository.get_character(project_id, "c-isa")
    adr = await repository.get_character(project_id, "c-adr")
    sev = await repository.get_character(project_id, "c-sev")
    assert contents(isa) == {_CEREMONY, "伊莎贝尔的私人设定"}
    assert contents(adr) == {_CEREMONY, _CROWN}
    assert contents(sev) == {_CEREMONY, _ADOPTED}
    assert {e.scope for e in sev.world_lore_entries} == {"global", "character:c-sev"}

    backup = json.loads(report.backup_path.read_text(encoding="utf-8"))
    assert len(backup["c-adr"]["world_lore_entries"]) == 3
    assert any(e["content"] == _ADOPTED for e in backup["c-isa"]["world_lore_entries"])


@pytest.mark.asyncio
async def test_reclassify_apply_twice_in_same_second_keeps_both_backups(monkeypatch):
    """秒级时间戳本身会撞车：两次 --apply 落在同一秒，后一次不能覆盖前一次的备份。

    判定有随机性，两次运行移除的条目可能不同——覆盖掉就是真的丢了第一次的备份
    （工单29 §3.4 的备份要求因此落空），不是"反正内容一样，覆盖也无妨"。
    用冻结的时钟逼出撞车，不依赖两次调用恰好落在同一秒这种偶然性。
    """

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 0, 0, 0)

    monkeypatch.setattr(reclassify_lore, "datetime", _FrozenDatetime)

    project_id = "p-lore-apply-twice"
    await _legacy_project(project_id)
    with patch(_CLASSIFY_LLM, new=_llm_reply(_GOOD_REPLY)):
        first = await reclassify_lore.reclassify_project(project_id, apply=True)
        second = await reclassify_lore.reclassify_project(project_id, apply=True)

    assert first.backup_path != second.backup_path
    assert first.backup_path.exists()
    assert second.backup_path.exists()


@pytest.mark.asyncio
async def test_reclassify_apply_aborts_on_call_failure():
    """有人在场时，调用失败就整个不写、让人重试，不把设定从所有卡上撤掉。"""
    project_id = "p-lore-fail"
    await _legacy_project(project_id)
    with patch(_CLASSIFY_LLM, new=AsyncMock(side_effect=RuntimeError("down"))):
        report = await reclassify_lore.reclassify_project(project_id, apply=True)

    assert report.aborted
    assert report.backup_path is None
    adr = await repository.get_character(project_id, "c-adr")
    assert len(adr.world_lore_entries) == 3


@pytest.mark.asyncio
async def test_reclassify_apply_removes_unresolvable_entries_instead_of_keeping_global():
    """判了但判不出（知情者对不上）不是重试能解决的，照常收紧：移除，而不是原样留着 global。"""
    project_id = "p-lore-unresolved"
    await _legacy_project(project_id)
    reply = [
        {"index": 0, "visibility": "public"},
        {"index": 1, "visibility": "private", "known_by": ["王子"]},
        {"index": 2, "visibility": "secret"},
    ]
    with patch(_CLASSIFY_LLM, new=_llm_reply(reply)):
        report = await reclassify_lore.reclassify_project(project_id, apply=True)

    assert not report.aborted
    for cid in ("c-isa", "c-adr", "c-sev"):
        card = await repository.get_character(project_id, cid)
        assert _CROWN not in {e.content for e in card.world_lore_entries}
        assert _ADOPTED not in {e.content for e in card.world_lore_entries}
    assert report.backup_path.exists()
