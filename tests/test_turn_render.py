"""轮次渲染与感知判定的收敛（工单24/20 PR-0）。

PR-0 是纯重构：把散在各处的 transcript 式渲染收成 `render_turn`，把"这一轮我能感知到什么"
收成 `perceive`。这里的 golden 字符串是重构**之前**各入口的逐字输出 —— 长期记忆按正文
sha256 寻址（`long_term.memory_id`），记忆文本差一个空格就是一条新记录，续跑重放时会
与已入库的旧文本对不上、重复写入。
"""

from __future__ import annotations

import pytest

from backend.agents.director_agent import DirectorAgent
from backend.agents.summary_agent import _transcript_lines as summary_lines
from backend.memory.episodic import EpisodicMemory
from backend.memory.memory_manager import MemoryManager
from backend.models import DialogueTurn, TurnKind
from backend.scene_engine.engine import SceneEngine
from backend.scene_engine.termination import check_termination


def _turn(**kw) -> DialogueTurn:
    base = {"turn_number": 1, "character_id": "c1", "character_name": "甲"}
    base.update(kw)
    return DialogueTurn(**base)


FULL = _turn(action="拔出剑", dialogue="跟我走", inner_thought="他在怀疑我")
DIALOGUE_ONLY = _turn(dialogue="跟我走")
ACTION_ONLY = _turn(action="拔出剑")
THOUGHT_ONLY = _turn(inner_thought="他在怀疑我")
EMPTY = _turn()

# (轮次, 不含独白, 含独白)
GOLDEN = [
    (FULL, "甲: *拔出剑* 跟我走", "甲: *拔出剑* 跟我走 [他在怀疑我]"),
    (DIALOGUE_ONLY, "甲: 跟我走", "甲: 跟我走"),
    (ACTION_ONLY, "甲: *拔出剑*", "甲: *拔出剑*"),
    (THOUGHT_ONLY, "甲: ", "甲: [他在怀疑我]"),
    (EMPTY, "甲: ", "甲: "),
]


@pytest.mark.parametrize(("turn", "public", "_full"), GOLDEN)
def test_engine_transcript_line_is_stripped_public_view(turn, public, _full):
    """角色看到的"目前对话"：无独白，且去掉首尾空白（空轮次是 "甲:" 而不是 "甲: "）。"""
    assert SceneEngine._turn_line(turn) == public.strip()


@pytest.mark.parametrize(("turn", "_public", "full"), GOLDEN)
def test_director_transcript_keeps_inner_thought(turn, _public, full):
    """导演有全知权，评估用的对白带独白。"""
    assert DirectorAgent._transcript_lines([turn]) == [full]


@pytest.mark.parametrize(("turn", "public", "full"), GOLDEN)
def test_summary_transcript_follows_flag(turn, public, full):
    assert summary_lines([turn]) == [public]
    assert summary_lines([turn], include_thoughts=True) == [full]


@pytest.mark.asyncio
@pytest.mark.parametrize(("turn", "public", "full"), GOLDEN)
async def test_memory_text_strips_inner_thought_for_others(turn, public, full):
    """记忆文本不去首尾空白：它是长期记忆的寻址键，改一个字符就是一条新记录。"""
    owner = MemoryManager("c1", "p1")
    await owner.add_experience(turn)
    assert owner.short_term.dump() == [full]
    assert owner.short_term.dump_with_meta()[0][1]["is_self"] is True

    other = MemoryManager("c2", "p1")
    await other.add_experience(turn)
    assert other.short_term.dump() == [public]
    assert other.short_term.dump_with_meta()[0][1]["is_self"] is False


def test_episodic_entry_format_is_frozen():
    """事件摘要是序列化格式，老快照与重放去重都靠逐字相同（设计单 R9），不并入 render_turn。"""
    turn = _turn(action="拔出剑", dialogue="我发誓要走", inner_thought="其实我怕")
    ep = EpisodicMemory("c1")
    assert ep.record(turn) is True
    assert ep._events == ["[重要] 甲: （拔出剑） 我发誓要走"]


@pytest.mark.parametrize(
    ("turn", "viewer"),
    [
        (_turn(dialogue="今天天气不错", inner_thought="我要背叛他"), "c1"),  # 自己：独白参与判定
        (_turn(dialogue="今天天气不错", inner_thought="我要背叛他"), "c2"),  # 他人：独白不参与
        (_turn(action="拔出剑\n指向门口", dialogue="我发誓要走。"), "c1"),
        (_turn(action="拔出剑\n指向门口", dialogue="我发誓要走。"), "c2"),
        (_turn(dialogue="平平无奇"), "c1"),
    ],
)
def test_live_record_and_replay_produce_identical_entries(turn, viewer):
    """现场写入与续跑重放必须逐字一致，否则去重失效、条目膨胀（工单26 复盘 9/10）。

    PR-0 之前两条路径各判一次"能不能看见独白"；收敛到 `perceive` 之后，
    任何一边单独改判定都会让这条变红。
    """
    live = EpisodicMemory(viewer)
    live.record(turn)
    replayed = EpisodicMemory(viewer)
    replayed.replay([turn])
    assert replayed._events == live._events


