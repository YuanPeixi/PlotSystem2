"""揭示的执行者绑定、公开投射与容器物件（2026-11-07，玻璃王冠样例暴露的三个缺口）。

钉住：
- 绑定了执行者的揭示，别人的尝试在结构上命中不了（不进裁决器输入）；
- audience=present 的揭示内容进全场 transcript 与每个在场者的记忆，actor 的仍只给执行者；
- 对容器的动作连同在场的内容物一起裁决，允许对内容物写状态；容器只嵌一层；
- 导演按角色名写的触发者换成 id，匹配不上的整条丢弃（失败即收紧）。
"""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.agents.director_agent import DirectorAgent
from backend.agents.environment_agent import EnvironmentAgent, parse_world_adjudication
from backend.exceptions import InvalidRequestError
from backend.memory import MemoryManager
from backend.models import (
    ActionIntent,
    CharacterCard,
    DialogueTurn,
    RevealEntry,
    Scene,
    SceneConfig,
    WorldAdjudication,
    WorldObject,
    WorldOperation,
    WorldRevision,
)
from backend.scene_engine import SceneEngine
from backend.services.environment import normalize_environment_script, reveals_for_actor
from backend.services.objects import ObjectFields, apply_object_edit, clamp_object
from backend.services.world import apply_operations
from backend.snapshot import SnapshotManager
from backend.utils.turns import render_turn

CROWN = WorldObject(object_id="crown", name="玻璃王冠", visibility="global",
                    hidden_rules=["戴上时把佩戴者最强烈的记忆投射给在场所有人"])
NICHE = WorldObject(object_id="niche", name="底座夹层", visibility="private", known_by=["noan"],
                    hidden_rules=["夹层里藏着原始账页"], container_id="crown")
QUEEN = RevealEntry(condition="王后戴上", content="雪夜里两个襁褓被调换", actor_id="queen", audience="present")
PRINCESS = RevealEntry(condition="王女戴上", content="父王临终握着她的手", actor_id="isa", audience="present")
ANYONE = RevealEntry(condition="有人碰它", content="冰凉")


# ---------------------------------------------------------------------------
# 执行者绑定
# ---------------------------------------------------------------------------


def test_bound_reveals_only_reach_their_actor():
    entries = [QUEEN, PRINCESS, ANYONE]
    assert reveals_for_actor(entries, "isa") == [PRINCESS, ANYONE]
    assert reveals_for_actor(entries, "queen") == [QUEEN, ANYONE]
    assert reveals_for_actor(entries, "noan") == [ANYONE]


def test_script_normalization_keeps_binding_and_fails_closed_on_audience():
    raw = {"crown": [
        {"condition": "王后戴上", "content": "画面", "actor_id": "queen", "audience": "present"},
        {"condition": "王女戴上", "content": "画面", "actor_id": "isa", "audience": "everyone"},
        {"condition": "旧数据", "content": "画面"},
    ]}
    entries = normalize_environment_script(raw, {"crown"})["crown"]
    assert [(e.actor_id, e.audience) for e in entries] == [
        ("queen", "present"),
        # 非法取值不能放大可见范围
        ("isa", "actor"),
        ("", "actor"),
    ]


def test_director_actor_names_become_ids_and_unknown_names_are_dropped():
    cards = [CharacterCard(character_id="queen", name="塞芙拉"), CharacterCard(character_id="isa", name="伊莎贝尔")]
    raw = {"crown": [
        {"condition": "王后戴上", "actor": "塞芙拉", "audience": "present", "content": "a"},
        {"condition": "谁戴都行", "content": "b"},
        {"condition": "幽灵戴上", "actor": "不存在的人", "content": "c"},
    ]}
    bound = DirectorAgent._bind_reveal_actors(raw, cards)["crown"]
    assert [(e.get("actor_id", ""), e["content"]) for e in bound] == [("queen", "a"), ("", "b")]
    assert all("actor" not in e for e in bound)


# ---------------------------------------------------------------------------
# 公开投射
# ---------------------------------------------------------------------------


