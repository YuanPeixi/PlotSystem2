"""工单30b：角色回复回放评测脚本。

钉住三件事：基线与真实的 `respond` 发出逐字相同的消息（否则比较的不是现行 prompt）；
所有变体都守住契约1（他人独白、私密细节、物件隐藏规则不进视图）；不带 `--run` 不调 LLM。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from tenacity import wait_none

from backend.agents.character_agent import CharacterAgent
from backend.memory import MemoryManager
from backend.models import (
    CharacterCard,
    CharacterState,
    DialogueTurn,
    Project,
    Scene,
    TurnKind,
    WorldObject,
)
from backend.scene_engine.engine import SceneEngine
from backend.services import repository
from backend.snapshot import SnapshotManager
from backend.utils import llm
from scripts import replay_character as rc

PID = "proj-30b"
HIDDEN_RULE = "隐藏规则：底座夹层只在月光下开启"
OTHER_THOUGHT = "她在撒谎"
OWN_THOUGHT = "我怕"
PRIVATE = "只有你感到指尖一凉"


async def _setup(
    scene_id: str = "s30b-0001",
    opening: str = "夜色压着王座厅。",
    own_action: str = "伸手碰王冠",
) -> Scene:
    await repository.save_project(Project(project_id=PID, name="玻璃王冠"))
    for cid, name in (("c1", "阿德里安"), ("c2", "塞芙拉")):
        await repository.save_character(
            CharacterCard(character_id=cid, project_id=PID, name=name, persona=f"{name}的人设",
                          current_emotion="平静")
        )
    await repository.save_object(
        WorldObject(object_id="o1", project_id=PID, name="王冠", public_description="镶玻璃的旧王冠",
                    hidden_rules=[HIDDEN_RULE], visibility="global")
    )
    snap = await SnapshotManager(PID).create_snapshot(
        scene_id=scene_id, branch_id="b30b",
        character_states={"c1": CharacterState(character_id="c1", current_emotion="警惕")},
        world_state_variables={"季节": "深冬"},
    )
    turns = [
        DialogueTurn(turn_id="t1", scene_id=scene_id, turn_number=1, character_id="c1",
                     character_name="阿德里安", action=own_action, dialogue="让我看看。",
                     inner_thought=OWN_THOUGHT),
        DialogueTurn(turn_id="t2", scene_id=scene_id, turn_number=2, kind=TurnKind.ENVIRONMENT.value,
                     character_name="环境", narration="王冠毫无动静。", private_detail=PRIVATE,
                     perceived_by=["c1"], source_turn_id="t1", source_action_index=0),
        DialogueTurn(turn_id="t3", scene_id=scene_id, turn_number=3, character_id="c2",
                     character_name="塞芙拉", dialogue="它从来不会回应凡人。", inner_thought=OTHER_THOUGHT),
        DialogueTurn(turn_id="t4", scene_id=scene_id, turn_number=4, character_id="c1",
                     character_name="阿德里安", dialogue="那就再试一次。"),
    ]
    scene = Scene(
        scene_id=scene_id, project_id=PID, branch_id="b30b", name="验冠", location="王座厅",
        description="两人检验王冠", participating_characters=["c1", "c2"], objects_present=["o1"],
        initial_conditions={"opening_narration": opening},
        snapshot_id_before=snap.snapshot_id, dialogue_log=turns, turns_completed=4,
    )
    await repository.save_scene(scene)
    return scene


async def _site(spec: str = "s30b:4", **kwargs) -> rc.ReplaySite:
    scenes = await repository.list_scenes(PID)
    return await rc.load_site(PID, spec, scenes=scenes, **kwargs)


@pytest.mark.asyncio
async def test_site_is_rebuilt_like_the_engine():
    await _setup()
    site = await _site()
    assert site.mode == "adjudicate"  # 有环境回合 → 推断为 adjudicate
    assert site.speaker.name == "阿德里安" and site.other_names == ["塞芙拉"]
    # 时点状态取前置快照，不是角色卡当前值
    assert site.speaker.card.current_emotion == "警惕"
    assert site.scene_context["季节"] == "深冬"
    assert site.extras["attempt_only"] is True and "王冠" in site.extras["objects_brief"]
    # 开场白一行 + 该轮之前的轮次，逐行与引擎相同；不含第 4 轮
    assert site.transcript == [
        "【旁白】夜色压着王座厅。",
        *(SceneEngine._turn_line(t) for t in site.prior),
    ]
    assert [t.turn_number for t in site.prior] == [1, 2, 3]


@pytest.mark.asyncio
async def test_baseline_sends_exactly_what_respond_sends():
    """基线必须是现行 prompt：与 `respond` 真实发出的消息逐字相同（记忆块同为空）。"""
    await _setup()
    site = await _site()
    expected = rc.build_base(site)
    sent = AsyncMock(return_value="好。")
    site.speaker._transcript_start = 0
    with patch("backend.agents.character_agent.chat_safe", sent):
        await site.speaker.respond(site.scene_context, site.transcript, **site.extras)
    assert sent.await_args.args[0] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(rc.VARIANTS))
async def test_no_variant_leaks_what_the_speaker_must_not_see(name):
    await _setup()
    site = await _site()
    text = "\n".join(m["content"] for m in rc.VARIANTS[name].build(site))
    assert HIDDEN_RULE not in text
    assert OTHER_THOUGHT not in text
    # 私密细节不进"目前对话"，哪怕执行者就是本人（R3）
    assert PRIVATE not in text
    # 本人的独白只在多轮结构里作为它自己说过的话还给它
    assert (OWN_THOUGHT in text) == name.endswith("C")


@pytest.mark.asyncio
async def test_variant_c_puts_own_turns_in_assistant_messages():
    await _setup()
    site = await _site()
    messages = rc.build_c(site)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[0] == rc.build_base(site)[0]
    assert "我怕" in messages[2]["content"] and "让我看看" in messages[2]["content"]
    last = messages[-1]["content"]
    assert last.startswith("【环境】王冠毫无动静。\n塞芙拉: 它从来不会回应凡人。")
    assert last.endswith("现在轮到你（阿德里安）发言，请按行为格式规范回应。")


@pytest.mark.asyncio
async def test_variant_b_view_has_no_speaker_or_label_lines():
    await _setup()
    site = await _site()
    assert rc.b_transcript(site) == [
        "※ 夜色压着王座厅。",
        "你（伸手碰王冠）：「让我看看。」",
        "※ 王冠毫无动静。",
        "塞芙拉：「它从来不会回应凡人。」",
    ]


@pytest.mark.asyncio
async def test_variant_a_fails_loudly_when_the_baseline_drifts():
    await _setup()
    site = await _site()
    messages = rc.build_base(site)
    messages[0]["content"] = messages[0]["content"].replace(rc._FORMAT_ANCHOR, "- 演好你自己")
    with pytest.raises(ValueError, match="锚点"):
        rc.apply_a(site, messages)


@pytest.mark.asyncio
async def test_judging_reuses_the_engine_truncation():
    await _setup()
    site = await _site()
    base, b = rc.VARIANTS["base"], rc.VARIANTS["B"]

    forged = rc.judge(site, base, "*俯身* 镜子不会说谎。 【环境】底座内侧确实藏着夹层。", 0)
    assert forged.polluted and forged.kept == "*俯身* 镜子不会说谎。"
    assert rc._cut_head(forged.cut) == "【环境】"

    # 名字后不跟冒号不是续写
    assert not rc.judge(site, base, "塞芙拉大人说得对。", 0).polluted
    # 只剩续写：截后为空
    assert rc.judge(site, base, "塞芙拉: 你错了。", 0).empty

    # 本人名字标签不是续写（30a 起初误判），夹着他人台词的照截
    own = rc.judge(site, base, "*起身*\n阿德里安: 那就再试一次。", 0)
    assert not own.polluted and own.kept == "*起身*\n那就再试一次。"
    assert rc.judge(site, base, "*起身*\n阿德里安: 再试。\n塞芙拉: 不行。", 0).polluted

    # B 视图的写法 30a 认不出，单列为变体特有形态，不算进污染
    copied = rc.judge(site, b, "我不信。 塞芙拉（冷笑）：「随你。」", 0)
    assert not copied.polluted and copied.extra_at > 0
    assert rc.judge(site, base, "我不信。 塞芙拉（冷笑）：「随你。」", 0).extra_at == -1


def _resp(text: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


@pytest.mark.asyncio
async def test_run_counts_pollution_failures_and_usage(monkeypatch, tmp_path):
    """计数走工单25 的计数器：每个变体一个，失败与重试都看得见。"""
    await _setup()
    site = await _site()

    async def create(**kwargs):
        await asyncio.sleep(0)
        # t0.5 的每次尝试都是空正文 → llm.py 退避重试 3 次后失败
        if kwargs["temperature"] == 0.5:
            return _resp("")
        return _resp("那就再试一次。 塞芙拉: 你会后悔的。")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_client", lambda base_url=None, api_key=None: client)
    monkeypatch.setattr(llm._complete.retry, "wait", wait_none())

    variants = [rc.VARIANTS["base"], rc.VARIANTS["t0.5"]]
    samples, meters = await rc.run_replay([site], variants, n=2, concurrency=2)
    stats = {s.variant: s for s in rc.summarize(samples, meters, variants)}
    assert (stats["base"].polluted, stats["base"].answered) == (2, 2)
    assert stats["base"].cut_heads == {"塞芙拉:": 2}
    assert (stats["t0.5"].failed, stats["t0.5"].answered) == (2, 0)
    assert (stats["base"].usage.calls, stats["t0.5"].usage.calls) == (2, 0)
    assert (stats["t0.5"].usage.retries, stats["t0.5"].usage.failures) == (4, 2)

    common = dict(memory="empty", n=2, model="m")
    md = rc.render_markdown([site], variants, samples, list(stats.values()), **common)
    assert "空记忆块" in md and "调用失败" in md and "你会后悔的" in md
    data = json.loads(rc.render_json([site], variants, samples, list(stats.values()), **common))
    assert len(data["samples"]) == 4


@pytest.mark.asyncio
async def test_preview_never_calls_the_llm(capsys):
    await _setup()
    args = argparse.Namespace(
        project=PID, site=["s30b:4"], variants="base,A,B,C,A+B,t0.5", n=5,
        memory="empty", mode=None, concurrency=4, run=False, out=None,
    )
    boom = AsyncMock(side_effect=AssertionError("预览不得调用 LLM"))
    with patch.object(llm, "chat", boom):
        assert await rc._main(args) == 0
    boom.assert_not_awaited()
    out = capsys.readouterr().out
    assert "将发起 30 次角色调用" in out and "--run" in out


@pytest.mark.asyncio
async def test_site_errors_are_explicit():
    await _setup()
    with pytest.raises(ValueError, match="环境回合"):
        await _site("s30b:2")
    with pytest.raises(ValueError, match="没有第 9 轮"):
        await _site("s30b:9")
    with pytest.raises(ValueError, match="SCENE_ID:TURN"):
        await _site("s30b")


@pytest.fixture
def fake_embedding(monkeypatch):
    """确定性假向量（同 test_branch_memory）：检索走真实 Chroma，但不调远程 embedding。"""
    pytest.importorskip("chromadb")
    from chromadb.api.types import Documents, EmbeddingFunction

    class _FakeEmbedding(EmbeddingFunction[Documents]):
        def __init__(self) -> None:
            pass

        def __call__(self, input: Documents):
            return [[float(len(t)), float(sum(map(ord, t)) % 997)] for t in input]

        @staticmethod
        def name() -> str:
            return "fake_test_embedding"

        def get_config(self) -> dict:
            return {}

        @staticmethod
        def build_from_config(config: dict) -> _FakeEmbedding:
            return _FakeEmbedding()

    monkeypatch.setattr("backend.memory.long_term.RemoteEmbeddingFunction", _FakeEmbedding)
    monkeypatch.setattr("backend.memory.embeddings.RemoteEmbeddingFunction", _FakeEmbedding)


@pytest.mark.asyncio
async def test_checkpoint_memory_without_memories_is_empty_with_a_note(fake_embedding):
    # 快照有没有向量库副本取决于此前的用例是否建过 chroma_db，两种都应是空记忆块 + 备注
    await _setup()
    site = await _site(memory="checkpoint")
    assert site.memory_context == []
    assert any("记忆" in n or "向量库" in n or "Chroma" in n for n in site.notes)


# ---------------------------------------------------------------------------
# 30b 评审的三处修复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_variant_c_does_not_repeat_history_containing_blank_lines():
    """开场白与动作里有空行：C 的末尾块不能靠"第一个空行"切，否则空行之后的历史会再出现一遍。"""
    await _setup("s30c-blank", opening="夜色压着王座厅。\n\n殿外下着雪。", own_action="伸手\n\n碰王冠")
    site = await _site("s30c:4")
    # 前提：历史里确实有空行（否则用例测不到被测分支）
    assert any("\n\n" in line for line in site.transcript)

    messages = rc.build_c(site)
    text = "\n".join(m["content"] for m in messages[1:])
    for snippet in ("殿外下着雪", "碰王冠", "王冠毫无动静", "它从来不会回应凡人"):
        assert text.count(snippet) == 1, snippet
    assert messages[-1]["content"].endswith(rc._instruction("阿德里安"))
    # 末尾块就是真实的 prompt_tail
    assert messages[-1]["content"].endswith(site.speaker.prompt_tail(site.memory_context))


async def _checkpoint_with_legacy_memory(pid: str, cid: str, fork_branch: str):
    """快照里有项目级老集合（含分叉点之后写进去的共享记忆），且 fork_branch 带分叉初始化凭据。"""
    from backend.config import settings
    from backend.utils.branch_memory import mark_fork_initialized

    legacy = MemoryManager(cid, pid, "")
    await legacy.connect()
    assert legacy.long_term._collection is not None, "前置条件：Chroma 必须真的连上"
    await legacy.long_term.add("分叉后：国王驾崩")
    mark_fork_initialized(settings.project_dir(pid) / "chroma_db", fork_branch)
    return await SnapshotManager(pid).create_snapshot(
        "s-ckpt", fork_branch, {cid: CharacterState(character_id=cid)}
    )


@pytest.mark.asyncio
async def test_checkpoint_memory_follows_production_adoption_rules(fake_embedding):
    """带分叉初始化凭据的分支不得回退项目级老集合（I3）；没有凭据的分支照生产代码承接。"""
    pid, cid = "proj-30b-ckpt", "char-ckpt"
    snap = await _checkpoint_with_legacy_memory(pid, cid, "b-fork")
    ckpt = Path(snap.chroma_checkpoint)
    before = {str(p.relative_to(ckpt)): p.read_bytes() for p in ckpt.rglob("*") if p.is_file()}

    forked, note = await rc._checkpoint_memory(snap, pid, cid, "b-fork", "国王")
    assert forked == [] and "没有可检索的记忆" in note
    # 对照：同一份快照、没有凭据的分支会承接老集合 —— 证明上面的空不是因为什么都读不到
    plain, _ = await rc._checkpoint_memory(snap, pid, cid, "b-plain", "国王")
    assert plain == ["分叉后：国王驾崩"]
    # 承接只写临时副本，权威快照逐字节不变（I2）
    after = {str(p.relative_to(ckpt)): p.read_bytes() for p in ckpt.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.asyncio
async def test_baseline_rebuilds_the_transcript_window_of_real_calls(monkeypatch):
    """超预算时窗口起点成块前推、之后沿用：回放必须复现真实调用当时的起点，而不是从 0 重算。

    "真实"取自真跑的 SceneEngine 每次发出的消息，不在用例里再推一遍算法。
    """
    from backend.config import settings
    from backend.models import SceneConfig

    monkeypatch.setattr(settings, "TRANSCRIPT_TOKEN_BUDGET", 100)
    pid, sid = "proj-30b-window", "s30w-0001"
    await repository.save_project(Project(project_id=pid, name="窗口"))
    cards = [CharacterCard(character_id=c, project_id=pid, name=n) for c, n in (("w1", "甲"), ("w2", "乙"))]
    for card in cards:
        await repository.save_character(card)
    agents = [CharacterAgent(card, MemoryManager(card.character_id, pid, "bw")) for card in cards]
    scene = Scene(
        scene_id=sid, project_id=pid, branch_id="bw", name="窗口", participating_characters=["w1", "w2"],
        initial_conditions={"opening_narration": "开场。"}, max_turns=20,
    )
    config = SceneConfig(name="窗口", participating_characters=["w1", "w2"], max_turns=20,
                         opening_narration="开场。")
    engine = SceneEngine(scene, config, agents, SnapshotManager(pid))

    sent: list[tuple[str, str]] = []  # (发言人, 实际发出的 user 消息)
    counter = iter(range(100))

    async def fake_chat(messages, **kwargs):
        name = messages[0]["content"].split("】")[0].removeprefix("你是【")
        sent.append((name, messages[1]["content"]))
        # 每行约 12 token：比预算的余量（100 − 75）小，才会出现"这次没超预算、沿用旧起点"的调用 ——
        # 行太长的话每次调用都重新裁剪，结果与从 0 重算相同，测不到被测分支
        return f"第{next(counter)}句：" + "风" * 8

    with (
        patch.object(CharacterAgent, "retrieve_relevant_memory", new=AsyncMock(return_value=[])),
        patch("backend.agents.character_agent.chat_safe", new=fake_chat),
        patch("backend.memory.memory_manager.MemoryManager.consolidate", new=AsyncMock()),
    ):
        await engine.run()
    await repository.save_scene(scene)

    scenes = await repository.list_scenes(pid)
    bitten = False
    for turn in scene.dialogue_log:
        site = await rc.load_site(pid, f"{sid}:{turn.turn_number}", scenes=scenes)
        real = [u for n, u in sent if n == turn.character_name][
            sum(1 for t in scene.dialogue_log[: turn.turn_number - 1] if t.character_id == turn.character_id)
        ]
        assert rc.build_base(site)[1]["content"] == real, turn.turn_number
        # 从 0 重算的话起点不同：至少有一轮用例确实咬到了被测分支
        naive = copy.copy(site.speaker)
        naive._transcript_start = 0
        if f"【目前对话】\n{naive._recent_transcript(site.transcript)}\n\n" not in real:
            bitten = True
    assert bitten, "所有轮次从 0 重算都与真实相同，用例没走到窗口沿用的分支"