def test_turn_kind_defaults_to_character():
    assert DialogueTurn().kind == TurnKind.CHARACTER.value


def _env(n: int) -> DialogueTurn:
    return _turn(turn_number=n, character_id="", character_name="环境", kind=TurnKind.ENVIRONMENT.value)


def test_termination_counts_character_turns_only():
    """max_turns 只数角色轮次（设计单 §5.5）；环境回合由 PR-2 引入，口径先行。"""
    chars = [_turn(turn_number=i, dialogue=f"第{i}句") for i in range(3)]
    mixed = [chars[0], _env(1), chars[1], _env(2), chars[2]]
    assert check_termination(mixed, max_turns=4) == (False, "")
    assert check_termination(mixed, max_turns=3)[0] is True


def test_stall_detection_ignores_environment_turns():
    """连续的环境回合不能被判成"对话停滞"，也不能打断角色轮次的停滞判定。"""
    same = [_turn(turn_number=i, dialogue="嗯。") for i in range(4)]
    env_run = [_turn(turn_number=0, dialogue="开场"), *[_env(i) for i in range(1, 5)]]
    assert check_termination(env_run, max_turns=99) == (False, "")
    interleaved = [same[0], _env(1), same[1], _env(2), same[2], same[3]]
    assert check_termination(interleaved, max_turns=99) == (True, "对话停滞")


@pytest.mark.asyncio
async def test_round_robin_does_not_count_environment_turns():
    """轮询按角色轮次数取模：插进环境回合后，下一位仍是轮到的那个角色。"""
    from types import SimpleNamespace

    from backend.models import SceneConfig, SpeakerMode

    engine = object.__new__(SceneEngine)
    engine.agents = [SimpleNamespace(name="甲"), SimpleNamespace(name="乙")]
    engine.config = SceneConfig(speaker_mode=SpeakerMode.ROUND_ROBIN.value)
    engine._selector = None
    engine._unknown_mode_warned = False

    one_spoken = [_turn(turn_number=1, dialogue="开场")]
    agent, _ = await engine._select_speaker([], one_spoken)
    assert agent.name == "乙"
    agent, _ = await engine._select_speaker([], [*one_spoken, _env(2), _env(3), _env(4)])
    assert agent.name == "乙", "环境回合占了发言顺序"


def test_turn_kind_deserialization_is_lenient():
    """旧轮次缺键即角色轮次；非法取值也按角色轮次 —— 当成环境回合会让它从计数里消失。"""
    from backend.services.repository import _turn_kind

    assert _turn_kind(None) == TurnKind.CHARACTER.value
    assert _turn_kind("environment") == TurnKind.ENVIRONMENT.value
    assert _turn_kind("bogus") == TurnKind.CHARACTER.value


@pytest.mark.parametrize("raw", [[], {}, ["environment"], {"k": 1}, 1, True, 1.5])
def test_turn_kind_non_string_falls_back_to_character(raw):
    """非字符串取值也只降级、不抛异常：[] / {} 不可哈希，直接拿去查集合会 TypeError。"""
    from backend.services.repository import _turn_kind

    assert _turn_kind(raw) == TurnKind.CHARACTER.value


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [[], {}])
async def test_corrupt_turn_kind_does_not_break_scene_list(raw):
    """一条坏轮次不能让整个项目的场景列表五百（评审复现：基线读取成功，PR 读取失败）。"""
    from backend.models import Project, Scene
    from backend.services import repository

    project_id = f"proj-bad-kind-{type(raw).__name__}"
    await repository.save_project(Project(project_id=project_id, name=project_id))
    scene = Scene(
        scene_id=f"scene-bad-kind-{type(raw).__name__}",
        project_id=project_id,
        branch_id="branch-main",
        dialogue_log=[_turn(dialogue="跟我走", kind=raw), _turn(turn_number=2, dialogue="好")],
    )
    await repository.save_scene(scene)

    scenes = await repository.list_scenes(project_id)
    assert [t.kind for t in scenes[0].dialogue_log] == [TurnKind.CHARACTER.value] * 2
    loaded = await repository.get_scene(scene.scene_id)
    assert loaded.dialogue_log[0].kind == TurnKind.CHARACTER.value


# ---------------------------------------------------------------------------
# 环境回合（工单20 PR-2a C2）：渲染、感知、事件摘要
# ---------------------------------------------------------------------------


def _env_turn(narration="王冠亮起微光", detail="她看见了母亲的脸", perceived=("c1",)) -> DialogueTurn:
    return DialogueTurn(
        turn_number=2, character_id="", character_name="环境", kind=TurnKind.ENVIRONMENT.value,
        narration=narration, private_detail=detail, perceived_by=list(perceived),
        source_turn_id="t1", source_action_index=0,
    )


