"""DirectorAgent 单测（工单04）。"""

from __future__ import annotations

import json

import pytest

from backend.agents import director_agent as da
from backend.models import CharacterCard, DialogueTurn, Scene


def _cards() -> list[CharacterCard]:
    return [
        CharacterCard(
            character_id="c1",
            name="王子",
            persona="外冷内热的继承人",
            known_facts=["国王病重"],
            unknown_facts=["丞相已与敌国密约"],
        ),
        CharacterCard(
            character_id="c2",
            name="公主",
            persona="心思缜密",
            known_facts=["宫中有内奸"],
            unknown_facts=["王子并非亲生"],
        ),
        CharacterCard(character_id="c3", name="王", persona="老国王"),
    ]


def _scene(**kw) -> Scene:
    base = dict(
        scene_id="s1",
        project_id="p1",
        branch_id="b1",
        name="朝堂对峙",
        description="王子质问丞相",
        location="金銮殿",
        initial_conditions={"opening_narration": "夜色沉沉"},
        participating_characters=["c1", "c2"],
    )
    base.update(kw)
    return Scene(**base)


def _log(n: int = 3) -> list[DialogueTurn]:
    return [
        DialogueTurn(
            turn_id=f"t{i}",
            scene_id="s1",
            turn_number=i,
            character_id="c1",
            character_name="王子",
            dialogue=f"第{i}句台词",
            inner_thought=f"内心{i}",
        )
        for i in range(n)
    ]


def _patch_chat(monkeypatch, reply: str, sink: list | None = None):
    async def _fake(messages, **kwargs):
        if sink is not None:
            sink.append(messages)
        return reply

    monkeypatch.setattr(da, "chat_safe", _fake)


# --- 评估上下文（D5）---------------------------------------------------------


async def test_eval_prompt_contains_character_profiles(monkeypatch):
    """导演有全知权：角色设定与 unknown_facts 都必须进评估 prompt（契约1 的合法例外）。"""
    sink: list = []
    _patch_chat(monkeypatch, json.dumps({"synopsis": "还行"}), sink)

    agent = da.DirectorAgent("p1")
    await agent.evaluate_scene(_scene(), _log(), _cards()[:2])

    prompt = sink[0][0]["content"]
    assert "王子" in prompt and "公主" in prompt
    assert "丞相已与敌国密约" in prompt  # unknown_facts
    assert "金銮殿" in prompt and "夜色沉沉" in prompt  # 场景预设与开场白


async def test_eval_without_characters_still_works(monkeypatch):
    _patch_chat(monkeypatch, json.dumps({"synopsis": "x", "narrative_goal_score": 8}))
    agent = da.DirectorAgent("p1")
    result = await agent.evaluate_scene(_scene(), _log())
    assert result.narrative_goal_score == 8


# --- 解析失败不再伪装（D2）---------------------------------------------------


async def test_unparsable_evaluation_returns_negative_scores(monkeypatch):
    _patch_chat(monkeypatch, "模型今天不想输出 JSON。")
    agent = da.DirectorAgent("p1")

    result = await agent.evaluate_scene(_scene(), _log(), _cards())

    assert result.narrative_goal_score == -1.0
    assert result.character_consistency_score == -1.0
    assert "解析失败" in result.synopsis
    assert result.recommended_decision == "next_scene"


async def test_non_dict_json_is_treated_as_failure(monkeypatch):
    _patch_chat(monkeypatch, "[1, 2, 3]")
    agent = da.DirectorAgent("p1")
    result = await agent.evaluate_scene(_scene(), _log())
    assert result.narrative_goal_score == -1.0


async def test_failed_evaluation_does_not_become_rollback(monkeypatch):
    """评估不可用时，-1 绝不能进阈值规则——rollback 会真的建分支、真的改剧情。"""
    _patch_chat(monkeypatch, "今天不想输出 JSON。")
    agent = da.DirectorAgent("p1")

    evaluation = await agent.evaluate_scene(_scene(), _log(), _cards())
    decision = await agent.make_decision(evaluation)

    assert decision.decision_type == "next_scene"
    assert decision.rollback_notes is None


