"""工单20 PR-2a C4–C6：环境裁决接入引擎、角色侧视野、编排层的落盘与推送。

《玻璃王冠》缩写：王冠全员可见、暗格只有诺安知道、铜镜谁都不知道。每条用例对应设计单的
一道防护（§5、R1–R8、A15 / A24 / A25 / A27 / A34 / A35），撤掉那道防护时用例必须变红。

LLM 一律按 `purpose` 分派（同一个假 chat_safe 装在四个调用点上）：既能分别给角色、抽取、
裁决、selector 排队回复，又能按用途检查每一次调用实际收到的 prompt。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.agents.environment_agent import EnvironmentAgent
from backend.config import settings
from backend.exceptions import LLMError
from backend.memory import MemoryManager
from backend.models import (
    ENVIRONMENT_SPEAKER,
    ActionIntent,
    CharacterCard,
    DialogueTurn,
    EnvironmentMode,
    LLMPurpose,
    Project,
    Scene,
    SceneConfig,
    SceneEvaluation,
    TurnKind,
    WorldObject,
    environment_turn_id,
)
from backend.scene_engine import SceneEngine
from backend.services import events, orchestrator, repository
from backend.services.environment import Adjudication, remaining_environment_quota
from backend.services.objects import describe_objects_for, visible_to
from backend.snapshot import SnapshotManager

_CALL_SITES = (
    "backend.agents.character_agent.chat_safe",
    "backend.scene_engine.action_intents.chat_safe",
    "backend.agents.environment_agent.chat_safe",
    "backend.scene_engine.speaker_selector.chat_safe",
)
ADJ = EnvironmentMode.ADJUDICATE.value

_CROWN_RULE = "隐藏规则甲：戴上后投射佩戴者最强烈的记忆"
_NICHE_RULE = "隐藏规则乙：按顺序踩三块地砖才开"
_MIRROR_RULE = "隐藏规则丙：照见的是三天后的人"
CROWN = WorldObject(
    object_id="o-crown", project_id="p-env", name="玻璃王冠", aliases=["王冠"],
    public_description="一顶通体透明的玻璃王冠。", hidden_rules=[_CROWN_RULE], visibility="global",
)
NICHE = WorldObject(
    object_id="o-niche", project_id="p-env", name="暗格", aliases=["祭坛暗格"],
    public_description="祭坛下松动的地砖。", hidden_rules=[_NICHE_RULE],
    visibility="private", known_by=["c2"],
)
MIRROR = WorldObject(
    object_id="o-mirror", project_id="p-env", name="铜镜", aliases=[],
    public_description="墙上蒙尘的铜镜。", hidden_rules=[_MIRROR_RULE], visibility="hidden",
)
_RULES = (_CROWN_RULE, _NICHE_RULE, _MIRROR_RULE)


def _extracted(*items: dict) -> str:
    return json.dumps(list(items), ensure_ascii=False)


def _attempt(index: int = 0, code: str = "o1", verb: str = "戴上") -> dict:
    return {"index": index, "object": code, "is_attempt": True, "verb": verb, "detail": ""}


def _verdict(narration: str = "王冠微微发亮。", changes: dict | None = None, triggered: bool = True) -> str:
    return json.dumps({
        "executable": True, "triggered": triggered, "reveal_index": 0,
        "narration": narration, "state_changes": changes or {},
    }, ensure_ascii=False)


class Router:
    """按用途排队回复，记下每次调用的 (用途, messages)。队列空了给一个无害的缺省回复。"""

    _DEFAULTS = {
        LLMPurpose.CHARACTER.value: "*沉默*",
        LLMPurpose.ACTION_EXTRACT.value: "[]",
        LLMPurpose.SELECTOR.value: '{"urge": 5, "relevance": 5, "initiative": 5}',
        LLMPurpose.ADJUDICATE.value: _verdict(),
    }

    def __init__(self, **queues: list):
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls: list[tuple[str, list[dict]]] = []

    async def __call__(self, messages, *, purpose=LLMPurpose.UNTAGGED, **_kw):
        key = purpose.value
        self.calls.append((key, messages))
        queue = self.queues.get(key)
        result = queue.pop(0) if queue else self._DEFAULTS.get(key, "")
        if isinstance(result, Exception):
            raise result
        return result

    def of(self, purpose: LLMPurpose) -> list[list[dict]]:
        return [m for p, m in self.calls if p == purpose.value]

    def __enter__(self):
        self._patches = [patch(site, new=self) for site in _CALL_SITES]
        self._patches.append(patch.object(CharacterAgent, "retrieve_relevant_memory",
                                          new=AsyncMock(return_value=[])))
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


def _agent(cid: str, name: str, project_id: str = "p-env") -> CharacterAgent:
    card = CharacterCard(character_id=cid, project_id=project_id, name=name)
    return CharacterAgent(card, MemoryManager(cid, project_id))


def _cast():
    return [_agent("c1", "伊莎贝尔"), _agent("c2", "诺安")]


def _engine(scene_id: str, *, mode: str = ADJ, max_turns: int = 2, agents=None,
            objects=(CROWN, NICHE, MIRROR), speaker_mode: str = "round_robin", scene: Scene | None = None):
    agents = agents or _cast()
    scene = scene or Scene(scene_id=scene_id, project_id="p-env", branch_id="b",
                           objects_present=[o.object_id for o in objects])
    config = SceneConfig(name="加冕前夜", location="王城", participating_characters=[a.character_id for a in agents],
                         max_turns=max_turns, speaker_mode=speaker_mode)
    engine = SceneEngine(scene, config, agents, SnapshotManager(scene.project_id),
                         objects=list(objects), environment_mode=mode,
                         adjudicator=EnvironmentAgent() if mode == ADJ else None)
    return engine, agents


def _pending_scene(scene_id: str, actions: list[ActionIntent], extra: list[DialogueTurn] = ()) -> Scene:
    """已落盘、还没裁决完的一轮：模拟裁决途中崩溃后的库内状态。"""
    source = DialogueTurn(turn_id="t1", scene_id=scene_id, turn_number=1, character_id="c1",
                          character_name="伊莎贝尔", action="戴上王冠", actions=actions)
    log = [source, *extra]
    return Scene(scene_id=scene_id, project_id="p-env", branch_id="b",
                 objects_present=["o-crown", "o-niche", "o-mirror"], max_turns=1,
                 dialogue_log=log, turns_completed=len(log), turns_consolidated=len(log))


def _pending(index: int = 0, object_id: str = "o-crown") -> ActionIntent:
    return ActionIntent(index=index, text="戴上王冠", object_id=object_id, verb="戴上", status="pending")


def _character_prompts(router: Router) -> list[tuple[str, str]]:
    return [(m[0]["content"], m[1]["content"]) for m in router.of(LLMPurpose.CHARACTER)]


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_quota_counts_environment_turns_and_pending_but_not_failed_or_skipped():
    """A15：pending 必须计入，否则续跑补裁决后超限；failed / skipped 不会再产生回合。"""
    turns = [
        DialogueTurn(actions=[ActionIntent(status="pending"), ActionIntent(status="failed"),
                              ActionIntent(status="skipped"), ActionIntent(status="resolved")]),
        DialogueTurn(kind=TurnKind.ENVIRONMENT.value),
    ]
    assert remaining_environment_quota(turns, 3) == 1
    assert remaining_environment_quota(turns, 1) == -1


def test_objects_brief_follows_visibility_and_never_carries_hidden_rules():
    """A24：global 全员、private 只给 known_by、hidden 谁都不给；非法可见性按 hidden。"""
    bogus = WorldObject(object_id="o-x", name="木匣", public_description="一只木匣。", visibility="public")
    objects = [CROWN, NICHE, MIRROR, bogus]
    c1, _ = describe_objects_for(objects, "c1")
    c2, _ = describe_objects_for(objects, "c2")
    assert c1 == "- 玻璃王冠：一顶通体透明的玻璃王冠。"
    assert c2 == "- 玻璃王冠：一顶通体透明的玻璃王冠。\n- 暗格：祭坛下松动的地砖。"
    assert not visible_to(bogus, "c1")
    assert all(rule not in c1 + c2 for rule in _RULES)


def test_objects_brief_drops_whole_entries_past_the_budget():
    many = [WorldObject(object_id=f"o{i}", name=f"物件{i}", public_description="描" * 80, visibility="global")
            for i in range(5)]
    text, dropped = describe_objects_for(many, "c1", budget_tokens=150)
    assert text and dropped
    assert len(text.splitlines()) + len(dropped) == 5
    assert dropped == [f"物件{i}" for i in range(5 - len(dropped), 5)], "从排在后面的物件起整条丢弃"


# ---------------------------------------------------------------------------
# 引擎主循环（C4）
# ---------------------------------------------------------------------------


def test_adjudicate_mode_requires_an_injected_adjudicator():
    """R8：引擎不自己建裁决器。"""
    scene = Scene(scene_id="s-env-noadj", project_id="p-env", branch_id="b")
    with pytest.raises(ValueError):
        SceneEngine(scene, SceneConfig(name="x"), _cast(), SnapshotManager("p-env"),
                    objects=[CROWN], environment_mode=ADJ)


@pytest.mark.asyncio
async def test_resolved_attempt_appends_an_environment_turn_then_the_next_speaker_sees_it():
    engine, _ = _engine("s-env-happy")
    log: list[tuple] = []

    async def on_turn(turn):
        log.append(("turn", turn.turn_id))

    async def on_environment(source, env):
        # R5：回调时这一切都已写回 scene，编排层一次落盘即可提交
        log.append(("env", source.turn_id, env.turn_id, env in engine.scene.dialogue_log,
                    source.revision, source.actions[0].status, dict(engine.scene.environment_state)))

    router = Router(character=["*戴上王冠* 这是母亲的。", "*皱眉* 不对。"],
                    action_extract=[_extracted(_attempt())],
                    adjudicate=[_verdict(changes={"佩戴者": "伊莎贝尔"})])
    with router:
        result = await engine.run(on_turn=on_turn, on_environment=on_environment)

    src, env, nxt = result.dialogue_log
    assert [t.kind for t in result.dialogue_log] == ["character", "environment", "character"]
    assert env.turn_id == environment_turn_id(src.turn_id, 0)
    assert (env.turn_number, nxt.turn_number, result.turns_completed) == (2, 3, 3)
    assert (env.character_id, env.character_name) == ("", ENVIRONMENT_SPEAKER)
    assert env.perceived_by == ["c1"], "A25：只给执行者本人"
    assert (env.source_turn_id, env.source_action_index, env.narration) == (src.turn_id, 0, "王冠微微发亮。")
    assert src.actions[0].status == "resolved" and src.revision == 1
    assert engine.scene.environment_state == {"o-crown": {"佩戴者": "伊莎贝尔"}}
    assert log == [
        ("turn", src.turn_id),
        ("env", src.turn_id, env.turn_id, True, 1, "resolved", {"o-crown": {"佩戴者": "伊莎贝尔"}}),
        ("turn", nxt.turn_id),
    ]
    # 下一个发言者：叙述进"目前对话"，状态进 user 的【当前环境】，都不进 system（契约3）
    (sys1, user1), (sys2, user2) = _character_prompts(router)
    assert "【当前环境】" not in user1, "状态为空时不加这一块"
    assert "【环境】王冠微微发亮。" in user2
    assert "【当前环境】\n- 玻璃王冠·佩戴者：伊莎贝尔\n\n【你此刻想起的】" in user2
    assert "佩戴者" not in sys2


@pytest.mark.asyncio
async def test_environment_turn_goes_through_remember_for_every_participant():
    """R6：环境回合作为普通轮次走 _remember，全体在场角色都记得。"""
    engine, _ = _engine("s-env-mem", max_turns=1)
    seen: list[tuple[str, str]] = []
    real = MemoryManager.add_experience

    async def spy(self, turn):
        seen.append((self.character_id, turn.kind))
        await real(self, turn)

    router = Router(character=["*戴上王冠*"], action_extract=[_extracted(_attempt())])
    with router, patch.object(MemoryManager, "add_experience", spy):
        await engine.run()
    assert ("c1", "environment") in seen and ("c2", "environment") in seen


@pytest.mark.asyncio
async def test_environment_turn_takes_part_in_periodic_consolidation_before_it_is_pushed():
    """陷阱 9：环境回合同样计入固化周期，水位线在推送之前已经推进并落盘。"""
    engine, _ = _engine("s-env-consolidate", max_turns=1)
    at_push: list[int] = []
    persisted: list[int] = []

    async def on_persist():
        persisted.append(engine.scene.turns_consolidated)

    async def on_environment(source, env):
        at_push.append(engine.scene.turns_consolidated)

    router = Router(character=["*戴上王冠*"], action_extract=[_extracted(_attempt())])
    with router, patch.object(settings, "MEMORY_CONSOLIDATE_EVERY_TURNS", 2):
        await engine.run(on_persist=on_persist, on_environment=on_environment)
    assert at_push == [2]
    assert 2 in persisted


@pytest.mark.asyncio
async def test_failed_adjudication_marks_failed_and_produces_no_environment_turn():
    """§5.7：失败不伪造结果，绝不补一句"毫无反应"。"""
    engine, _ = _engine("s-env-fail")
    calls: list[tuple] = []

    async def on_environment(source, env):
        calls.append((source.turn_id, env, source.revision, source.actions[0].status))

    router = Router(character=["*戴上王冠*", "*皱眉*"], action_extract=[_extracted(_attempt())],
                    adjudicate=[LLMError("boom")])
    with router:
        result = await engine.run(on_environment=on_environment)
    assert [t.kind for t in result.dialogue_log] == ["character", "character"]
    assert calls == [(result.dialogue_log[0].turn_id, None, 1, "failed")]


@pytest.mark.asyncio
async def test_quota_exhausted_skips_extraction_and_adjudication_entirely():
    """A15：到顶后的命中轮次，抽取与裁决调用都是 0。"""
    engine, _ = _engine("s-env-quota", agents=[_agent("c1", "伊莎贝尔")])
    router = Router(character=["*戴上王冠*", "*摘下王冠*"], action_extract=[_extracted(_attempt())])
    with router, patch.object(settings, "MAX_ENVIRONMENT_TURNS", 1):
        result = await engine.run()
    assert len(router.of(LLMPurpose.ACTION_EXTRACT)) == 1
    assert len(router.of(LLMPurpose.ADJUDICATE)) == 1
    second = result.dialogue_log[-1]
    assert (second.actions[0].status, second.actions[0].skip_reason) == ("skipped", "quota")


@pytest.mark.asyncio
async def test_attempts_beyond_the_remaining_quota_are_skipped_in_segment_order():
    engine, _ = _engine("s-env-quota-split", max_turns=1, agents=[_agent("c1", "伊莎贝尔")])
    router = Router(character=["*戴上王冠* *掀开祭坛暗格*"],
                    action_extract=[_extracted(_attempt(0, "o1"), _attempt(1, "o2", "掀开"))])
    with router, patch.object(settings, "MAX_ENVIRONMENT_TURNS", 1):
        result = await engine.run()
    first, second = result.dialogue_log[0].actions
    assert first.status == "resolved"
    assert (second.status, second.skip_reason, second.object_id) == ("skipped", "quota", "o-niche")
    assert len(router.of(LLMPurpose.ADJUDICATE)) == 1


@pytest.mark.asyncio
async def test_two_environment_turns_in_a_row_do_not_count_toward_max_turns():
    """§5.5：max_turns 只数角色轮次。"""
    engine, _ = _engine("s-env-count", max_turns=2)
    router = Router(character=["*戴上王冠*", "*拿起王冠*"],
                    action_extract=[_extracted(_attempt()), _extracted(_attempt(verb="拿起"))])
    with router:
        result = await engine.run()
    assert [t.kind for t in result.dialogue_log] == ["character", "environment"] * 2
    assert result.terminated_reason == "达到最大轮次"


# 格式写坏的回复：星号落在独白里（想做、没做），或独白的 `[` 没闭合吞掉了后面的星号。
# 解析层不把它们算作动作（action 为空），意图抽取也必须同口径 —— 拿原文重跑动作正则的话，
# 角色心里的打算会被裁决成一个全场可见的环境回合（评审 P1，契约1）
_THOUGHT_ONLY_ACTIONS = [
    "[我想*戴上王冠*，可不能让人看见] 晚上好。",
    "*走近王冠[她在撒谎* 你来了",
    "［等他们走了再*戴上王冠*］ 夜深了。",
]


@pytest.mark.parametrize("raw", _THOUGHT_ONLY_ACTIONS)
@pytest.mark.parametrize("mode", [ADJ, EnvironmentMode.RECORD.value])
@pytest.mark.asyncio
async def test_actions_inside_thoughts_are_never_extracted_or_adjudicated(raw, mode):
    engine, _ = _engine(f"s-env-thought-{mode}", mode=mode, max_turns=1)
    router = Router(character=[raw], action_extract=[_extracted(_attempt())])
    with router:
        result = await engine.run()
    (src,) = result.dialogue_log
    assert src.action is None, "前提：解析层没把它当成动作"
    assert src.actions == []
    assert router.of(LLMPurpose.ACTION_EXTRACT) == []
    assert router.of(LLMPurpose.ADJUDICATE) == []


@pytest.mark.asyncio
async def test_extraction_uses_the_same_segments_as_the_public_action():
    """正常动作与独白混写：抽取只拿到公开的那一段，独白里的那一段不算。"""
    engine, _ = _engine("s-env-thought-mixed", max_turns=1)
    router = Router(character=["*拿起王冠* [其实想*戴上王冠*] 真轻。"],
                    action_extract=[_extracted(_attempt(verb="拿起"))])
    with router:
        result = await engine.run()
    src = result.dialogue_log[0]
    assert src.action == "拿起王冠"
    assert [a.text for a in src.actions] == ["拿起王冠"]
    (extract,) = router.of(LLMPurpose.ACTION_EXTRACT)
    segments = extract[0]["content"].split("【动作】")[1].split("只输出")[0]
    assert segments.strip() == "0. 拿起王冠"


# ---------------------------------------------------------------------------
# 续跑（§5.4、A27、A34、A35）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_adjudicates_pending_actions_exactly_once():
    scene = _pending_scene("s-env-resume", [_pending()])
    engine, _ = _engine("s-env-resume", max_turns=1, scene=scene)
    engine.inject_history(scene.dialogue_log)
    router = Router()
    with router:
        await engine.run()
    assert len(router.of(LLMPurpose.ADJUDICATE)) == 1
    assert router.of(LLMPurpose.CHARACTER) == [], "max_turns 已满，只补裁决"
    assert [t.kind for t in scene.dialogue_log] == ["character", "environment"]
    assert scene.dialogue_log[1].turn_number == 2 and scene.turns_completed == 2

    again, _ = _engine("s-env-resume", max_turns=1, scene=scene)
    router2 = Router()
    with router2:
        await again.run()
    assert router2.of(LLMPurpose.ADJUDICATE) == []
    assert len(scene.dialogue_log) == 2


@pytest.mark.asyncio
async def test_interrupt_between_actions_leaves_the_rest_pending_for_resume():
    """中断时剩下的动作保持 pending；续跑只补出剩下那一个回合。"""
    scene = _pending_scene("s-env-int", [_pending(0), _pending(1, "o-niche")])
    engine, _ = _engine("s-env-int", max_turns=1, scene=scene)

    async def adjudicate_then_interrupt(*_a, **_kw):
        engine.interrupt()
        return Adjudication(narration="王冠微微发亮。")

    with Router(), patch.object(engine._adjudicator, "adjudicate", new=adjudicate_then_interrupt):
        result = await engine.run()
    assert result.terminated_reason == "导演中断"
    assert [a.status for a in scene.dialogue_log[0].actions] == ["resolved", "pending"]

    resumed, _ = _engine("s-env-int", max_turns=1, scene=scene)
    router = Router()
    with router:
        await resumed.run()
    assert len(router.of(LLMPurpose.ADJUDICATE)) == 1
    assert [a.status for a in scene.dialogue_log[0].actions] == ["resolved", "resolved"]
    assert [t.source_action_index for t in scene.dialogue_log[1:]] == [0, 1]


@pytest.mark.asyncio
async def test_non_adjudicate_mode_keeps_pending_untouched():
    """A27：改成 skipped 就不可恢复；回到 adjudicate 档再补。"""
    scene = _pending_scene("s-env-a27", [_pending()])
    engine, _ = _engine("s-env-a27", mode=EnvironmentMode.RECORD.value, max_turns=1, scene=scene)
    router = Router()
    with router:
        await engine.run()
    assert router.of(LLMPurpose.ADJUDICATE) == []
    assert scene.dialogue_log[0].actions[0].status == "pending"
    assert remaining_environment_quota(scene.dialogue_log, 1) == 0, "仍占额度"


@pytest.mark.asyncio
async def test_pending_action_on_an_object_no_longer_present_fails():
    """A34：物件已被删或不在本场，无从裁决，与裁决失败同一语义。"""
    scene = _pending_scene("s-env-a34", [_pending(object_id="o-gone")])
    engine, _ = _engine("s-env-a34", max_turns=1, scene=scene)
    pushed: list = []

    async def on_environment(source, env):
        pushed.append(env)

    router = Router()
    with router:
        await engine.run(on_environment=on_environment)
    assert router.of(LLMPurpose.ADJUDICATE) == []
    assert scene.dialogue_log[0].actions[0].status == "failed"
    assert pushed == [None] and len(scene.dialogue_log) == 1


@pytest.mark.asyncio
async def test_pending_action_whose_environment_turn_already_exists_is_marked_resolved():
    """A35：不再裁决第二次，也不让它永久占额度。"""
    existing = DialogueTurn(turn_id=environment_turn_id("t1", 0), kind=TurnKind.ENVIRONMENT.value,
                            turn_number=2, character_name=ENVIRONMENT_SPEAKER, narration="王冠亮了。",
                            source_turn_id="t1", source_action_index=0)
    scene = _pending_scene("s-env-a35", [_pending()], extra=[existing])
    engine, _ = _engine("s-env-a35", max_turns=1, scene=scene)
    router = Router()
    with router:
        await engine.run()
    assert router.of(LLMPurpose.ADJUDICATE) == []
    assert len(scene.dialogue_log) == 2
    source = scene.dialogue_log[0]
    assert source.actions[0].status == "resolved" and source.revision == 1


# ---------------------------------------------------------------------------
# 角色侧（C5）
# ---------------------------------------------------------------------------

# off / record 两档的角色 system prompt 必须与 PR-2a 之前逐字相同。这里手抄的是改动前的模板，
# 不从代码里取 —— 从代码里取的话，模板被改了这条也照样绿
_GOLDEN_SYSTEM = """你是【伊莎贝尔】。