ENV = _env_turn()
ENV_PUBLIC = "【环境】王冠亮起微光"
ENV_FULL = "【环境】王冠亮起微光（私密：她看见了母亲的脸）"


def test_environment_turn_in_the_transcript_is_narration_only():
    """R3："目前对话"只写公开叙述，私密细节不进 transcript（它会进全场每个角色的 prompt）。"""
    assert SceneEngine._turn_line(ENV) == ENV_PUBLIC


def test_director_sees_the_private_detail():
    assert DirectorAgent._transcript_lines([ENV]) == [ENV_FULL]


def test_summary_private_detail_follows_the_thoughts_flag():
    assert summary_lines([ENV]) == [ENV_PUBLIC]
    assert summary_lines([ENV], include_thoughts=True) == [ENV_FULL]


def test_environment_turn_without_detail_renders_narration_only():
    bare = _env_turn(detail=None, perceived=())
    assert DirectorAgent._transcript_lines([bare]) == [ENV_PUBLIC]


@pytest.mark.asyncio
async def test_only_the_perceiver_remembers_the_private_detail():
    """A6：当事人 = 公开叙述 + 私密细节拼成一条；其他在场角色只记公开叙述。"""
    actor = MemoryManager("c1", "p1")
    await actor.add_experience(ENV)
    assert actor.short_term.dump() == [ENV_FULL]
    text, meta = actor.short_term.dump_with_meta()[0]
    assert meta["is_self"] is False and meta["speaker"] == "环境"

    bystander = MemoryManager("c2", "p1")
    await bystander.add_experience(ENV)
    assert bystander.short_term.dump() == [ENV_PUBLIC]


def test_perception_of_character_turns_never_grants_private_detail():
    from backend.utils.turns import perceive

    assert perceive(FULL, "c1").private_detail is False
    assert perceive(ENV, "c1").private_detail is True
    assert perceive(ENV, "c2").private_detail is False
    assert perceive(ENV, "").private_detail is False
    assert perceive(ENV, "c1").is_self is False  # 环境回合没有说话人


def test_episodic_environment_entry_never_carries_the_private_detail():
    turn = _env_turn(narration="暗格开启，露出一封信", detail="信上写着王后的秘密")
    actor = EpisodicMemory("c1")
    assert actor.record(turn) is True
    assert actor._events == ["[重要] 环境: 暗格开启，露出一封信"]


def test_bystander_importance_does_not_read_the_private_detail():
    """与"他人独白不参与判定"同理：关键词只在私密细节里时，只有当事人把它记成重要事件。"""
    turn = _env_turn(narration="王冠亮起微光", detail="她明白了真相")
    assert EpisodicMemory("c1").record(turn) is True
    assert EpisodicMemory("c2").record(turn) is False


@pytest.mark.parametrize(
    ("turn", "viewer"),
    [
        (_env_turn(narration="暗格开启", detail="她明白了真相"), "c1"),
        (_env_turn(narration="暗格开启", detail="她明白了真相"), "c2"),
        (_env_turn(narration="王冠映出一场战争"), "c2"),
        (_env_turn(narration="王冠\n毫无动静"), "c1"),
    ],
)
def test_live_and_replay_agree_on_environment_turns(turn, viewer):
    live = EpisodicMemory(viewer)
    live.record(turn)
    replayed = EpisodicMemory(viewer)
    replayed.replay([turn])
    assert replayed._events == live._events


@pytest.mark.asyncio
async def test_selector_local_signals_skip_environment_turns(monkeypatch):
    """点名检测与重复发言惩罚只看角色轮次：环境回合没有发言者，叙述里提到谁也不算点名。"""
    from unittest.mock import AsyncMock

    from backend.agents.character_agent import CharacterAgent
    from backend.models import CharacterCard
    from backend.scene_engine import speaker_selector
    from backend.scene_engine.speaker_selector import ScoringSpeakerSelector

    agents = [
        CharacterAgent(CharacterCard(character_id=cid, project_id="p", name=name), MemoryManager(cid, "p"))
        for cid, name in (("c1", "甲"), ("c2", "乙"))
    ]
    sel = ScoringSpeakerSelector(agents)
    seen: dict[str, list] = {}
    monkeypatch.setattr(sel, "_detect_addressed_ids", lambda turns: seen.setdefault("addressed", turns) and set())
    monkeypatch.setattr(sel, "_repeat_penalties", lambda turns: seen.setdefault("penalties", turns) and {})
    monkeypatch.setattr(speaker_selector, "chat_safe", AsyncMock(side_effect=Exception("不打分")))

    spoken = _turn(dialogue="乙，你来")
    await sel.select(["甲: 乙，你来", ENV_PUBLIC], [spoken, _env_turn(narration="乙的身影映在王冠上")])
    assert seen["addressed"] == [spoken]
    assert seen["penalties"] == [spoken]
