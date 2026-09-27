"""工单18：导演分镜稿（storyboard）。

按 §5 验收表组织。几条最容易"假通过"的用例（CONVENTIONS §7 第 4 条）都刻意让写入
来自**两条不同路径**：

- 并发不覆盖 / 两场各带非空 patch：B 必须持有**开场的旧副本**，否则它读到的已经是
  A 写完的新稿，走不到"基于旧稿的 patch"那条分支；
- 用户↔导演冲突：用户的 PUT 发生在**评估那次 LLM 调用期间**（在假 LLM 里触发），
  而不是评估之前 —— 之前改的话导演读到的就是新稿，没有冲突可言；
- 分叉继承边界：来源分支在分叉**之后**再写一次分镜稿。
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.agents import CharacterAgent, director_agent
from backend.agents import character_agent as character_agent_mod
from backend.agents.director_agent import parse_storyboard_patch
from backend.exceptions import ConflictError, InvalidRequestError
from backend.memory import MemoryManager
from backend.models import (
    BeatStatus,
    CharacterCard,
    Project,
    Scene,
    SceneEvaluation,
    StoryBeat,
    Storyboard,
    StoryboardPatch,
    goal_revision,
)
from backend.scene_engine import speaker_selector
from backend.services import events, orchestrator, repository
from backend.services import storyboard as sb
from backend.snapshot import SnapshotManager

GOAL = "揭露叛徒"
REV = goal_revision(GOAL)
PLAN_MARK = "请为剧情推演规划下一个场景"


def _eval_reply(patch: dict | None = None, **extra) -> str:
    data = {
        "synopsis": extra.pop("synopsis", "本场梗概"),
        "narrative_goal_score": 7,
        "dramatic_tension_score": 7,
        "plot_deviation_score": 2,
        "character_consistency_score": 8,
        "story_progress": 0.3,
    }
    if patch is not None:
        data["storyboard_patch"] = patch
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


def _plan_reply() -> str:
    return json.dumps(
        {"name": "下一场", "participating_characters": ["甲", "乙"], "location": "某处"},
        ensure_ascii=False,
    )


class FakeLLM:
    """替换导演 / 角色 / selector 三处 chat_safe，记录每次调用看到的全文。"""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[tuple[str, str]] = []
        # 导演评估的回复：可以是字符串，也可以是 async (prompt) -> str
        self.eval_reply = _eval_reply()

        def text(messages) -> str:
            return "\n".join(m["content"] for m in messages)

        async def director(messages, **kw):
            prompt = text(messages)
            self.calls.append(("director", prompt))
            if PLAN_MARK in prompt:
                return _plan_reply()
            reply = self.eval_reply
            return await reply(prompt) if callable(reply) else reply

        async def character(messages, **kw):
            self.calls.append(("character", text(messages)))
            return "*点头* 好。"

        async def selector(messages, **kw):
            self.calls.append(("selector", text(messages)))
            return json.dumps({"urge": 5, "relevance": 5, "initiative": 5, "reason": "r"})

        monkeypatch.setattr(director_agent, "chat_safe", director)
        monkeypatch.setattr(character_agent_mod, "chat_safe", character)
        monkeypatch.setattr(speaker_selector, "chat_safe", selector)

    def of(self, kind: str) -> list[str]:
        return [t for k, t in self.calls if k == kind]


@pytest.fixture
async def env(monkeypatch, request):
    """项目 + 两个角色 + 主分支，记忆 IO 全部替身。返回 (pid, main_branch_id, llm)。"""
    pid = request.node.name.replace("[", "_").replace("]", "_")
    await repository.save_project(Project(project_id=pid, name=pid, narrative_goal=GOAL))
    for cid, name in (("c1", "甲"), ("c2", "乙")):
        await repository.save_character(
            CharacterCard(project_id=pid, character_id=cid, name=name, known_facts=["公开事实"])
        )
    main = await SnapshotManager(pid).ensure_main_branch()
    monkeypatch.setattr(MemoryManager, "connect", AsyncMock())
    monkeypatch.setattr(MemoryManager, "add_experience", AsyncMock())
    monkeypatch.setattr(MemoryManager, "consolidate", AsyncMock())
    monkeypatch.setattr(CharacterAgent, "retrieve_relevant_memory", AsyncMock(return_value=[]))
    return pid, main.branch_id, FakeLLM(monkeypatch)


def _beats(*titles: str, status: str = BeatStatus.PLANNED.value) -> list[StoryBeat]:
    return [StoryBeat(beat_id=f"b{i}", title=t, status=status) for i, t in enumerate(titles, 1)]


async def _seed_board(pid: str, bid: str, *titles: str, memo: str = "", rev: str = REV) -> Storyboard:
    board = Storyboard(
        project_id=pid, branch_id=bid, outline=_beats(*titles), memo=memo,
        goal_revision=rev, next_beat_seq=len(titles) + 1, revision=1,
    )
    await repository.save_storyboard(board)
    return await repository.get_storyboard(pid, bid)


async def _run(pid: str, bid: str, name: str = "S", *, parent: str | None = None,
               mode: str = "round_robin", turns: int = 1) -> Scene:
    scene = Scene(project_id=pid, branch_id=bid, name=name, parent_scene_id=parent,
                  participating_characters=["c1", "c2"], max_turns=turns, speaker_mode=mode)
    await repository.save_scene(scene)
    await orchestrator.run_scene(scene.scene_id)
    return await repository.get_scene(scene.scene_id)


def _capture_events(monkeypatch) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    real = events.publish

    async def spy(scene_id, event_type, data):
        captured.append((event_type, data))
        await real(scene_id, event_type, data)

    monkeypatch.setattr(events, "publish", spy)
    return captured


# ---------------------------------------------------------------------------
# 契约 1 / 红线 R1：分镜稿只进导演 prompt
# ---------------------------------------------------------------------------


async def test_storyboard_reaches_director_but_never_characters_or_selector(env):
    pid, main, llm = env
    await _seed_board(pid, main, "MARK-密谋揭穿叛徒", memo="MEMO-MARK 乙其实是内鬼")
    await _run(pid, main, mode="selector", turns=2)
    await orchestrator.plan_scene(pid, main)

    director_prompts = llm.of("director")
    assert any(PLAN_MARK in p and "MARK-密谋揭穿叛徒" in p for p in director_prompts)
    assert any(PLAN_MARK not in p and "MEMO-MARK" in p for p in director_prompts)  # 评估
    # 不是空转：角色与 selector 都真的被调用过
    assert llm.of("character") and llm.of("selector")
    for prompt in llm.of("character") + llm.of("selector"):
        assert "MARK-密谋揭穿叛徒" not in prompt and "MEMO-MARK" not in prompt


# ---------------------------------------------------------------------------
# 解析失败不写
# ---------------------------------------------------------------------------


async def test_unparseable_evaluation_leaves_storyboard_and_snapshot_copy_untouched(env):
    pid, main, llm = env
    board = await _seed_board(pid, main, "甲", "乙", memo="原备忘")
    path = repository._storyboard_path(pid, main)
    before = path.read_text(encoding="utf-8")
    llm.eval_reply = "这不是 JSON"

    scene = await _run(pid, main)

    assert path.read_text(encoding="utf-8") == before
    copy = (await SnapshotManager(pid).get_snapshot(scene.snapshot_id_after)).storyboard
    assert [(b.beat_id, b.title, b.status) for b in copy.outline] == [
        (b.beat_id, b.title, b.status) for b in board.outline
    ]
    assert copy.memo == "原备忘"


# ---------------------------------------------------------------------------
# 导演写：合并、补写快照、changelog
# ---------------------------------------------------------------------------


async def test_director_patch_merges_and_backfills_after_snapshot(env):
    pid, main, llm = env
    await _seed_board(pid, main, "查账", "对质")
    llm.eval_reply = _eval_reply({
        "complete": ["b1"],
        "add": [{"title": "设局", "description": "引叛徒现身"}],
        "memo": "乙的动机还没交代",
    })
    scene = await _run(pid, main)

    live = await repository.get_storyboard(pid, main)
    assert [(b.beat_id, b.title, b.status) for b in live.outline] == [
        ("b1", "查账", "done"), ("b2", "对质", "planned"), ("b3", "设局", "planned"),
    ]
    assert live.outline[0].resolved_scene_id == scene.scene_id
    assert live.memo == "乙的动机还没交代"
    assert live.changelog[-1].source == "director" and live.changelog[-1].scene_id == scene.scene_id
    # 后置快照补写成合并后的那份：从它分叉的分支要带着本场的路线图调整
    copy = (await SnapshotManager(pid).get_snapshot(scene.snapshot_id_after)).storyboard
    assert [b.title for b in copy.outline] == ["查账", "对质", "设局"]
    # 前置快照是开场那份
    before = (await SnapshotManager(pid).get_snapshot(scene.snapshot_id_before)).storyboard
    assert [b.title for b in before.outline] == ["查账", "对质"]
    # 评估副本里不带 patch：它随谱系被复制进每个快照
    assert all(
        sb.is_patch_empty(r.evaluation.storyboard_patch)
        for r in await orchestrator._story_records(scene)
    )


async def test_first_director_beats_stamp_the_goal_revision_seen(env):
    """路线图在第一场评估之后建立；首次产生节拍时写入当次看到的目标版本。"""
    pid, main, llm = env
    llm.eval_reply = _eval_reply({"add": [{"title": "起"}]})
    await _run(pid, main)
    live = await repository.get_storyboard(pid, main)
    assert [b.title for b in live.outline] == ["起"]
    assert live.goal_revision == REV


# ---------------------------------------------------------------------------
# 并发：B 持有开场旧副本
# ---------------------------------------------------------------------------


async def _after_snapshot(pid: str, bid: str) -> str:
    snap = await SnapshotManager(pid).create_snapshot("s", bid, {}, label="after:t")
    return snap.snapshot_id


def _eval_with(patch: StoryboardPatch, scene_id: str) -> SceneEvaluation:
    return SceneEvaluation(scene_id=scene_id, storyboard_patch=patch)


async def test_empty_patch_from_stale_copy_does_not_erase_earlier_merge(env):
    """验收"并发不覆盖"：A 的 patch 先落盘，B 拿着开场旧副本以空 patch 收尾。"""
    pid, main, _ = env
    opening = await _seed_board(pid, main, "查账")
    snap_id = await _after_snapshot(pid, main)
    sm = SnapshotManager(pid)
    a = Scene(scene_id="A", project_id=pid, branch_id=main)
    b = Scene(scene_id="B", project_id=pid, branch_id=main)

    await orchestrator._apply_storyboard_patch(
        a, _eval_with(StoryboardPatch(add=[StoryBeat(title="设局")]), "A"), opening, REV, snap_id, sm
    )
    await orchestrator._apply_storyboard_patch(
        b, _eval_with(StoryboardPatch(), "B"), opening, REV, snap_id, sm
    )
    live = await repository.get_storyboard(pid, main)
    assert [b.title for b in live.outline] == ["查账", "设局"]
    assert [b.title for b in (await sm.get_snapshot(snap_id)).storyboard.outline] == ["查账", "设局"]


async def test_two_non_empty_patches_from_same_stale_copy_both_apply(env, monkeypatch):
    """验收"两场各带非空 patch"：各改不同节拍都生效；B 的备忘改写基于旧备忘，被跳过留痕。"""
    pid, main, _ = env
    opening = await _seed_board(pid, main, "查账", "对质", memo="旧备忘")
    snap_id = await _after_snapshot(pid, main)
    sm = SnapshotManager(pid)
    real_get = repository.get_storyboard

    async def yielding_get(*args):
        board = await real_get(*args)
        await asyncio.sleep(0)  # 在读与写之间让出控制权，否则单线程下测不出并发窗口
        return board

    monkeypatch.setattr(repository, "get_storyboard", yielding_get)
    await asyncio.gather(
        orchestrator._apply_storyboard_patch(
            Scene(scene_id="A", project_id=pid, branch_id=main),
            _eval_with(StoryboardPatch(complete=["b1"], memo="A 的备忘"), "A"),
            opening, REV, snap_id, sm,
        ),
        orchestrator._apply_storyboard_patch(
            Scene(scene_id="B", project_id=pid, branch_id=main),
            _eval_with(StoryboardPatch(complete=["b2"], memo="B 的备忘"), "B"),
            opening, REV, snap_id, sm,
        ),
    )
    live = await real_get(pid, main)
    assert [(b.beat_id, b.status) for b in live.outline] == [("b1", "done"), ("b2", "done")]
    assert live.memo == "A 的备忘"
    assert any("跳过" in c.summary and "备忘已被他人修改" in c.summary for c in live.changelog)


async def test_user_edit_during_evaluation_wins_conflicting_director_ops(env, monkeypatch):
    """验收"用户↔导演冲突"：PUT 发生在评估那次 LLM 调用期间。"""
    pid, main, llm = env
    await _seed_board(pid, main, "查账", "对质", "设局", memo="旧备忘")
    captured = _capture_events(monkeypatch)

    async def reply_after_user_edit(prompt: str) -> str:
        current = await repository.get_storyboard(pid, main)
        outline = [StoryBeat(beat_id=b.beat_id, title=b.title, status=b.status)
                   for b in current.outline]
        outline[1].status = BeatStatus.DROPPED.value
        await orchestrator.update_storyboard(
            pid, main, outline, "用户的备忘", base_revision=current.revision
        )
        return _eval_reply(
            {"memo": "导演的备忘", "complete": ["b2", "b1"], "update": [
                {"beat_id": "b3", "title": "设局（改）"}
            ]},
            world_state_delta={"季节": "冬"},
        )

    llm.eval_reply = reply_after_user_edit
    scene = await _run(pid, main)

    live = await repository.get_storyboard(pid, main)
    assert live.memo == "用户的备忘"
    assert [(b.beat_id, b.title, b.status) for b in live.outline] == [
        ("b1", "查账", "done"), ("b2", "对质", "dropped"), ("b3", "设局（改）", "planned"),
    ]
    skipped = [c for c in live.changelog if c.summary.startswith("跳过")]
    assert skipped and "b2" in skipped[-1].summary and "备忘" in skipped[-1].summary
    errors = [d for e, d in captured if e == "scene_error"]
    assert any(not d["fatal"] and "分镜稿" in d["message"] for d in errors)
    # 本场评估与世界变量照常保留
    assert (await repository.get_evaluation(scene.scene_id)) is not None
    assert (await repository.get_world_state(pid, main)).variables == {"季节": "冬"}
    assert (await repository.get_scene(scene.scene_id)).status == "completed"


# ---------------------------------------------------------------------------
# 节拍身份与重排（纯函数）
# ---------------------------------------------------------------------------


def _board(*titles: str, memo: str = "") -> Storyboard:
    return Storyboard(project_id="p", branch_id="b", outline=_beats(*titles), memo=memo,
                      goal_revision=REV, next_beat_seq=len(titles) + 1)


def test_complete_by_id_survives_user_rename_and_reorder():
    base = _board("查账", "对质", "设局")
    current = _board("查账", "对质", "设局")
    current.outline = [current.outline[2], current.outline[0], current.outline[1]]
    current.outline[1].title = "查账（改名）"
    merge = sb.merge_storyboard_patch(
        current, base, StoryboardPatch(complete=["b1"]), scene_id="s", seen_revision=REV
    )
    done = [b for b in merge.storyboard.outline if b.status == "done"]
    assert [(b.beat_id, b.title) for b in done] == [("b1", "查账（改名）")]


async def test_unknown_beat_id_is_not_treated_as_new(env, caplog):
    pid, main, _ = env
    opening = await _seed_board(pid, main, "查账")
    snap_id = await _after_snapshot(pid, main)
    await orchestrator._apply_storyboard_patch(
        Scene(scene_id="A", project_id=pid, branch_id=main),
        _eval_with(StoryboardPatch(complete=["b99"], update=[StoryBeat(beat_id="b42", title="x")]), "A"),
        opening, REV, snap_id, SnapshotManager(pid),
    )
    live = await repository.get_storyboard(pid, main)
    assert [b.beat_id for b in live.outline] == ["b1"]
    assert "b99" in caplog.text and "未知节拍" in caplog.text


def test_director_cannot_touch_beat_it_never_saw():
    """当前稿里恰好有同名 ID（别人后加的），没见过它的导演也不能改它。"""
    base = _board("查账")
    current = _board("查账", "用户新加")
    merge = sb.merge_storyboard_patch(
        current, base, StoryboardPatch(complete=["b2"]), scene_id="s", seen_revision=REV
    )
    assert merge.storyboard.outline[1].status == "planned"
    assert merge.skipped


def test_deleted_beat_ids_are_never_reused():
    board = _board("查账", "对质")  # next_beat_seq = 3
    board.revision = 1
    edited, _ = sb.apply_user_edit(
        board, [StoryBeat(beat_id="b1", title="查账")], "", base_revision=1,
        confirm_goal=False, current_goal_revision=REV,
    )
    merge = sb.merge_storyboard_patch(
        edited, edited, StoryboardPatch(add=[StoryBeat(title="新节拍")]), scene_id="s",
        seen_revision=REV,
    )
    assert [b.beat_id for b in merge.storyboard.outline] == ["b1", "b3"]
    again, _ = sb.apply_user_edit(
        merge.storyboard,
        [*merge.storyboard.outline, StoryBeat(title="用户新加")],
        "", base_revision=merge.storyboard.revision, confirm_goal=False,
        current_goal_revision=REV,
    )
    assert [b.beat_id for b in again.outline] == ["b1", "b3", "b4"]


def test_reorder_full_permutation_keeps_ids_and_resolved_positions():
    base = _board("甲", "乙", "丙", "丁")
    base.outline[0].status = "done"
    merge = sb.merge_storyboard_patch(
        base, base, StoryboardPatch(reorder=["b4", "b2", "b3"]), scene_id="s", seen_revision=REV
    )
    assert [b.beat_id for b in merge.storyboard.outline] == ["b1", "b4", "b2", "b3"]
    assert merge.applied == ["重排路线图"]


@pytest.mark.parametrize("order", [
    ["b2", "b3"],               # 缺一个
    ["b2", "b3", "b4", "b9"],   # 多一个
    ["b2", "b2", "b3"],         # 重复
])
def test_reorder_rejects_non_permutations(order):
    base = _board("甲", "乙", "丙")
    merge = sb.merge_storyboard_patch(
        base, base, StoryboardPatch(reorder=order), scene_id="s", seen_revision=REV
    )
    assert [b.beat_id for b in merge.storyboard.outline] == ["b1", "b2", "b3"]
    assert merge.skipped and not merge.applied


def test_reorder_rejected_when_planned_set_changed_since_read():
    base = _board("甲", "乙", "丙")
    current = _board("甲", "乙", "丙")
    current.outline[2].status = "dropped"
    merge = sb.merge_storyboard_patch(
        current, base, StoryboardPatch(reorder=["b3", "b2", "b1"]), scene_id="s", seen_revision=REV
    )
    assert [b.beat_id for b in merge.storyboard.outline] == ["b1", "b2", "b3"]
    assert "已被他人调整" in merge.skipped[0]


def test_reorder_skipped_when_user_reordered_since_read():
    """重排的前提是**顺序**没被改过，不只是集合：只比集合的话，用户刚调好的顺序
    会被导演基于旧稿的重排悄悄覆盖，且没有任何冲突记录。"""
    base = _board("甲", "乙", "丙")
    current = _board("甲", "乙", "丙")
    current.outline = [current.outline[2], current.outline[0], current.outline[1]]
    merge = sb.merge_storyboard_patch(
        current, base, StoryboardPatch(reorder=["b2", "b1", "b3"]), scene_id="s", seen_revision=REV
    )
    assert [b.beat_id for b in merge.storyboard.outline] == ["b3", "b1", "b2"]
    assert "已被他人调整" in merge.skipped[0]


def test_reorder_matching_current_order_is_idempotent():
    """别人已经排成了导演想要的顺序：不算冲突，也不算一次改动。"""
    base = _board("甲", "乙", "丙")
    current = _board("甲", "乙", "丙")
    current.outline = [current.outline[1], current.outline[0], current.outline[2]]
    merge = sb.merge_storyboard_patch(
        current, base, StoryboardPatch(reorder=["b2", "b1", "b3"]), scene_id="s", seen_revision=REV
    )
    assert [b.beat_id for b in merge.storyboard.outline] == ["b2", "b1", "b3"]
    assert not merge.skipped and not merge.applied


def test_parse_patch_is_strict_about_booleans_and_shapes():
    assert parse_storyboard_patch({"goal_realigned": "false"}).goal_realigned is False
    assert parse_storyboard_patch({"goal_realigned": "true"}).goal_realigned is True
    patch = parse_storyboard_patch({
        "add": ["字符串节拍", {"title": "对象节拍"}, 3],
        "complete": ["b1", None, 2],
        "update": [{"title": "没有 ID 的改写"}],
        "reorder": "不是列表",
        "memo": None,
    })
    assert [b.title for b in patch.add] == ["字符串节拍", "对象节拍"]
    assert patch.complete == ["b1", "2"]
    assert patch.update == [] and patch.reorder is None and patch.memo is None
    assert sb.is_patch_empty(parse_storyboard_patch("垃圾"))


# ---------------------------------------------------------------------------
# 分叉与继承
# ---------------------------------------------------------------------------


async def test_fork_inherits_snapshot_copy_not_later_source_writes(env):
    pid, main, llm = env
    llm.eval_reply = _eval_reply({"add": [{"title": "主线节拍"}]})
    scene = await _run(pid, main)
    branch, _ = await orchestrator.fork_from_snapshot(pid, scene.snapshot_id_after, "IF")

    # 来源分支在分叉之后再写一次（另一条路径：用户 PUT）
    src = await repository.get_storyboard(pid, main)
    await orchestrator.update_storyboard(
        pid, main, [*src.outline, StoryBeat(title="分叉后写入")], src.memo,
        base_revision=src.revision,
    )

    forked = await repository.get_storyboard(pid, branch.branch_id)
    assert [b.title for b in forked.outline] == ["主线节拍"]
    assert forked.fork_origin is not None and forked.fork_origin.source_branch_id == main
    assert forked.changelog[-1].source == "fork"


async def test_fork_from_legacy_snapshot_starts_empty_with_warning(env, caplog):
    pid, main, _ = env
    await _seed_board(pid, main, "来源分支当前的节拍")
    legacy = await SnapshotManager(pid).create_snapshot("s", main, {}, label="after:旧")
    assert (await SnapshotManager(pid).get_snapshot(legacy.snapshot_id)).storyboard is None

    branch, _ = await orchestrator.fork_from_snapshot(pid, legacy.snapshot_id, "旧格式")
    forked = await repository.get_storyboard(pid, branch.branch_id)
    assert forked.outline == []  # 不回读来源分支的当前分镜稿
    assert forked.fork_origin is not None
    assert "没有分镜稿副本" in caplog.text


async def test_fork_origin_reaches_director_prompts_without_llm_calls(env):
    pid, main, llm = env
    scene = await _run(pid, main)
    calls_before = len(llm.calls)
    branch, scene0 = await orchestrator.fork_from_snapshot(
        pid, scene.snapshot_id_after, "IF：公主知情",
        conditions={"公主": "已知情"}, director_notes="让公主提前知道真相",
    )
    assert len(llm.calls) == calls_before  # 红线 R4：分叉路径零 LLM 调用

    await orchestrator.run_scene(scene0.scene_id)
    await orchestrator.plan_scene(pid, branch.branch_id)
    eval_prompt = [p for p in llm.of("director") if PLAN_MARK not in p][-1]
    plan_prompt = [p for p in llm.of("director") if PLAN_MARK in p][-1]
    for prompt in (eval_prompt, plan_prompt):
        assert "让公主提前知道真相" in prompt
        assert "公主=已知情" in prompt
        assert "主线" in prompt  # 来源分支名


async def test_plan_after_fork_sees_history_before_fork_point(env):
    """验收"规划同源"：分叉分支首场跑完后规划下一场，prompt 里有分叉点之前的梗概。"""
    pid, main, llm = env
    llm.eval_reply = _eval_reply(synopsis="主线第一场的梗概")
    a = await _run(pid, main, "A")
    branch, scene0 = await orchestrator.fork_from_snapshot(pid, a.snapshot_id_after, "IF")
    llm.eval_reply = _eval_reply(synopsis="分叉首场的梗概")
    await orchestrator.run_scene(scene0.scene_id)

    await orchestrator.plan_scene(pid, branch.branch_id)
    plan_prompt = [p for p in llm.of("director") if PLAN_MARK in p][-1]
    assert "主线第一场的梗概" in plan_prompt
    assert "分叉首场的梗概" in plan_prompt


# ---------------------------------------------------------------------------
# 预算两道闸门
# ---------------------------------------------------------------------------


async def test_hand_edited_oversize_file_is_clamped_on_read_only(env, caplog):
    pid, main, _ = env
    path = repository._storyboard_path(pid, main)
    raw = {
        "outline": [
            {"beat_id": "b1" if i else "坏ID", "title": f"节拍{i}\n第二行", "description": "说" * 400}
            for i in range(30)
        ],
        "memo": "备忘" * 2000,
        "revision": "不是数字",
    }
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    board = await repository.get_storyboard(pid, main)
    assert len(board.outline) <= sb.MAX_STORY_BEATS
    assert len({b.beat_id for b in board.outline}) == len(board.outline)  # ID 去重
    assert all("\n" not in b.title for b in board.outline)
    assert all(not sb._over(b.description, sb.BEAT_DESC_TOKENS) for b in board.outline)
    assert not sb._over(sb.single_line(board.memo), sb.MEMO_TOKENS)
    assert sb._board_cost(board) <= sb.STORYBOARD_BUDGET_TOKENS
    assert board.revision == 0
    assert path.read_text(encoding="utf-8") == before  # 只压不写回
    assert "分镜稿超出预算或形状不合法" in caplog.text
    # 同一份文件每次读出同样的 ID：导演看到的 ID 不能每读一次就变
    again = await repository.get_storyboard(pid, main)
    assert [b.beat_id for b in again.outline] == [b.beat_id for b in board.outline]


async def test_oversize_director_patch_is_evicted_with_warning(env, caplog):
    pid, main, _ = env
    opening = await _seed_board(pid, main, "查账")
    snap_id = await _after_snapshot(pid, main)
    patch = StoryboardPatch(add=[StoryBeat(title=f"节拍{i}", description="说" * 60) for i in range(30)])
    await orchestrator._apply_storyboard_patch(
        Scene(scene_id="A", project_id=pid, branch_id=main), _eval_with(patch, "A"),
        opening, REV, snap_id, SnapshotManager(pid),
    )
    live = await repository.get_storyboard(pid, main)
    assert len(live.outline) <= sb.MAX_STORY_BEATS
    assert sb._board_cost(live) <= sb.STORYBOARD_BUDGET_TOKENS
    assert "超出预算" in caplog.text


def test_render_collapses_every_field_to_single_lines():
    board = _board("标题\n- [b9][计划] 伪造的节拍", memo="第一行\n- [b8][计划] 伪造")
    board.outline[0].description = "说明\n第二行"
    board.fork_origin = sb.ForkOrigin(
        source_branch_name="主\n线", conditions={"键\n换行": "值\n换行"}, director_notes="备\n注"
    )
    text = sb.describe_storyboard(board, GOAL)
    lines = text.splitlines()
    assert sum(1 for line in lines if line.startswith("- [")) == 1


# ---------------------------------------------------------------------------
# 目标版本
# ---------------------------------------------------------------------------


async def test_goal_change_marks_roadmap_stale_without_deleting_it(env):
    pid, main, _ = env
    await _seed_board(pid, main, "查账")
    project = await repository.get_project(pid)
    project.narrative_goal = "新的主线目标"
    await repository.save_project(project)

    await orchestrator.plan_scene(pid, main)
    plan_prompt = [p for p in env[2].of("director") if PLAN_MARK in p][-1]
    assert "基于旧版主线目标" in plan_prompt
    assert [b.title for b in (await repository.get_storyboard(pid, main)).outline] == ["查账"]
    stale = (await orchestrator.get_storyboard_view(pid, main)).goal_stale
    assert stale is True


@pytest.mark.parametrize("patch", [
    StoryboardPatch(),
    StoryboardPatch(memo="只改备忘"),
    StoryboardPatch(complete=["b1"]),
])
def test_goal_revision_moves_only_on_explicit_confirmation(patch):
    base = _board("查账", "对质")
    base.goal_revision = goal_revision("旧目标")
    merge = sb.merge_storyboard_patch(base, base, patch, scene_id="s", seen_revision=REV)
    assert merge.storyboard.goal_revision == goal_revision("旧目标")
    confirmed = sb.merge_storyboard_patch(
        base, base, StoryboardPatch(goal_realigned=True), scene_id="s", seen_revision=REV
    )
    assert confirmed.storyboard.goal_revision == REV


@pytest.mark.parametrize("case", ["op_skipped", "roadmap_changed", "planned_evicted"])
def test_goal_confirmation_needs_the_roadmap_the_director_confirmed(case):
    """确认是对导演读到的那一版路线图的判断。三种情况各自单独成立，任一都挡住确认：
    本次有节拍操作被跳过 / 路线图在导演读取后被他人改过 / 计划中节拍被预算淘汰。"""
    base = _board("查账", "对质")
    base.goal_revision = goal_revision("旧目标")
    current = deepcopy(base)
    patch = StoryboardPatch(goal_realigned=True)
    if case == "op_skipped":
        patch.complete = ["b99"]
    elif case == "roadmap_changed":
        current.outline[1].title = "用户改过"
    else:
        patch.add = [StoryBeat(title=f"节拍{i}", description="说" * 60) for i in range(30)]
    merge = sb.merge_storyboard_patch(current, base, patch, scene_id="s", seen_revision=REV)
    assert merge.storyboard.goal_revision == goal_revision("旧目标")
    assert any(s.startswith("确认已按当前主线目标重排") for s in merge.skipped)
    assert sb.is_goal_stale(merge.storyboard, GOAL)


async def test_parse_failure_does_not_confirm_goal(env):
    pid, main, llm = env
    await _seed_board(pid, main, "查账", rev=goal_revision("旧目标"))
    llm.eval_reply = "不是 JSON goal_realigned: true"
    await _run(pid, main)
    assert (await repository.get_storyboard(pid, main)).goal_revision == goal_revision("旧目标")


async def test_goal_changed_during_evaluation_writes_back_the_revision_seen(env):
    pid, main, llm = env
    await _seed_board(pid, main, "查账", rev=goal_revision("旧目标"))

    async def reply_then_change_goal(prompt: str) -> str:
        assert "基于旧版主线目标" in prompt
        project = await repository.get_project(pid)
        project.narrative_goal = "评估期间又改的目标"
        await repository.save_project(project)
        return _eval_reply({"goal_realigned": True})

    llm.eval_reply = reply_then_change_goal
    await _run(pid, main)
    live = await repository.get_storyboard(pid, main)
    assert live.goal_revision == REV  # 评估 prompt 看到的是 GOAL
    stale = (await orchestrator.get_storyboard_view(pid, main)).goal_stale
    assert stale is True  # 下一场照样提示过期


# ---------------------------------------------------------------------------
# 用户读写（HTTP）
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():
    from backend.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


def _url(pid: str, bid: str) -> str:
    return f"/projects/{pid}/branches/{bid}/storyboard"


async def test_get_missing_storyboard_returns_empty_not_404(env, client):
    pid, main, _ = env
    resp = await client.get(_url(pid, main))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["outline"] == [] and data["revision"] == 0 and data["goal_stale"] is False


async def test_put_storyboard_versioning_validation_and_replay(env, client):
    pid, main, _ = env
    body = {"outline": [{"title": "查账"}, {"title": "对质", "description": "当面"}],
            "memo": "第一行\n第二行", "revision": 0}

    first = await client.put(_url(pid, main), json=body)
    assert first.status_code == 200
    data = first.json()["data"]
    assert [b["beat_id"] for b in data["outline"]] == ["b1", "b2"]
    assert data["revision"] == 1 and data["memo"] == "第一行\n第二行"
    assert [c["source"] for c in data["changelog"]] == ["user"]
    assert data["goal_revision"] == REV  # 首次产生节拍，盖上当前目标版本

    # 内容与当前完全相同：修订号已过期也不报冲突（无操作，不记 changelog）
    replay = await client.put(_url(pid, main), json={
        **body, "outline": [{"beat_id": "b1", "title": "查账"},
                            {"beat_id": "b2", "title": "对质", "description": "当面"}],
    })
    assert replay.status_code == 200
    assert len(replay.json()["data"]["changelog"]) == 1

    stale = await client.put(_url(pid, main), json={**body, "memo": "别的内容"})
    assert stale.status_code == 409

    too_long = await client.put(_url(pid, main), json={
        "outline": [{"title": "长" * 200}], "memo": "", "revision": 1,
    })
    assert too_long.status_code == 422

    unknown = await client.put(_url(pid, main), json={
        "outline": [{"beat_id": "b77", "title": "查账"}], "memo": "", "revision": 1,
    })
    assert unknown.status_code == 422

    bad_status = await client.put(_url(pid, main), json={
        "outline": [{"title": "查账", "status": "finished"}], "memo": "", "revision": 1,
    })
    assert bad_status.status_code == 422

    missing_branch = await client.put(_url(pid, "no-such-branch"), json=body)
    assert missing_branch.status_code == 404

    board = await repository.get_storyboard(pid, main)
    assert board.revision == 1 and len(board.changelog) == 1


async def test_put_retry_with_identical_body_is_replayed_by_request_id(env, client):
    """契约5：响应丢失时客户端拿不到后端分配的新节拍 ID，只能**原样**重发。
    不认幂等键的话，这份请求与当前内容永远判不等（当前稿里新节拍已有 ID），只会 409。"""
    pid, main, _ = env
    body = {"outline": [{"title": "查账"}, {"title": "对质"}], "memo": "",
            "revision": 0, "request_id": "req-1"}
    first = await client.put(_url(pid, main), json=body)
    assert first.status_code == 200

    # 重试到达之前，分镜稿又被另一次编辑推进了（另一条路径：不同的幂等键）
    now = await repository.get_storyboard(pid, main)
    await orchestrator.update_storyboard(
        pid, main, now.outline, "后来的备忘", base_revision=now.revision, request_id="req-2"
    )

    retry = await client.put(_url(pid, main), json=body)
    assert retry.status_code == 200
    data = retry.json()["data"]
    assert [b["beat_id"] for b in data["outline"]] == ["b1", "b2"]  # 没有再建一遍
    assert data["memo"] == "后来的备忘"  # 也没有覆盖后来的编辑
    assert data["revision"] == 2 and len(data["changelog"]) == 2

    reused = await client.put(_url(pid, main), json={**body, "memo": "换了内容"})
    assert reused.status_code == 422


async def test_put_goal_confirmation_is_explicit(env, client):
    pid, main, _ = env
    await _seed_board(pid, main, "查账", rev=goal_revision("旧目标"))
    outline = [{"beat_id": "b1", "title": "查账"}]

    memo_only = await client.put(_url(pid, main), json={
        "outline": outline, "memo": "只改备忘", "revision": 1,
    })
    assert memo_only.status_code == 200
    assert memo_only.json()["data"]["goal_revision"] == goal_revision("旧目标")
    assert memo_only.json()["data"]["goal_stale"] is True

    confirmed = await client.put(_url(pid, main), json={
        "outline": outline, "memo": "只改备忘", "revision": 2, "confirm_goal": True,
        "goal_revision_seen": REV,
    })
    assert confirmed.status_code == 200
    data = confirmed.json()["data"]
    assert data["goal_revision"] == REV and data["goal_stale"] is False


async def test_put_goal_confirmation_writes_back_the_revision_user_saw(env, client):
    """读取时目标是 A → 另一个页面把目标改成 B → 原页面确认。确认的是 A，
    写回 B 就把旧稿标成了适配一个用户根本没看过的目标。"""
    pid, main, _ = env
    await _seed_board(pid, main, "查账", rev=goal_revision("旧目标"))
    view = (await client.get(_url(pid, main))).json()["data"]
    assert view["narrative_goal"] == GOAL and view["current_goal_revision"] == REV
    assert view["goal_stale"] is True

    project = await repository.get_project(pid)
    project.narrative_goal = "另一个页面改的目标"
    await repository.save_project(project)

    body = {"outline": [{"beat_id": "b1", "title": "查账"}], "memo": "", "revision": 1,
            "confirm_goal": True, "goal_revision_seen": view["current_goal_revision"]}
    resp = await client.put(_url(pid, main), json=body)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["goal_revision"] == REV
    assert data["goal_stale"] is True and data["narrative_goal"] == "另一个页面改的目标"

    missing = await client.put(_url(pid, main), json={
        **body, "revision": 2, "goal_revision_seen": "",
    })
    assert missing.status_code == 422


def test_user_edit_rejects_budget_overflow_instead_of_truncating():
    board = _board("查账")
    with pytest.raises(InvalidRequestError):
        sb.apply_user_edit(board, [StoryBeat(title="查账")], "备" * 5000, base_revision=0,
                           confirm_goal=False, current_goal_revision=REV)
    with pytest.raises(InvalidRequestError):
        sb.apply_user_edit(board, [StoryBeat(title=f"节拍{i}") for i in range(30)], "",
                           base_revision=0, confirm_goal=False, current_goal_revision=REV)
    with pytest.raises(ConflictError):
        sb.apply_user_edit(board, [StoryBeat(title="新的")], "", base_revision=5,
                           confirm_goal=False, current_goal_revision=REV)