【角色设定】
（待补充）

【外貌】
（未描述）

【说话风格】
（自然）

【当前状态】
- 情绪：平静
- 目标：（顺其自然）
- 位置：王城

【你所了解的世界】
（你对世界所知有限）

【你知道的事实】
（无特别已知事实）

【人际关系】
（暂无明确关系）

【当前场景】
加冕前夜 @ 王城

【行为格式规范】
- 对白直接说出，无需引号
- 动作用 *星号包裹*，如：*走向窗边*
- 内心独白用 [方括号包裹]，如：[他在说谎]
- 每轮回应必须包含至少一种格式
- 保持角色一致性，不得跳出角色视角
- 你只知道你"已知"的信息，不得使用你不该知道的信息
- 回应要简洁有戏剧张力，控制在 3 句话以内
"""
_GOLDEN_FIRST_USER = (
    "【目前对话】\n（场景刚刚开始）\n\n"
    "【你此刻想起的】\n（暂无相关记忆）\n\n"
    "现在轮到你（伊莎贝尔）发言，请按行为格式规范回应。"
)


@pytest.mark.parametrize("mode", [EnvironmentMode.OFF.value, EnvironmentMode.RECORD.value])
@pytest.mark.asyncio
async def test_off_and_record_character_prompts_are_byte_identical_to_before(mode):
    engine, _ = _engine(f"s-env-golden-{mode}", mode=mode, max_turns=2)
    router = Router(character=["*戴上王冠*", "*皱眉*"],
                    action_extract=[_extracted({**_attempt(), "is_attempt": True})])
    with router:
        await engine.run()
    (sys1, user1), (_, user2) = _character_prompts(router)
    assert sys1 == _GOLDEN_SYSTEM
    assert user1 == _GOLDEN_FIRST_USER
    assert "【当前环境】" not in user2
    assert router.of(LLMPurpose.ADJUDICATE) == []


@pytest.mark.asyncio
async def test_adjudicate_mode_adds_visible_objects_and_the_attempt_rule_to_system():
    engine, _ = _engine("s-env-system", max_turns=2)
    router = Router(character=["*皱眉*", "*皱眉*"])
    with router:
        await engine.run()
    (sys_c1, _), (sys_c2, _) = _character_prompts(router)
    assert "【当前场景】\n加冕前夜 @ 王城\n\n【在场物件】\n- 玻璃王冠：一顶通体透明的玻璃王冠。\n\n【行为格式规范】" in sys_c1
    assert "暗格" not in sys_c1 and "铜镜" not in sys_c1
    assert "- 暗格：祭坛下松动的地砖。" in sys_c2 and "铜镜" not in sys_c2
    for system in (sys_c1, sys_c2):
        assert "- 对物件的动作只写你的尝试，不写结果" in system, "R7"


@pytest.mark.asyncio
async def test_hidden_rules_reach_only_the_adjudicator():
    """R1：隐藏规则不进任何角色的 system / user、selector 打分、意图抽取 prompt。"""
    engine, _ = _engine("s-env-r1", max_turns=3, speaker_mode="selector")
    router = Router(
        character=["*戴上王冠* *掀开祭坛暗格*", "*看向铜镜*", "*拿起王冠*"],
        action_extract=[_extracted(_attempt(0, "o1"), _attempt(1, "o2", "掀开")),
                        _extracted({"index": 0, "object": "o1", "is_attempt": False}),
                        _extracted(_attempt(verb="拿起"))],
        adjudicate=[_verdict(changes={"佩戴者": "伊莎贝尔"}), _verdict("暗格纹丝不动。", triggered=False),
                    _verdict("王冠被拿在手里。")],
    )
    with router:
        await engine.run()
    # 前提：每条通道都真的走到了
    for purpose in (LLMPurpose.CHARACTER, LLMPurpose.SELECTOR, LLMPurpose.ACTION_EXTRACT):
        assert router.of(purpose), purpose
    adjudications = " ".join(m[0]["content"] for m in router.of(LLMPurpose.ADJUDICATE))
    assert _CROWN_RULE in adjudications and _NICHE_RULE in adjudications
    for purpose, messages in router.calls:
        if purpose == LLMPurpose.ADJUDICATE.value:
            continue
        text = "\n".join(m["content"] for m in messages)
        assert all(rule not in text for rule in _RULES), purpose


# ---------------------------------------------------------------------------
# 编排层（C6）
# ---------------------------------------------------------------------------


async def _orchestrated_scene(scene_id: str) -> Scene:
    pid = f"p-{scene_id}"
    await repository.save_project(Project(project_id=pid, name="玻璃王冠"))
    await repository.save_character(CharacterCard(character_id="c1", project_id=pid, name="伊莎贝尔"))
    await repository.save_object(WorldObject(**{**CROWN.__dict__, "project_id": pid}))
    scene = Scene(scene_id=scene_id, project_id=pid, branch_id="branch-main", name="加冕前夜",
                  participating_characters=["c1"], objects_present=["o-crown"], max_turns=2)
    await repository.save_scene(scene)
    return scene


def _record_order(monkeypatch, order: list):
    real_save, real_publish = repository.save_scene, events.publish

    async def save(scene):
        order.append(("save", len(scene.dialogue_log)))
        await real_save(scene)

    async def publish(scene_id, event, data):
        order.append((event, (data or {}).get("turn_id") if isinstance(data, dict) else None))
        await real_publish(scene_id, event, data)

    class FakeDirector:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate_scene(self, scene, *args, **kwargs):
            return SceneEvaluation(scene_id=scene.scene_id)

    monkeypatch.setattr(repository, "save_scene", save)
    monkeypatch.setattr(events, "publish", publish)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)
    monkeypatch.setattr(settings, "ENVIRONMENT_MODE", ADJ)


@pytest.mark.asyncio
async def test_run_scene_persists_then_pushes_turn_update_then_the_environment_turn(monkeypatch):
    scene = await _orchestrated_scene("s-env-orch")
    order: list = []
    _record_order(monkeypatch, order)
    router = Router(character=["*戴上王冠*", "*皱眉*"], action_extract=[_extracted(_attempt())],
                    adjudicate=[_verdict(changes={"佩戴者": "伊莎贝尔"})])
    with router:
        await orchestrator.run_scene(scene.scene_id)

    stored = await repository.get_scene(scene.scene_id)
    src, env, _ = stored.dialogue_log
    assert env.kind == "environment" and stored.environment_state == {"o-crown": {"佩戴者": "伊莎贝尔"}}
    assert src.revision == 1 and src.actions[0].status == "resolved"
    i = order.index(("turn_update", src.turn_id))
    assert order[i - 1] == ("save", 2), "落盘先于推送（R5），且这次落盘已含环境回合"
    assert order[i + 1] == ("turn", env.turn_id), "先 turn_update 再 turn"


@pytest.mark.asyncio
async def test_run_scene_reports_a_failed_adjudication_as_a_non_fatal_error(monkeypatch):
    scene = await _orchestrated_scene("s-env-orch-fail")
    order: list = []
    _record_order(monkeypatch, order)
    errors: list[dict] = []
    real_publish = events.publish

    async def publish(scene_id, event, data):
        if event == "scene_error":
            errors.append(data)
        await real_publish(scene_id, event, data)

    monkeypatch.setattr(events, "publish", publish)
    router = Router(character=["*戴上王冠*", "*皱眉*"], action_extract=[_extracted(_attempt())],
                    adjudicate=[LLMError("boom")])
    with router:
        await orchestrator.run_scene(scene.scene_id)

    stored = await repository.get_scene(scene.scene_id)
    assert stored.status == "completed"
    assert [t.kind for t in stored.dialogue_log] == ["character", "character"]
    assert stored.dialogue_log[0].actions[0].status == "failed"
    assert errors and errors[0]["fatal"] is False
    src_id = stored.dialogue_log[0].turn_id
    i = order.index(("turn_update", src_id))
    assert order[i - 1][0] == "save" and order[i + 1] == ("scene_error", None)
