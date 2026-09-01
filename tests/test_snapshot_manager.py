"""SnapshotManager 测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

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


def test_fork_memory_initialization_has_no_hard_chromadb_dependency(tmp_path):
    """契约6：没有安装 Chroma 时仍能确定一个空起点，且不在导入阶段崩溃。"""
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import os
        import sys
        from pathlib import Path

        os.environ["DATA_DIR"] = sys.argv[1]
        sys.modules["chromadb"] = None

        from backend.models import CharacterState
        from backend.snapshot import SnapshotManager
        from backend.utils.branch_memory import is_fork_initialized
        from backend.utils.db import init_db

        async def main():
            await init_db()
            sm = SnapshotManager("proj-no-chroma")
            snap = await sm.create_snapshot(
                "scene-no-chroma",
                "branch-main",
                {"char-a": CharacterState(character_id="char-a")},
            )
            checkpoint = Path(sys.argv[1]) / "fake-checkpoint"
            checkpoint.mkdir(parents=True)
            (checkpoint / "chroma.sqlite3").touch()
            meta = (
                Path(sys.argv[1])
                / "projects"
                / "proj-no-chroma"
                / "snapshots"
                / snap.snapshot_id
                / "meta.json"
            )
            payload = json.loads(meta.read_text(encoding="utf-8"))
            payload["chroma_checkpoint"] = str(checkpoint)
            meta.write_text(json.dumps(payload), encoding="utf-8")

            copied = await sm.clone_collections_for_branch(
                snap.snapshot_id, "branch-no-chroma"
            )
            live = Path(sys.argv[1]) / "projects" / "proj-no-chroma" / "chroma_db"
            assert copied == 0
            assert is_fork_initialized(live, "branch-no-chroma")

        asyncio.run(main())
        """
    )
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo_root), env.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
