"""分支级长期记忆的继承与承接（工单08 review 修复）。

这些用例都跑真实 Chroma，只把 embedding 换成确定性假向量：记忆是否真的被读到，
必须由向量库本身回答，mock 掉集合层的测试无法证明继承完整。
"""

from __future__ import annotations

import pytest

from backend.memory import MemoryManager, long_term
from backend.models import CharacterState, Scene
from backend.services import inspection, repository
from backend.snapshot import SnapshotManager


@pytest.fixture
def fake_embedding(monkeypatch):
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
    return _FakeEmbedding


async def _seed(project_id: str, character_id: str, branch_id: str, text: str) -> MemoryManager:
    mem = MemoryManager(character_id, project_id, branch_id)
    await mem.connect()
    assert mem.long_term._collection is not None, "前置条件：Chroma 必须真的连上"
    await mem.long_term.add(text)
    return mem


@pytest.mark.asyncio
async def test_legacy_project_collection_is_adopted(fake_embedding):
    """升级前写在项目级集合里的记忆，第一次以分支身份连接时应被承接。"""
    project_id, character_id = "proj-legacy", "char-legacy"
    await _seed(project_id, character_id, "", "旧世界：老国王把王位传给了次子")

    mem = MemoryManager(character_id, project_id, "branch-main")
    await mem.connect()
    hits = [c.text for c in await mem.long_term.retrieve("王位", top_k=5)]

    assert "旧世界：老国王把王位传给了次子" in hits


@pytest.mark.asyncio
async def test_adoption_happens_only_once(fake_embedding):
    """承接是一次性的：再次连接不得把老集合重复灌进来。"""
    project_id, character_id = "proj-legacy2", "char-legacy2"
    await _seed(project_id, character_id, "", "旧集合内容")
    first = MemoryManager(character_id, project_id, "branch-x")
    await first.connect()
    await first.long_term.add("分支自己的内容")

    again = MemoryManager(character_id, project_id, "branch-x")
    await again.connect()

    assert again.long_term._collection.count() == 2


@pytest.mark.asyncio
async def test_forked_branch_never_adopts_post_fork_memory(fake_embedding):
    """分叉点之后写进共享集合的记忆，不能被新分支当成"待承接的历史"灌进来。"""
    project_id, character_id = "proj-future", "char-future"
    legacy = await _seed(project_id, character_id, "", "分叉前：国王仍在位")
    sm = SnapshotManager(project_id)
    snap = await sm.create_snapshot(
        "scene-future", "branch-src", {character_id: CharacterState(character_id=character_id)}
    )
    await legacy.long_term.add("分叉后：国王驾崩")

    await sm.clone_collections_for_branch(snap.snapshot_id, "branch-if")
    mem = MemoryManager(character_id, project_id, "branch-if")
    await mem.connect()
    hits = [c.text for c in await mem.long_term.retrieve("国王", top_k=5)]

    assert "分叉前：国王仍在位" in hits
    assert "分叉后：国王驾崩" not in hits


@pytest.mark.asyncio
async def test_interrupted_adoption_resumes_on_next_connect(fake_embedding, monkeypatch):
    """承接中断（半截 + 期间还写了新记忆）后，下次连接必须继续补齐。"""
    project_id, character_id = "proj-resume", "char-resume"
    legacy = await _seed(project_id, character_id, "", "老记忆一")
    await legacy.long_term.add("老记忆二")

    real_copy = long_term.copy_collection

    def half_then_die(src_col, dst_col, _batch):
        page = src_col.get(limit=1, include=["documents", "metadatas", "embeddings"])
        dst_col.upsert(
            ids=page["ids"],
            documents=page["documents"],
            metadatas=page["metadatas"],
            embeddings=page["embeddings"],
        )
        raise RuntimeError("killed mid-copy")

    monkeypatch.setattr(long_term, "copy_collection", half_then_die)
    broken = MemoryManager(character_id, project_id, "branch-r")
    await broken.connect()
    await broken.long_term.add("承接期间产生的新记忆")
    assert broken.long_term._collection.count() < 3, "前置条件：承接确实被打断"

    monkeypatch.setattr(long_term, "copy_collection", real_copy)
    healed = MemoryManager(character_id, project_id, "branch-r")
    await healed.connect()

    assert healed.long_term._collection.count() == 3


