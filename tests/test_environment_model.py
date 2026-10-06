"""工单20 PR-2a C1：环境回合的数据模型与读取侧闸门。

钉住：环境回合字段往返不丢；旧轮次缺键取默认；手改出的脏值收紧到"什么都不做"
（不能变成待裁决、不能扩大私密细节的可见范围、不能让 list_scenes 五百）；
场景物件状态的形状与预算在读取那一刻压回、只压不写回。
"""

from __future__ import annotations

import uuid

import pytest

from backend.models import (
    ActionIntent,
    ActionSkipReason,
    ActionStatus,
    DialogueTurn,
    Project,
    Scene,
    TurnKind,
    environment_turn_id,
)
from backend.services import repository
from backend.services.environment import (
    ENVIRONMENT_ATTR_CHARS,
    MAX_ENVIRONMENT_STATE_KEYS,
    clamp_environment_state,
    describe_environment_state,
    normalize_attribute,
)
from backend.services.repository import _deserialize_actions, _deserialize_scene

# ---------------------------------------------------------------------------
# 环境回合的确定性 ID
# ---------------------------------------------------------------------------


def test_environment_turn_id_is_deterministic_and_uuid_shaped():
    a = environment_turn_id("t-1", 0)
    assert a == environment_turn_id("t-1", 0)
    assert a != environment_turn_id("t-1", 1)
    assert a != environment_turn_id("t-2", 0)
    assert str(uuid.UUID(a)) == a  # 与 new_id() 同形态


# ---------------------------------------------------------------------------
# 轮次往返
# ---------------------------------------------------------------------------


def _scene_dict(turns: list[dict], **extra) -> dict:
    return {"scene_id": "s", "project_id": "p", "dialogue_log": turns, **extra}


@pytest.mark.asyncio
async def test_environment_turn_round_trips_through_the_repository():
    await repository.save_project(Project(project_id="proj-env-rt", name="环境往返"))
    source = DialogueTurn(
        turn_id="t-src", character_id="c1", character_name="伊莎贝尔", action="戴上王冠", revision=1,
        actions=[ActionIntent(index=0, text="戴上王冠", object_id="o1", verb="戴上", status=ActionStatus.RESOLVED.value)],
    )
    env = DialogueTurn(
        turn_id=environment_turn_id("t-src", 0), character_name="环境", kind=TurnKind.ENVIRONMENT.value,
        narration="王冠亮起微光", private_detail="她看见了母亲的脸", perceived_by=["c1"],
        source_turn_id="t-src", source_action_index=0,
    )
    scene = Scene(scene_id="scene-env-rt", project_id="proj-env-rt", branch_id="b",
                  dialogue_log=[source, env],
                  environment_state={"o-crown": {"佩戴者": "伊莎贝尔"}, "o-niche": {"状态": None}})
    await repository.save_scene(scene)

    back = await repository.get_scene("scene-env-rt")
    src_back, env_back = back.dialogue_log
    assert src_back.revision == 1
    assert src_back.actions[0].status == ActionStatus.RESOLVED.value
    for name in ("kind", "narration", "private_detail", "perceived_by", "source_turn_id",
                 "source_action_index", "revision", "turn_id"):
        assert getattr(env_back, name) == getattr(env, name), name
    assert back.environment_state == {"o-crown": {"佩戴者": "伊莎贝尔"}, "o-niche": {"状态": None}}


def test_old_turns_without_environment_fields_take_defaults():
    turn = _deserialize_scene(_scene_dict([{"turn_id": "t", "dialogue": "旧台词"}])).dialogue_log[0]
    assert (turn.narration, turn.private_detail, turn.perceived_by) == (None, None, [])
    assert (turn.source_turn_id, turn.source_action_index, turn.revision) == ("", -1, 0)
    assert _deserialize_scene(_scene_dict([])).environment_state == {}


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("perceived_by", "c1", []),
        # 名单决定谁的记忆里有私密细节：非字符串项一律丢掉，不能把它扩成别的东西
        ("perceived_by", ["c1", 2, None, "", {"id": "c2"}], ["c1"]),
        ("source_action_index", True, -1),
        ("source_action_index", -3, -1),
        ("source_action_index", "0", -1),
        ("revision", "3", 0),
        ("revision", -1, 0),
        ("revision", False, 0),
        ("narration", ["不是字符串"], None),
        ("private_detail", 42, None),
        ("narration", "", None),
    ],
)
def test_corrupt_environment_fields_degrade_safely(field, raw, expected):
    turn = _deserialize_scene(_scene_dict([{"turn_id": "t", field: raw}])).dialogue_log[0]
    assert getattr(turn, field) == expected


# ---------------------------------------------------------------------------
# 动作意图的读取侧收紧
# ---------------------------------------------------------------------------


def _action(**kw) -> dict:
    return {"index": 0, "text": "拿起王冠", "object_id": "o1", **kw}


@pytest.mark.parametrize("status", ["pending", "resolved", "failed", "recorded"])
def test_new_statuses_survive_with_an_object(status):
    (intent,) = _deserialize_actions([_action(status=status, skip_reason="no_object")])
    assert intent.status == status
    assert intent.skip_reason == ""  # 原因只对 skipped 有意义


