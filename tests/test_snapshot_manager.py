"""SnapshotManager 测试。"""

from __future__ import annotations

import pytest

from backend.models import CharacterState, RelationshipState
from backend.snapshot import SnapshotManager


@pytest.mark.asyncio
async def test_create_and_restore_snapshot():
    sm = SnapshotManager("proj-snap")
    states = {
        "c1": CharacterState(
            character_id="c1",
            current_emotion="愤怒",
            current_goal="复仇",
            current_location="客栈",
            relationships={
                "c2": RelationshipState(target_character_id="c2", relation_type="敌对", strength=-0.8)
            },
            episodic_summary="发现了真相",
            short_term_buffer=["对话1", "对话2"],
        )
    }
    snap = await sm.create_snapshot("scene-1", "branch-1", states, label="test")
    assert snap.snapshot_id

    restored = await sm.restore_snapshot(snap.snapshot_id)
    assert "c1" in restored
    assert restored["c1"].current_emotion == "愤怒"
    assert restored["c1"].relationships["c2"].relation_type == "敌对"
    assert restored["c1"].short_term_buffer == ["对话1", "对话2"]


@pytest.mark.asyncio
async def test_branch_creation_and_tree():
    sm = SnapshotManager("proj-branch")
    main = await sm.ensure_main_branch()
    assert main.branch_id

    states = {"c1": CharacterState(character_id="c1")}
    snap = await sm.create_snapshot("scene-x", main.branch_id, states)

    forked = await sm.fork_branch(
        snap.snapshot_id, {"tension": "高"}, "支线A", "测试分叉"
    )
    assert forked.parent_branch_id == main.branch_id

    tree = await sm.get_branch_tree("proj-branch")
    assert len(tree.roots) >= 1


@pytest.mark.asyncio
async def test_list_and_delete_snapshot():
    sm = SnapshotManager("proj-del")
    states = {"c1": CharacterState(character_id="c1")}
    snap = await sm.create_snapshot("scene-d", "branch-d", states)

    snaps = await sm.list_snapshots()
    assert any(s["snapshot_id"] == snap.snapshot_id for s in snaps)

    await sm.delete_snapshot(snap.snapshot_id)
    snaps_after = await sm.list_snapshots()
    assert not any(s["snapshot_id"] == snap.snapshot_id for s in snaps_after)
