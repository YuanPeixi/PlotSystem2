"""工单29：世界设定的可见性判定与分发。

核心断言：种子里只有部分角色知道的秘密，不得进不知情角色的 system prompt；
任何判定失败都按"不发给任何角色"收紧，绝不退回 global。
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.graphrag_pipeline.pipeline import PipelineResult
from backend.graphrag_pipeline.world_rules import _FACT_CHARS as _CLASSIFIER_FACT_CHARS
from backend.graphrag_pipeline.world_rules import _MAX_FACTS as _CLASSIFIER_MAX_FACTS
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
async def test_classify_withholds_public_when_unknown_facts_truncated_by_count():
    """某角色 unknown_facts 条数超过裁剪阈值时，分类器看不全"谁不知道"的证据。

    证明本该不知道这件事的角色的那条 unknown_fact，可能恰好在被裁掉的那一截——
    分类器因此可能误判 public，把设定发给它。此时哪怕分类器真的返回了 public，
    也必须整条收紧为不发，而不是信任一份建立在不完整证据上的"公开"结论。
    """
    cards = _cards()
    cards[0].unknown_facts = [f"无关事实{i}" for i in range(_CLASSIFIER_MAX_FACTS + 1)]
    with patch(_CLASSIFY_LLM, new=_llm_reply([{"index": 0, "visibility": "public"}])):
        [verdict] = await LoreVisibilityClassifier().classify(_entries()[:1], cards)

    assert verdict.visibility == WITHHELD
    assert lore_for_character([verdict], "c-isa") == []


@pytest.mark.asyncio
async def test_classify_withholds_public_when_unknown_fact_truncated_by_length():
    """超长度阈值与超条数阈值是两条独立的裁剪，必须分别测——只测一条会漏掉另一条回归。"""
    cards = _cards()
    cards[0].unknown_facts = ["超长的秘密" * _CLASSIFIER_FACT_CHARS]
    with patch(_CLASSIFY_LLM, new=_llm_reply([{"index": 0, "visibility": "public"}])):
        [verdict] = await LoreVisibilityClassifier().classify(_entries()[:1], cards)

    assert verdict.visibility == WITHHELD


@pytest.mark.asyncio
async def test_classify_truncation_safety_net_does_not_touch_private_or_hidden():
    """证据不全只会让"公开"结论变得可疑，不该连带收紧 private/hidden。

    known_facts 被裁只会让 private 的知情者名单偏少（方向本身安全，漏发不是泄密），
    不该因为某个角色的 unknown_facts 超长就把本来正常判定的 private/hidden 条目也废掉——
    否则证据一旦不全，整批判定形同虚设，而不是只收紧真正有风险的那一类。
    """
    cards = _cards()
    cards[0].unknown_facts = [f"无关事实{i}" for i in range(_CLASSIFIER_MAX_FACTS + 1)]
    reply = [
        {"index": 0, "visibility": "private", "known_by": ["阿德里安"]},
        {"index": 1, "visibility": "hidden"},
    ]
    with patch(_CLASSIFY_LLM, new=_llm_reply(reply)):
        verdicts = await LoreVisibilityClassifier().classify(_entries()[1:3], cards)

    assert [v.visibility for v in verdicts] == [PRIVATE, HIDDEN]
    assert verdicts[0].holders == ["c-adr"]


@pytest.mark.asyncio
async def test_classify_does_not_withhold_public_when_evidence_is_complete():
    """安全网不能误伤：证据没被裁剪时，public 判定要照常放行。"""
    with patch(_CLASSIFY_LLM, new=_llm_reply([{"index": 0, "visibility": "public"}])):
        [verdict] = await LoreVisibilityClassifier().classify(_entries()[:1], _cards())

    assert verdict.visibility == PUBLIC


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
async def test_classify_withholds_entry_when_index_repeated_even_if_first_is_public():
    """重复 index 不能靠"挑第一项"消化掉——哪怕第一项恰好是看起来最安全的 public。

    旧实现用 setdefault 悄悄留下第一项：分类器先说 public、又自相矛盾地说 hidden，
    结果仍以 public 把条目发给全员。重复本身就是判定不可信的信号，不看两次给出的
    结论是否碰巧一致。
    """
    reply = [{"index": 0, "visibility": "public"}, {"index": 0, "visibility": "hidden"}]
    with patch(_CLASSIFY_LLM, new=_llm_reply(reply)):
        [verdict] = await LoreVisibilityClassifier().classify(_entries()[:1], _cards())

    assert verdict.visibility == WITHHELD
    assert not verdict.distributed


@pytest.mark.asyncio
async def test_classify_duplicate_index_does_not_affect_other_entries_in_batch():
    """一个 index 重复，不能连带把同批里其他正常解析的条目也判不可信。"""
    reply = [
        {"index": 0, "visibility": "public"},
        {"index": 0, "visibility": "hidden"},
        {"index": 1, "visibility": "public"},
    ]
    with patch(_CLASSIFY_LLM, new=_llm_reply(reply)):
        verdicts = await LoreVisibilityClassifier().classify(_entries()[:2], _cards())

    assert verdicts[0].visibility == WITHHELD
    assert verdicts[1].visibility == PUBLIC


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
async def test_write_backup_retries_on_collision_without_overwriting(monkeypatch):
    """排他创建踩到已存在的文件名必须换个新名重试，不能退化成覆盖写。

    随机后缀只是把碰撞概率压低，不是消除——`--apply` 失败重跑、或同一进程里
    连续调用，都可能撞上同一个 uuid4 前缀。这里强制第一次尝试撞车，断言第二次
    尝试换了后缀、且第一份备份的内容原样未变。
    """
    project_id = "p-lore-collision"
    await _legacy_project(project_id)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 0, 0, 0)

    monkeypatch.setattr(reclassify_lore, "datetime", _FrozenDatetime)

    project_dir = reclassify_lore.settings.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    collided_path = project_dir / "lore_backup_20260101-000000_aaaaaaaa.json"
    collided_path.write_text("别人的旧备份，不许动", encoding="utf-8")

    hexes = iter(["aaaaaaaa11111111", "bbbbbbbb22222222"])
    monkeypatch.setattr(reclassify_lore, "uuid4", lambda: SimpleNamespace(hex=next(hexes)))

    cards = sorted(await repository.list_characters(project_id), key=lambda c: c.name)
    path = reclassify_lore._write_backup(project_id, cards)

    assert path.name == "lore_backup_20260101-000000_bbbbbbbb.json"
    assert collided_path.read_text(encoding="utf-8") == "别人的旧备份，不许动"


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