@pytest.mark.parametrize("status", ["pending", "resolved", "failed", "recorded"])
def test_object_statuses_without_an_object_are_tightened(status):
    (intent,) = _deserialize_actions([_action(status=status, object_id="")])
    assert intent.status == ActionStatus.SKIPPED.value
    assert intent.skip_reason == ActionSkipReason.INVALID_OBJECT.value


@pytest.mark.parametrize("status", ["PENDING", "待裁决", None, 3, ["pending"], {"s": 1}])
def test_unknown_or_unhashable_status_never_becomes_pending(status):
    (intent,) = _deserialize_actions([_action(status=status)])
    assert intent.status == ActionStatus.SKIPPED.value


@pytest.mark.parametrize("reason", [["quota"], {"r": 1}, 7])
def test_unhashable_skip_reason_does_not_crash(reason):
    (intent,) = _deserialize_actions([_action(status="skipped", skip_reason=reason)])
    assert intent.skip_reason == ""


def test_quota_is_a_known_skip_reason():
    (intent,) = _deserialize_actions([_action(status="skipped", skip_reason="quota")])
    assert intent.skip_reason == ActionSkipReason.QUOTA.value


# ---------------------------------------------------------------------------
# 场景物件状态：按 object_id 归属、预算、渲染
# ---------------------------------------------------------------------------


def _flat(state: dict) -> list[tuple[str, str]]:
    return [(oid, attr) for oid, attrs in state.items() for attr in attrs]


def test_attribute_shape():
    assert normalize_attribute(" 佩戴\t者 ") == "佩戴 者"
    assert normalize_attribute("") == ""
    assert normalize_attribute("甲·乙") == ""  # 渲染成"物件名·甲·乙"会被读成别的东西
    assert len(normalize_attribute("很" * 40)) == ENVIRONMENT_ATTR_CHARS


def test_clamp_rejects_bad_shapes_and_collapses_newlines():
    state = clamp_environment_state(
        {"o-crown": {"佩戴者": "伊莎\n贝尔", "": "x", "甲·乙": "x", "光芒": ""},
         "o-niche": {"状态": None}, "": {"x": "y"}, "o-bad": "不是对象", " o-mirror\n": {"裂痕": "一道"}},
        "测试",
    )
    assert state == {"o-crown": {"佩戴者": "伊莎 贝尔", "光芒": None}, "o-niche": {"状态": None},
                     "o-mirror": {"裂痕": "一道"}}


@pytest.mark.parametrize("raw", ["坏", ["o-crown"], 3])
def test_clamp_non_dict_is_empty(raw):
    assert clamp_environment_state(raw, "测试") == {}


def test_clamp_keeps_the_most_recent_entries_over_the_count_limit():
    raw = {f"o-{i}": {"状态": f"值{i}"} for i in range(MAX_ENVIRONMENT_STATE_KEYS + 3)}
    state = clamp_environment_state(raw, "测试")
    assert len(_flat(state)) == MAX_ENVIRONMENT_STATE_KEYS
    assert list(state) == list(raw)[3:]  # 淘汰最早的，顺序保持


def test_clamp_enforces_the_token_budget():
    raw = {f"o-{i}": {"状态": "很长的描述" * 40} for i in range(MAX_ENVIRONMENT_STATE_KEYS)}
    state = clamp_environment_state(raw, "测试")
    assert 0 < len(state) < MAX_ENVIRONMENT_STATE_KEYS
    assert list(state)[-1] == list(raw)[-1]


def test_reading_a_scene_applies_the_clamp():
    """场景 JSON 可被人工编辑：读出来那一刻就要压回，不能等到渲染时才发现。"""
    raw = {f"o-{i}": {"状态": f"值{i}"} for i in range(MAX_ENVIRONMENT_STATE_KEYS + 2)}
    raw["o-bad"] = "不是对象"
    raw["o-crown"] = {"佩戴者": "伊莎\n贝尔"}
    scene = _deserialize_scene(_scene_dict([], environment_state=raw))
    assert len(_flat(scene.environment_state)) == MAX_ENVIRONMENT_STATE_KEYS
    assert "o-bad" not in scene.environment_state
    assert scene.environment_state["o-crown"] == {"佩戴者": "伊莎 贝尔"}
    assert _deserialize_scene(_scene_dict([], environment_state="坏")).environment_state == {}


def test_describe_renders_current_names_and_skips_cleared_or_absent():
    state = {"o-crown": {"佩戴者": "伊莎贝尔", "光芒": None}, "o-niche": {"状态": None}, "o-gone": {"x": "y"}}
    names = {"o-crown": "水晶冠", "o-niche": "暗格"}  # 王冠改过名：按 ID 归属，照样读得到
    assert describe_environment_state(state, names) == "- 水晶冠·佩戴者：伊莎贝尔"
    assert describe_environment_state({"o-niche": {"状态": None}}, names) == ""
    assert describe_environment_state(None, names) == ""
