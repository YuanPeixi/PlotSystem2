"""Inspection 层：角色内部状态的统一只读查询（工单17）。

用户面板、导演智能体、总结智能体要看的是同一份东西（情绪 / 记忆 / 位置 / 关系），
若三方各写各的会出现三套口径不一致的读取路径。本模块是这三方共用的唯一入口，
同时也是快照解析逻辑（CLAUDE.md 契约4 的四级继承）的唯一实现处 ——
`orchestrator._load_inherited_states` 也委托到这里，避免逻辑分叉。

本模块只读：不写 SQLite、不写角色卡；只有显式给出检索词时才会触碰长期记忆
（embedding 调用），默认路径纯磁盘读取。
"""

from __future__ import annotations

from backend.exceptions import SnapshotNotFoundError
from backend.memory import MemoryManager
from backend.models import (
    CharacterInspection,
    CharacterState,
    MemoryChunk,
    Scene,
)
from backend.services import repository
from backend.snapshot import SnapshotManager
from backend.utils.logger import get_logger

logger = get_logger("services.inspection")


async def resolve_scene_states(
    scene: Scene, sm: SnapshotManager
) -> tuple[dict[str, CharacterState], str]:
    """解析某个场景应继承的运行时记忆，返回 (角色状态字典, 来源快照 id)。

    按 CLAUDE.md 契约4 的四级优先级（顺序不可调换）：
    1. `snapshot_id_after`：本场景已跑过一次（continue 续跑）→ 承接上次结束态；
    2. `restore_snapshot_id`：回滚重演场景 → 承接回滚目标快照；
    3. `snapshot_id_before`：本场景已打过前置快照但未跑完（异常恢复）；
    4. 父场景的 `snapshot_id_after`：next_scene → 承接上一场的结束态。
    全部取不到时返回 ({}, "")。
    """
    candidates = [
        scene.snapshot_id_after,
        scene.restore_snapshot_id,
        scene.snapshot_id_before,
    ]
    if scene.parent_scene_id:
        try:
            parent = await repository.get_scene(scene.parent_scene_id)
            candidates.append(parent.snapshot_id_after)
        except Exception:  # noqa: BLE001
            logger.debug("父场景 %s 不可用，跳过记忆继承", scene.parent_scene_id)

    for snapshot_id in candidates:
        if not snapshot_id:
            continue
        try:
            snap = await sm.get_snapshot(snapshot_id)
        except Exception:  # noqa: BLE001
            logger.warning("读取快照 %s 失败，跳过记忆继承", snapshot_id, exc_info=True)
            continue
        if snap and snap.character_states:
            logger.info("场景 %s 从快照 %s 继承运行时记忆", scene.scene_id, snapshot_id)
            return dict(snap.character_states), snapshot_id
    return {}, ""


async def _latest_snapshot_id(
    sm: SnapshotManager, character_id: str, branch_id: str = ""
) -> str:
    """按时间倒序找到第一个含该角色状态的快照 id（list_snapshots 已按 created_at DESC）。"""
    for row in await sm.list_snapshots():
        if branch_id and row.get("branch_id") != branch_id:
            continue
        if character_id in (row.get("character_states") or {}):
            return row.get("snapshot_id", "")
    return ""


async def load_character_state(
    project_id: str,
    character_id: str,
    *,
    scene_id: str = "",
    snapshot_id: str = "",
    branch_id: str = "",
) -> tuple[CharacterState, str]:
    """读取角色在指定时点的状态，返回 (状态, 来源快照 id)。

    时点解析优先级：显式 `snapshot_id` > 指定 `scene_id`（走契约4 四级继承）
    > 该角色最近一次出现的快照（可用 `branch_id` 限定分支）。
    一个快照都没有时（项目刚构建完、场景还没跑过）退回角色卡的当前值，
    此时返回的来源 id 为空字符串。

    短期缓冲与事件摘要是纯内存态，只存在于快照里——这正是旧的
    `GET /characters/{id}/memory` 每次新建 MemoryManager 而恒返回空的原因。
    """
    sm = SnapshotManager(project_id)
    states: dict[str, CharacterState] = {}
    source = ""

    if snapshot_id:
        snap = await sm.get_snapshot(snapshot_id)
        if snap is None:
            raise SnapshotNotFoundError(f"快照不存在: {snapshot_id}")
        states, source = dict(snap.character_states), snapshot_id
    elif scene_id:
        scene = await repository.get_scene(scene_id)
        states, source = await resolve_scene_states(scene, sm)
    else:
        found = await _latest_snapshot_id(sm, character_id, branch_id)
        if found:
            snap = await sm.get_snapshot(found)
            if snap is not None:
                states, source = dict(snap.character_states), found

    state = states.get(character_id)
    if state is not None:
        return state, source

    card = await repository.get_character(project_id, character_id)
    fallback = CharacterState(
        character_id=character_id,
        current_emotion=card.current_emotion,
        current_goal=card.current_goal,
        current_location=card.current_location,
        relationships=card.relationships,
    )
    return fallback, ""


async def inspect_character(
    project_id: str,
    character_id: str,
    *,
    scene_id: str = "",
    snapshot_id: str = "",
    branch_id: str = "",
    memory_query: str = "",
    top_k: int | None = None,
    include_private: bool = True,
) -> CharacterInspection:
    """组装角色的完整可观测视图（人设 + 时点状态 + 三层记忆）。

    include_private=False 会抹掉 `unknown_facts`：调用方若可能把结果送进
    任何角色可见的上下文，必须传 False（契约1）。
    `memory_query` 非空时才检索长期记忆，避免面板每次打开都触发 embedding 调用。
    """
    card = await repository.get_character(project_id, character_id)
    state, source = await load_character_state(
        project_id,
        character_id,
        scene_id=scene_id,
        snapshot_id=snapshot_id,
        branch_id=branch_id,
    )

    hits: list[MemoryChunk] = []
    if memory_query:
        try:
            mem = MemoryManager(character_id, project_id)
            hits = await mem.retrieve(memory_query, top_k)
        except Exception:  # noqa: BLE001
            logger.warning("角色 %s 长期记忆检索失败", character_id, exc_info=True)

    return CharacterInspection(
        character_id=character_id,
        name=card.name,
        persona=card.persona,
        appearance=card.appearance,
        speech_style=card.speech_style,
        current_emotion=state.current_emotion,
        current_goal=state.current_goal,
        current_location=state.current_location,
        relationships=state.relationships or card.relationships,
        known_facts=list(card.known_facts),
        unknown_facts=list(card.unknown_facts) if include_private else [],
        world_lore_entries=list(card.world_lore_entries),
        short_term_buffer=list(state.short_term_buffer),
        episodic_summary=state.episodic_summary,
        long_term_hits=hits,
        source_snapshot_id=source,
        state_source="snapshot" if source else "card",
    )