async def test_missing_evaluation_does_not_become_rollback():
    """压根没有评估时同理：裸的 SceneEvaluation() 四项是 0.0，会被阈值判成回滚。"""
    agent = da.DirectorAgent("p1")
    decision = await agent.make_decision(da.unavailable_evaluation("s1"))
    assert decision.decision_type == "next_scene"


async def test_valid_low_scores_still_trigger_rollback():
    """修复不得把真实的低分回滚也一并屏蔽掉。"""
    from backend.models import SceneEvaluation

    agent = da.DirectorAgent("p1")
    decision = await agent.make_decision(
        SceneEvaluation(
            scene_id="s1",
            narrative_goal_score=2.0,
            dramatic_tension_score=5.0,
            plot_deviation_score=8.0,
            character_consistency_score=3.0,
            recommended_decision="next_scene",
        )
    )
    assert decision.decision_type == "rollback"


# --- 角色名匹配（D3）---------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("王子", "c1"),
        (" 王子 ", "c1"),
        ("王子殿下", "c1"),  # 子串包含
        ("公主", "c2"),
    ],
)
def test_match_characters_three_levels(name, expected):
    hit, missed = da.DirectorAgent._match_characters([name], _cards())
    assert hit == [expected]
    assert missed == []


def test_match_characters_prefers_longest_name():
    """子串匹配时，真实存在的短名角色"王"不能抢在"王子"前面命中。"""
    hit, missed = da.DirectorAgent._match_characters(["年轻的王子"], _cards())
    assert hit == ["c1"]
    assert missed == []


def test_match_characters_reports_missed():
    hit, missed = da.DirectorAgent._match_characters(["铁匠铺老板"], _cards())
    assert hit == []
    assert missed == ["铁匠铺老板"]


def test_fallback_uses_recent_appearance_not_extraction_order():
    history = [
        _scene(scene_id="s0", participating_characters=["c2", "c3"]),
        _scene(scene_id="s1", participating_characters=["c2"]),
    ]
    ids = da.DirectorAgent._fallback_characters(_cards(), history, limit=2)
    assert ids[0] == "c2"  # 出场最多者优先，而非角色列表里的第一个 c1


async def test_plan_scene_falls_back_and_warns(monkeypatch, caplog):
    _patch_chat(
        monkeypatch,
        json.dumps({"name": "夜谈", "participating_characters": ["不存在的角色"]}),
    )
    agent = da.DirectorAgent("p1")
    with caplog.at_level("WARNING"):
        config = await agent.plan_scene("b1", "推进主线", _cards())
    assert len(config.participating_characters) >= 2
    assert any("无法匹配" in r.message for r in caplog.records)


# --- 开场白端到端（D1）-------------------------------------------------------


async def test_opening_narration_survives_scene_creation(monkeypatch):
    """AI 规划出的开场白必须落到 initial_conditions —— 引擎只从那里读。"""
    from backend.services import orchestrator

    _patch_chat(
        monkeypatch,
        json.dumps(
            {
                "name": "密谈",
                "description": "两人夜里密谈",
                "participating_characters": ["王子"],
                "location": "偏殿",
                "opening_narration": "烛火摇曳",
            }
        ),
    )
    agent = da.DirectorAgent("p1")
    config = await agent.plan_scene("b1", "推进主线", _cards())
    assert config.opening_narration == "烛火摇曳"

    scene = await orchestrator.create_scene_from_config("p1", "b1", config)
    assert scene.initial_conditions["opening_narration"] == "烛火摇曳"


# --- 上下文预算（D4）---------------------------------------------------------


async def test_long_transcript_keeps_last_turn(monkeypatch):
    sink: list = []
    _patch_chat(monkeypatch, json.dumps({"synopsis": "ok"}), sink)
    monkeypatch.setattr(da.settings, "DIRECTOR_TRANSCRIPT_BUDGET", 200)
    monkeypatch.setattr(da.settings, "DIRECTOR_TRANSCRIPT_STRATEGY", "head_tail")

    agent = da.DirectorAgent("p1")
    log = _log(300)
    await agent.evaluate_scene(_scene(), log, _cards())

    prompt = sink[0][0]["content"]
    assert "第299句台词" in prompt  # 绝不截尾
    assert "省略" in prompt
