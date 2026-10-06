"""工单20 PR-2a C3：环境裁决器与裁决的纯函数。

钉住：失败不伪造结果（没有叙述就是没裁决）；执行性与触发必须明确；私密细节不由裁决器书写，
只能是它按编号选中的**一条**导演预制揭示（A31：跨分支泄露在结构上不可能），且只在规则真正
触发时给；预制揭示的内容不进裁决 prompt；状态变化只能改本物件、超预算拒掉新的不挤掉旧的；
裁决器的输入里没有任何角色视图。
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock

import pytest

from backend.agents import environment_agent
from backend.agents.environment_agent import EnvironmentAgent, build_adjudication_prompt
from backend.exceptions import LLMError
from backend.models import ActionIntent, ActionStatus, LLMPurpose, RevealEntry, WorldObject
from backend.services.environment import (
    ENVIRONMENT_STATE_BUDGET_TOKENS,
    MAX_ENVIRONMENT_STATE_KEYS,
    MAX_REVEAL_ENTRIES,
    MAX_STATE_CHANGES,
    NARRATION_TOKENS,
    REVEAL_OBSERVABLE,
    REVEAL_SCRIPT,
    apply_state_changes,
    normalize_reveals,
    object_state_view,
    parse_adjudication,
)
from backend.utils.llm import estimate_tokens

CROWN = WorldObject(
    object_id="o-crown", project_id="p", name="王冠", public_description="一顶水晶王冠",
    hidden_rules=["王室血脉戴上才会投影", "投出的是佩戴者最深的记忆", "伊莎贝尔并非王后亲生"],
)
WEAR = ActionIntent(index=0, text="把王冠戴到头上", object_id="o-crown", verb="戴上",
                    detail="戴到自己头上", status=ActionStatus.PENDING.value)
#: 导演预制揭示，按条存：一条只对应一种触发情形
REVEALS = [
    RevealEntry(condition="伊莎贝尔戴上", content="投出她六岁那年的冬天，她看见了母亲的脸"),
    RevealEntry(condition="王后戴上", content="投出加冕夜的大火"),
]


def _raw(**kw) -> str:
    base = {"executable": True, "triggered": True, "reveal_index": 1,
            "narration": "王冠亮起微光", "state_changes": {"光芒": "微弱"}}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 解析：叙述与执行性
# ---------------------------------------------------------------------------


def test_parse_happy_path_with_a_reveal():
    result = parse_adjudication(_raw(), reveals=REVEALS)
    assert (result.executable, result.triggered, result.reveal_source) == (True, True, REVEAL_SCRIPT)
    assert result.narration == "王冠亮起微光"
    assert result.private_detail == "投出她六岁那年的冬天，她看见了母亲的脸"
    assert result.state_changes == {"光芒": "微弱"}
    assert result.rejected == []


def test_parse_accepts_a_fenced_block():
    assert parse_adjudication(f"好的：\n```json\n{_raw()}\n```", reveals=REVEALS).narration == "王冠亮起微光"


@pytest.mark.parametrize("raw", ["不是 JSON", "[]", '{"narration": ""}', '{"narration": "   "}',
                                 '{"triggered": true}', '{"narration": null}'])
def test_no_narration_means_no_adjudication(raw):
    """失败不伪造结果：没有叙述的裁决就是失败，绝不能补一句"毫无反应"（设计单 §5.7）。"""
    assert parse_adjudication(raw, reveals=REVEALS) is None


@pytest.mark.parametrize(("value", "expected"), [(True, True), ("true", True), (False, False), ("false", False)])
def test_triggered_accepts_only_booleans(value, expected):
    """"false" 不能变成 True：误判触发会凭空改写物件状态。"""
    assert parse_adjudication(_raw(triggered=value), reveals=REVEALS).triggered is expected


@pytest.mark.parametrize("field", ["executable", "triggered"])
@pytest.mark.parametrize("value", ["是", "maybe", 1, None, ["true"], "MISSING"])
def test_unknown_executable_or_triggered_fails_the_adjudication(field, value):
    """评审 P1：缺失或非法时不能替模型补缺省。补成"可执行"会让残缺的输出也改写物件状态
    （进每个角色的【当前环境】，2b 起还进世界变量）；补成"不可执行"又会让写着触发的叙述
    与没动过的状态对不上。整次按失败处理（设计单 §5.7）。"""
    payload = json.loads(_raw(executable=True, triggered=True, state_changes={"光芒": "明亮"}))
    if value == "MISSING":
        del payload[field]
    else:
        payload[field] = value
    assert parse_adjudication(json.dumps(payload, ensure_ascii=False), reveals=REVEALS) is None


# ---------------------------------------------------------------------------
# 解析：私密细节只能是选中的那一条预制揭示（A31）
# ---------------------------------------------------------------------------


def test_one_branch_only_never_the_whole_script():
    """评审 P1（复现）：整段自由文本的子串校验挡不住"把整段原样返回"，两个分支一起进了伊莎贝尔
    的记忆。按条选之后，每次只能给一条，王后那一条结构上就到不了她手里。"""
    result = parse_adjudication(_raw(reveal_index=1), reveals=REVEALS)
    assert result.private_detail == REVEALS[0].content
    assert "加冕夜" not in result.private_detail


def test_adjudicator_written_detail_is_never_used():
    """评审 P2（复现）：裁决器看着隐藏规则，它自己写的细节可能是"伊莎贝尔并非王后亲生"。
    私密细节只从选中的条目里取，模型写什么都不采纳。"""
    result = parse_adjudication(_raw(private_detail="她终于明白王后不是她的亲生母亲"), reveals=REVEALS)
    assert result.private_detail == REVEALS[0].content
    assert any("不采纳" in r for r in result.rejected)

    result = parse_adjudication(
        _raw(reveal_index=0, private_detail="她终于明白王后不是她的亲生母亲"), reveals=REVEALS
    )
    assert result.private_detail == ""
    assert result.reveal_source == REVEAL_OBSERVABLE


@pytest.mark.parametrize("index", [0, None, "MISSING"])
def test_no_index_means_observable(index):
    payload = json.loads(_raw())
    if index == "MISSING":
        del payload["reveal_index"]
    else:
        payload["reveal_index"] = index
    result = parse_adjudication(json.dumps(payload, ensure_ascii=False), reveals=REVEALS)
    assert result.reveal_source == REVEAL_OBSERVABLE
    assert result.private_detail == ""
    assert result.rejected == []


@pytest.mark.parametrize("index", [3, -1, 99, True, "1", 1.0, [1]])
def test_out_of_range_or_malformed_index_reveals_nothing(index):
    result = parse_adjudication(_raw(reveal_index=index), reveals=REVEALS)
    assert result.reveal_source == REVEAL_OBSERVABLE
    assert result.private_detail == ""
    assert result.rejected


def test_no_reveals_means_any_index_is_out_of_range():
    """没有预制揭示时，声称命中只能是编的。"""
    result = parse_adjudication(_raw(reveal_index=1), reveals=[])
    assert result.private_detail == ""
    assert result.reveal_source == REVEAL_OBSERVABLE


def test_untriggered_rule_never_reveals():
    """评审 P1：规则没触发（不是王室血脉的人戴上王冠），预制揭示不能进执行者记忆（契约1）。"""
    result = parse_adjudication(_raw(triggered=False), reveals=REVEALS)
    assert result.reveal_source == REVEAL_OBSERVABLE
    assert result.private_detail == ""
    assert result.rejected


def test_unexecutable_action_never_reveals():
    """同一个洞的第二个入口：不可执行时 triggered 被强制为 false，预制揭示同样不能给。"""
    result = parse_adjudication(_raw(executable=False, triggered=True), reveals=REVEALS)
    assert result.triggered is False
    assert result.reveal_source == REVEAL_OBSERVABLE
    assert result.private_detail == ""


def test_normalize_reveals_drops_empty_entries_and_caps_the_count():
    entries = [RevealEntry("", "无条件"), RevealEntry("有条件", "  "),
               *[RevealEntry(f"情形\n{i}", f"内容{i}") for i in range(MAX_REVEAL_ENTRIES + 2)]]
    kept = normalize_reveals(entries)
    assert len(kept) == MAX_REVEAL_ENTRIES
    assert kept[0] == RevealEntry("情形 0", "内容0")


# ---------------------------------------------------------------------------
# 解析：状态变化
# ---------------------------------------------------------------------------


def test_not_executable_cannot_trigger_or_change_state():
    result = parse_adjudication(_raw(executable=False), reveals=REVEALS)
    assert result.executable is False
    assert result.triggered is False
    assert result.state_changes == {}


def test_state_changes_are_shaped():
    changes = {"光芒": "微\n弱", "佩戴者": None, "甲·乙": "x", "": "x", "很" * 30: "长属性名", "暗格": ""}
    result = parse_adjudication(_raw(state_changes=changes), reveals=REVEALS)
    assert result.state_changes["光芒"] == "微 弱"
    assert result.state_changes["佩戴者"] is None
    assert result.state_changes["暗格"] is None  # 空串与 null 同义：清除
    assert "甲·乙" not in result.state_changes
    assert len(result.state_changes) <= MAX_STATE_CHANGES


def test_too_many_state_changes_are_capped():
    changes = {f"属性{i}": "值" for i in range(MAX_STATE_CHANGES + 3)}
    result = parse_adjudication(_raw(state_changes=changes), reveals=REVEALS)
    assert len(result.state_changes) == MAX_STATE_CHANGES
    assert result.rejected


@pytest.mark.parametrize("changes", ["光芒=微弱", ["光芒"], 3])
def test_non_dict_state_changes_are_rejected(changes):
    result = parse_adjudication(_raw(state_changes=changes), reveals=REVEALS)
    assert result.state_changes == {}
    assert result.rejected


def test_narration_is_one_line_and_bounded():
    result = parse_adjudication(_raw(narration="王冠\n亮起" + "很长的光" * 200), reveals=REVEALS)
    assert "\n" not in result.narration
    assert estimate_tokens(result.narration) <= NARRATION_TOKENS + 10


# ---------------------------------------------------------------------------
# 物件状态：视图与写入侧闸门
# ---------------------------------------------------------------------------


def test_object_state_view_overlays_scene_state_on_world_variables():
    world = {"王冠·佩戴者": "王后", "王冠·光芒": "黯淡", "王冠碎片·数量": "三片", "季节": "冬"}
    env = {"王冠·光芒": "明亮", "王冠·佩戴者": None, "镜子·裂痕": "一道"}
    # 本场清除的属性从视图里消失；"王冠碎片"不是"王冠"
    assert object_state_view("王冠", world, env) == {"光芒": "明亮"}


def test_apply_state_changes_builds_keys_for_this_object_only():
    state, rejected = apply_state_changes({}, "王冠", {"光芒": "微弱", "佩戴者": None})
    assert state == {"王冠·光芒": "微弱", "王冠·佩戴者": None}
    assert rejected == []


def test_apply_state_changes_does_not_mutate_its_input():
    before = {"王冠·光芒": "黯淡"}
    apply_state_changes(before, "王冠", {"光芒": "明亮"})
    assert before == {"王冠·光芒": "黯淡"}


def test_updating_an_existing_key_moves_it_to_the_end():
    state, _ = apply_state_changes({"王冠·光芒": "黯淡", "镜子·裂痕": "一道"}, "王冠", {"光芒": "明亮"})
    assert list(state) == ["镜子·裂痕", "王冠·光芒"]


def test_over_the_count_limit_the_new_change_is_rejected_not_the_old():
    full = {f"物件{i}·状态": "值" for i in range(MAX_ENVIRONMENT_STATE_KEYS)}
    state, rejected = apply_state_changes(full, "王冠", {"光芒": "微弱"})
    assert state == full
    assert rejected == ["王冠·光芒"]
    # 改已有的键不增加条数，照常生效
    state, rejected = apply_state_changes(full, "物件0", {"状态": "新值"})
    assert state["物件0·状态"] == "新值" and rejected == []


def test_over_the_token_budget_the_new_change_is_rejected():
    big = {f"物件{i}·状态": "很长的描述" * 10 for i in range(5)}
    assert sum(estimate_tokens(f"- {k}：{v}") for k, v in big.items()) < ENVIRONMENT_STATE_BUDGET_TOKENS
    state, rejected = apply_state_changes(big, "王冠", {"光芒": "很长的描述" * 30})
    assert state == big
    assert rejected == ["王冠·光芒"]


# ---------------------------------------------------------------------------
# 裁决器
# ---------------------------------------------------------------------------


def test_prompt_contains_the_object_rules_state_conditions_and_action():
    prompt = build_adjudication_prompt(CROWN, "伊莎贝尔", WEAR, {"光芒": "黯淡"}, REVEALS)
    for text in ("王冠", "一顶水晶王冠", "王室血脉戴上才会投影", "光芒：黯淡", "1. 伊莎贝尔戴上", "2. 王后戴上",
                 "伊莎贝尔", "把王冠戴到头上", "戴上"):
        assert text in prompt


def test_reveal_contents_never_enter_the_adjudication_prompt():
    """A31：裁决器只看触发情形。内容不进 prompt，公开叙述就无从转述它。"""
    prompt = build_adjudication_prompt(CROWN, "伊莎贝尔", WEAR, {}, REVEALS)
    for entry in REVEALS:
        assert entry.content not in prompt


def test_prompt_builder_has_no_character_view_input():
    """R1 / R2：裁决器的输入里没有任何角色视图 —— 结构上就递不进去，而不是靠提示词约束。"""
    params = set(inspect.signature(build_adjudication_prompt).parameters)
    assert params == {"obj", "actor_name", "intent", "state", "reveals"}


@pytest.mark.asyncio
async def test_adjudicate_tags_its_purpose_and_parses(monkeypatch):
    fake = AsyncMock(return_value=_raw(reveal_index=0))
    monkeypatch.setattr(environment_agent, "chat_safe", fake)
    result = await EnvironmentAgent(model="m").adjudicate(CROWN, "伊莎贝尔", WEAR, {})
    assert result.narration == "王冠亮起微光"
    assert fake.await_args.kwargs["purpose"] == LLMPurpose.ADJUDICATE
    assert fake.await_args.kwargs["temperature"] == 0.3


@pytest.mark.asyncio
async def test_prompt_and_parse_share_the_same_numbering(monkeypatch):
    """编号必须指向同一条：prompt 与解析用的是同一份规整结果，前面的空条目被丢掉也不错位。"""
    fake = AsyncMock(return_value=_raw(reveal_index=2))
    monkeypatch.setattr(environment_agent, "chat_safe", fake)
    reveals = [RevealEntry("", "被丢掉的空条件"), *REVEALS]
    result = await EnvironmentAgent(model="m").adjudicate(CROWN, "王后", WEAR, {}, reveals)
    prompt = fake.await_args.args[0][0]["content"]
    assert "2. 王后戴上" in prompt
    assert result.private_detail == "投出加冕夜的大火"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [LLMError("挂了"), "不是 JSON"])
async def test_adjudicate_failure_returns_none(monkeypatch, outcome):
    side = outcome if isinstance(outcome, Exception) else None
    ret = None if side else outcome
    monkeypatch.setattr(environment_agent, "chat_safe", AsyncMock(side_effect=side, return_value=ret))
    assert await EnvironmentAgent(model="m").adjudicate(CROWN, "伊莎贝尔", WEAR, {}) is None
