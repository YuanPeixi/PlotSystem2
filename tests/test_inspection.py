"""Inspection 层测试（工单17）。

重点覆盖：短期缓冲/事件摘要必须来自快照（旧实现恒返回空）、时点解析优先级、
以及 unknown_facts 的可见性开关（契约1）。
"""

from __future__ import annotations

import pytest

from backend.agents.director_agent import DirectorAgent
from backend.exceptions import SnapshotNotFoundError
from backend.models import (
    CharacterCard,
    CharacterState,
    Project,
    RelationshipState,
    Scene,
)
from backend.services import inspection, repository
from backend.snapshot import SnapshotManager


def _state(cid: str, *, emotion: str, buffer: list[str], summary: str) -> CharacterState:
    return CharacterState(
        character_id=cid,
        current_emotion=emotion,
        current_goal="夺回王位",
        current_location="王座厅",
        relationships={"other": RelationshipState(target_character_id="other")},
        episodic_summary=summary,
        short_term_buffer=buffer,
    )


async def _setup(project_id: str, character_id: str) -> CharacterCard:
    await repository.save_project(Project(project_id=project_id, name="Inspection 测试项目"))
    card = CharacterCard(
        character_id=character_id,
        project_id=project_id,
        name="测试角色",
        persona="沉默寡言的剑客",
        known_facts=["国王病重"],
        unknown_facts=["国王已死"],
        current_emotion="平静",
    )
    await repository.save_character(card)
    return card


@pytest.mark.asyncio
async def test_inspect_falls_back_to_card_without_snapshot():
    """项目刚构建完、还没跑过场景时，应退回角色卡当前值而不是报错。"""
    project_id = "proj-inspect-nosnap"
    character_id = "char-inspect-nosnap"
    await _setup(project_id, character_id)

    view = await inspection.inspect_character(project_id, character_id)
    assert view.state_source == "card"
    assert view.source_snapshot_id == ""
    assert view.short_term_buffer == []
    assert view.name == "测试角色"
    assert view.unknown_facts == ["国王已死"]


@pytest.mark.asyncio
async def test_inspect_reads_latest_snapshot():
    """短期缓冲/事件摘要是纯内存态，必须从最近一个含该角色的快照读出来。"""
    project_id = "proj-inspect-latest"
    character_id = "char-inspect-latest"
    await _setup(project_id, character_id)

    sm = SnapshotManager(project_id)
    await sm.create_snapshot(
        "scene-1",
        "branch-main",
        {character_id: _state(character_id, emotion="愤怒", buffer=["旧的一句"], summary="旧摘要")},
        label="old",
    )
    await sm.create_snapshot(
        "scene-2",
        "branch-main",
        {character_id: _state(character_id, emotion="恐惧", buffer=["新的一句"], summary="新摘要")},
        label="new",
    )

    view = await inspection.inspect_character(project_id, character_id)
    assert view.state_source == "snapshot"
    assert view.current_emotion == "恐惧"
    assert view.short_term_buffer == ["新的一句"]
    assert view.episodic_summary == "新摘要"
    # 长期记忆检索需显式给 query，默认不触发 embedding 调用
    assert view.long_term_hits == []


@pytest.mark.asyncio
async def test_inspect_can_hide_private_facts():
    """unknown_facts 不得进入任何角色可见上下文（契约1）。"""
    project_id = "proj-inspect-private"
    character_id = "char-inspect-private"
    await _setup(project_id, character_id)

    view = await inspection.inspect_character(
        project_id, character_id, include_private=False
    )
    assert view.unknown_facts == []
    assert view.known_facts == ["国王病重"]


@pytest.mark.asyncio
async def test_load_state_by_scene_uses_inheritance_chain():
    """给定 scene_id 时走契约4 的四级继承（这里命中父场景结束态）。"""
    project_id = "proj-inspect-scene"
    character_id = "char-inspect-scene"
    await _setup(project_id, character_id)

    sm = SnapshotManager(project_id)
    snap = await sm.create_snapshot(
        "scene-parent",
        "branch-main",
        {character_id: _state(character_id, emotion="悲伤", buffer=["父场景一句"], summary="父摘要")},
        label="after",
    )
    parent = Scene(
        scene_id="scene-inspect-parent",
        project_id=project_id,
        branch_id="branch-main",
        name="上一场",
        snapshot_id_after=snap.snapshot_id,
        status="completed",
    )
    await repository.save_scene(parent)
    child = Scene(
        scene_id="scene-inspect-child",
        project_id=project_id,
        branch_id="branch-main",
        parent_scene_id=parent.scene_id,
        name="下一场",
        participating_characters=[character_id],
    )
    await repository.save_scene(child)

    state, source = await inspection.load_character_state(
        project_id, character_id, scene_id=child.scene_id
    )
    assert source == snap.snapshot_id
    assert state.short_term_buffer == ["父场景一句"]


@pytest.mark.asyncio
async def test_load_state_missing_snapshot_raises():
    project_id = "proj-inspect-missing"
    character_id = "char-inspect-missing"
    await _setup(project_id, character_id)

    with pytest.raises(SnapshotNotFoundError):
        await inspection.load_character_state(
            project_id, character_id, snapshot_id="no-such-snapshot"
        )


@pytest.mark.asyncio
async def test_director_query_character_state_is_no_longer_a_stub():
    """query_character_state 曾是返回空壳的死代码（CLAUDE 12.2）。"""
    project_id = "proj-inspect-director"
    character_id = "char-inspect-director"
    await _setup(project_id, character_id)

    sm = SnapshotManager(project_id)
    await sm.create_snapshot(
        "scene-1",
        "branch-main",
        {character_id: _state(character_id, emotion="决绝", buffer=["我要复仇"], summary="摘要")},
        label="after",
    )

    director = DirectorAgent(project_id)
    state = await director.query_character_state(character_id)
    assert state.current_emotion == "决绝"
    assert state.short_term_buffer == ["我要复仇"]