@pytest.mark.asyncio
async def test_scene_query_uses_scene_branch(fake_embedding):
    """场景查询按场景自身分支取记忆：新分支首场的 restore_snapshot 属于来源分支。"""
    project_id, character_id = "proj-scene-branch", "char-scene-branch"
    sm = SnapshotManager(project_id)
    snap = await sm.create_snapshot(
        "scene-src", "branch-main", {character_id: CharacterState(character_id=character_id)}
    )
    scene = Scene(
        scene_id="scene-if-first",
        project_id=project_id,
        branch_id="branch-if",
        restore_snapshot_id=snap.snapshot_id,
    )
    await repository.save_scene(scene)

    resolved = await inspection._resolve_branch(
        project_id,
        branch_id="",
        snapshot_id="",
        scene_id=scene.scene_id,
        source_snapshot_id=snap.snapshot_id,
    )

    assert resolved == "branch-if"


@pytest.mark.asyncio
async def test_fork_copies_absent_characters_memory(fake_embedding):
    """分叉必须搬走来源分支全部角色的记忆，不能只搬本场参演者。"""
    project_id, src_branch = "proj-absent", "branch-src"
    await _seed(project_id, "char-a", src_branch, "A 在第二场说的话")
    await _seed(project_id, "char-b", src_branch, "B 在第一场说的话")

    sm = SnapshotManager(project_id)
    # 快照只记录本场参演者 char-a，char-b 这场没出场
    snap = await sm.create_snapshot(
        "scene-absent",
        src_branch,
        {"char-a": CharacterState(character_id="char-a")},
    )
    copied = await sm.clone_collections_for_branch(snap.snapshot_id, "branch-new")

    assert copied == 2
    mem_b = MemoryManager("char-b", project_id, "branch-new")
    await mem_b.connect()
    hits = [c.text for c in await mem_b.long_term.retrieve("第一场", top_k=5)]
    assert "B 在第一场说的话" in hits


@pytest.mark.asyncio
async def test_copy_collection_respects_batch_size(fake_embedding):
    """超过单批上限的集合要分页搬完，不能整组失败。"""
    from backend.memory.long_term import copy_collection

    project_id, character_id = "proj-batch", "char-batch"
    mem = await _seed(project_id, character_id, "src-branch", "第 0 条记忆")
    for i in range(1, 7):
        await mem.long_term.add(f"第 {i} 条记忆")

    dst = MemoryManager(character_id, project_id, "dst-branch")
    await dst.connect()
    moved = copy_collection(mem.long_term._collection, dst.long_term._collection, 2)

    assert moved == 7
    assert dst.long_term._collection.count() == 7


@pytest.mark.asyncio
async def test_clone_reports_failure_instead_of_silent_success(fake_embedding, monkeypatch):
    """复制失败必须抛错：静默成功会产出一条"看似正常但角色失忆"的分支。"""
    from backend.exceptions import MemoryError as MemoryCopyError

    project_id, src_branch = "proj-clone-fail", "branch-src"
    await _seed(project_id, "char-f", src_branch, "会复制失败的记忆")
    sm = SnapshotManager(project_id)
    snap = await sm.create_snapshot(
        "scene-fail", src_branch, {"char-f": CharacterState(character_id="char-f")}
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("chroma write failed")

    monkeypatch.setattr("backend.memory.long_term.copy_collection", boom)

    with pytest.raises(MemoryCopyError):
        await sm.clone_collections_for_branch(snap.snapshot_id, "branch-new")


@pytest.mark.asyncio
async def test_resolve_branch_uses_snapshot_source(fake_embedding):
    """按快照查询时，长期记忆的分支要与状态来源一致（不能退回共享集合）。"""
    project_id = "proj-resolve"
    sm = SnapshotManager(project_id)
    snap = await sm.create_snapshot(
        "scene-resolve", "branch-resolve", {"char-r": CharacterState(character_id="char-r")}
    )

    explicit = await inspection._resolve_branch(
        project_id,
        branch_id="",
        snapshot_id=snap.snapshot_id,
        scene_id="",
        source_snapshot_id="",
    )
    # 默认时点（既没给场景也没给快照）靠解析出的最近一个快照定分支
    latest = await inspection._resolve_branch(
        project_id,
        branch_id="",
        snapshot_id="",
        scene_id="",
        source_snapshot_id=snap.snapshot_id,
    )

    assert explicit == "branch-resolve"
    assert latest == "branch-resolve"