def _root() -> WorldRevision:
    return WorldRevision(project_id="p", definitions={o.object_id: deepcopy(o) for o in [CROWN, NICHE]})


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry", "public"), [(QUEEN, True), (RevealEntry(condition="王后戴上", content="雪夜里两个襁褓被调换"), False)])
async def test_audience_decides_where_the_reveal_goes(monkeypatch, entry, public):
    async def llm(messages, **kwargs):
        return '{"executable":true,"triggered":true,"narration":"王冠泛起微光。","reveal_index":1,"operations":[]}'

    monkeypatch.setattr("backend.agents.environment_agent.chat_safe", llm)
    result = await EnvironmentAgent().adjudicate_world(
        CROWN, "queen", "塞芙拉", ActionIntent(text="戴上王冠"), _root(), reveals=[entry]
    )
    assert (result.projection, result.private_detail) == (
        ("雪夜里两个襁褓被调换", "") if public else ("", "雪夜里两个襁褓被调换")
    )


@pytest.mark.asyncio
async def test_public_generated_memory_becomes_a_projection(monkeypatch):
    calls = []

    async def llm(messages, **kwargs):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            return ('{"executable":true,"triggered":true,"narration":"王冠泛起微光。","reveal_index":0,'
                    '"reveal_effect":"actor_memory_public","operations":[]}')
        return "幻象里，一名乳母抱着襁褓走进雪夜。"

    monkeypatch.setattr("backend.agents.environment_agent.chat_safe", llm)
    result = await EnvironmentAgent().adjudicate_world(
        CROWN, "adrian", "阿德里安", ActionIntent(text="戴上王冠"), _root(), actor_material="他见过乳母"
    )
    assert result.projection == "幻象里，一名乳母抱着襁褓走进雪夜。" and result.private_detail == ""
    # 生成调用仍然拿不到隐藏规则
    assert "投射给在场所有人" not in calls[1]


def test_free_text_effects_are_still_rejected():
    raw = {"executable": True, "triggered": True, "narration": "光", "reveal_effect": "actor_memory_public：王后换婴"}
    assert parse_world_adjudication(json.dumps(raw, ensure_ascii=False), {"crown"}) is None


def test_projection_is_rendered_for_everyone_and_old_turns_render_unchanged():
    env = DialogueTurn(kind="environment", narration="王冠泛起微光。", projection="雪夜里两个襁褓被调换",
                       private_detail="你认出了那张脸", perceived_by=["queen"])
    public = render_turn(env)
    assert "雪夜里两个襁褓被调换" in public and "你认出了那张脸" not in public
    # 没有投射的回合与之前逐字相同：记忆文本是长期记忆的寻址键
    plain = DialogueTurn(kind="environment", narration="冰凉，毫无动静。")
    assert render_turn(plain) == "【环境】冰凉，毫无动静。"


# ---------------------------------------------------------------------------
# 引擎：绑定过滤、内容物一并裁决、投射进全场记忆
# ---------------------------------------------------------------------------


def _agent(cid: str, name: str) -> CharacterAgent:
    agent = CharacterAgent(CharacterCard(character_id=cid, project_id="p", name=name), MemoryManager(cid, "p"))
    agent.retrieve_relevant_memory = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return agent


def _engine(adjudicator, agents, script, commits):
    revision = _root()
    scene = Scene(project_id="p", branch_id="b", participating_characters=[a.character_id for a in agents],
                  objects_present=["crown", "niche"], environment_script=script)

    async def commit(event, current_scene):
        commits.append(event)
        return apply_operations(revision, event.operations, event.event_id, event.observers,
                                source="environment", allowed_objects={"crown", "niche"})

    return SceneEngine(scene, SceneConfig(), agents, SnapshotManager("p"), objects=[CROWN, NICHE],
                       environment_mode="adjudicate", adjudicator=adjudicator,
                       world_revision=revision, world_commit=commit)


@pytest.mark.asyncio
async def test_engine_filters_reveals_by_actor_and_includes_contents(monkeypatch):
    isa, noan = _agent("isa", "伊莎贝尔"), _agent("noan", "诺安")
    adjudicator = EnvironmentAgent()
    adjudicate = AsyncMock(return_value=WorldAdjudication(
        narration="底座的接缝被撬开，露出一卷泛黄的纸。",
        operations=[WorldOperation(subject_kind="object", subject_id="niche", attribute="open",
                                   value=True, visibility="observable")],
    ))
    monkeypatch.setattr(adjudicator, "adjudicate_world", adjudicate)
    script = {"crown": [QUEEN, PRINCESS], "niche": [RevealEntry(condition="撬开夹层", content="账页")]}
    commits: list = []
    engine = _engine(adjudicator, [isa, noan], script, commits)
    source = DialogueTurn(turn_id="t1", character_id="isa", character_name="伊莎贝尔",
                          actions=[ActionIntent(object_id="crown", text="翻看王冠底座", status="pending")])
    turns = [source]
    await engine._adjudicate_pending(source, turns, [], None, AsyncMock())

    kwargs = adjudicate.await_args.kwargs
    # 王后那一条不在伊莎贝尔的裁决输入里；内容物的条目带上了它的名字
    assert [r.condition for r in kwargs["reveals"]] == ["王女戴上", "【底座夹层】撬开夹层"]
    assert kwargs["allowed_object_ids"] == {"crown", "niche"}
    assert [c.object_id for c in kwargs["contents"]] == ["niche"]
    # 对内容物的状态变化被接受并提交
    assert source.actions[0].status == "resolved" and len(commits) == 1
    assert commits[0].operations[0].subject_id == "niche"


@pytest.mark.asyncio
async def test_projection_reaches_every_present_character(monkeypatch):
    queen, isa = _agent("queen", "塞芙拉"), _agent("isa", "伊莎贝尔")
    adjudicator = EnvironmentAgent()
    monkeypatch.setattr(adjudicator, "adjudicate_world", AsyncMock(return_value=WorldAdjudication(
        triggered=True, narration="王冠泛起微光。", projection="雪夜里两个襁褓被调换")))
    engine = _engine(adjudicator, [queen, isa], {"crown": [QUEEN]}, [])
    source = DialogueTurn(turn_id="t1", character_id="queen", character_name="塞芙拉",
                          actions=[ActionIntent(object_id="crown", text="戴上王冠", status="pending")])
    turns, transcript = [source], []
    await engine._adjudicate_pending(source, turns, transcript, None, AsyncMock())

    env = turns[-1]
    assert env.projection == "雪夜里两个襁褓被调换" and env.private_detail is None
    assert "雪夜里两个襁褓被调换" in transcript[-1]
    for agent in (queen, isa):
        assert any("雪夜里两个襁褓被调换" in m for m in agent.memory.short_term.recent())


@pytest.mark.asyncio
async def test_round_trip_keeps_projection():
    from backend.services.repository import _deserialize_turn
    from backend.utils.serializer import to_dict

    env = DialogueTurn(turn_id="e1", kind="environment", narration="光", projection="画面")
    assert _deserialize_turn(to_dict(env)).projection == "画面"
    assert _deserialize_turn({"turn_id": "e0", "kind": "environment"}).projection is None


# ---------------------------------------------------------------------------
# 容器校验（只嵌一层）
# ---------------------------------------------------------------------------


def _edit(current, container, containers):
    return apply_object_edit(
        current, ObjectFields(name="底座夹层", container_id=container, visibility="hidden"),
        project_id="p", object_id="niche", base_revision=current.revision if current else 0,
        request_id="r", character_names=set(), character_ids=set(), containers=containers,
    )


def test_container_must_exist_and_only_one_level_deep():
    obj, changed = _edit(None, "crown", {"crown": ""})
    assert changed and obj.container_id == "crown"
    with pytest.raises(InvalidRequestError, match="不存在"):
        _edit(None, "ghost", {"crown": ""})
    with pytest.raises(InvalidRequestError, match="自己"):
        _edit(None, "niche", {"crown": ""})
    with pytest.raises(InvalidRequestError, match="一层"):
        _edit(None, "crown", {"crown": "altar"})
    # 自己里面已经装着东西，就不能再放进别的容器
    with pytest.raises(InvalidRequestError, match="一层"):
        _edit(None, "crown", {"crown": "", "ledger": "niche"})


def test_read_side_drops_self_containment_but_keeps_dangling_ids():
    obj = WorldObject(object_id="niche", name="夹层", container_id="niche")
    assert clamp_object(obj) and obj.container_id == ""
    dangling = WorldObject(object_id="niche", name="夹层", container_id="ghost")
    clamp_object(dangling)
    # 不核对容器是否存在：读一个物件不扫整个目录，悬空的由消费方跳过
    assert dangling.container_id == "ghost"


def test_old_clients_replay_digest_is_unchanged():
    fields = ObjectFields(name="王冠", aliases=[], public_description="", hidden_rules=[],
                          visibility="global", known_by=[])
    assert fields.digest(0) == ObjectFields(**{**vars(fields), "container_id": None}).digest(0)
    assert fields.digest(0) != ObjectFields(**{**vars(fields), "container_id": "crown"}).digest(0)
