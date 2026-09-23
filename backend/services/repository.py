"""持久化仓储：Project / Character / Scene / Evaluation / WorldState 的读写。

Project / Scene / Evaluation 元数据存 SQLite；
CharacterCard 与分支世界变量以 JSON 文件存于项目目录（便于人工编辑与快照）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from backend.config import settings
from backend.exceptions import (
    CharacterNotFoundError,
    ProjectNotFoundError,
    SceneNotFoundError,
)
from backend.models import (
    PROGRESS_UNAVAILABLE,
    CharacterCard,
    DialogueTurn,
    DirectorDecision,
    LoreEntry,
    Project,
    RelationshipState,
    Scene,
    SceneEvaluation,
    SpeakerMode,
    WorldState,
    now,
)
from backend.services.world_state import clamp_world_variables
from backend.utils import db
from backend.utils.logger import get_logger
from backend.utils.serializer import to_json

logger = get_logger("services.repository")


def _characters_dir(project_id: str) -> Path:
    d = settings.project_dir(project_id) / "characters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _world_state_dir(project_id: str) -> Path:
    d = settings.project_dir(project_id) / "world_state"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


async def save_project(project: Project) -> None:
    settings.project_dir(project.project_id).mkdir(parents=True, exist_ok=True)
    (settings.project_dir(project.project_id) / "seed_texts").mkdir(exist_ok=True)
    project.updated_at = now()
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO projects "
            "(project_id, name, description, status, created_at, updated_at, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project.project_id,
                project.name,
                project.description,
                project.status,
                project.created_at.isoformat(),
                project.updated_at.isoformat(),
                to_json(project),
            ),
        )
        await conn.commit()


def _deserialize_project(data: dict) -> Project:
    """从 data_json 还原项目。

    get_project 与 list_projects 共用同一份：两处各自内联时，新字段只补一处
    就会在列表接口上静默丢失（CLAUDE.md §5.4）。旧项目的 data_json 没有新字段，
    全部走默认值。
    """
    return Project(
        project_id=data["project_id"],
        name=data["name"],
        description=data.get("description", ""),
        seed_texts=list(data.get("seed_texts", []) or []),
        status=data.get("status", "initializing"),
        narrative_goal=data.get("narrative_goal", ""),
        ending_criteria=data.get("ending_criteria", ""),
    )


async def get_project(project_id: str) -> Project:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM projects WHERE project_id = ?", (project_id,)
        )
        row = await cur.fetchone()
    if not row:
        raise ProjectNotFoundError(f"项目不存在: {project_id}")
    return _deserialize_project(json.loads(row[0]))


async def list_projects() -> list[Project]:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM projects ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
    return [_deserialize_project(json.loads(data_json)) for (data_json,) in rows]


async def delete_project(project_id: str) -> None:
    import shutil

    async with db.connect() as conn:
        # decisions/evaluations 以 scene_id 为键，需先按项目场景清理
        await conn.execute(
            "DELETE FROM decisions WHERE scene_id IN "
            "(SELECT scene_id FROM scenes WHERE project_id = ?)",
            (project_id,),
        )
        for table in ("scenes", "branches", "snapshots", "projects"):
            await conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
        await conn.execute("DELETE FROM outputs WHERE project_id = ?", (project_id,))
        await conn.commit()
    pdir = settings.project_dir(project_id)
    if pdir.exists():
        shutil.rmtree(pdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Character
# ---------------------------------------------------------------------------


def _deserialize_card(data: dict) -> CharacterCard:
    lore = [LoreEntry(**e) for e in (data.get("world_lore_entries") or [])]
    rels = {
        k: RelationshipState(**v)
        for k, v in (data.get("relationships") or {}).items()
    }
    return CharacterCard(
        character_id=data["character_id"],
        project_id=data["project_id"],
        name=data["name"],
        persona=data.get("persona", ""),
        appearance=data.get("appearance", ""),
        speech_style=data.get("speech_style", ""),
        world_lore_entries=lore,
        known_facts=list(data.get("known_facts", []) or []),
        unknown_facts=list(data.get("unknown_facts", []) or []),
        relationships=rels,
        current_emotion=data.get("current_emotion", "平静"),
        current_goal=data.get("current_goal", ""),
        current_location=data.get("current_location", ""),
    )


async def save_character(card: CharacterCard) -> None:
    path = _characters_dir(card.project_id) / f"{card.character_id}.json"
    path.write_text(to_json(card), encoding="utf-8")


async def get_character(project_id: str, character_id: str) -> CharacterCard:
    path = _characters_dir(project_id) / f"{character_id}.json"
    if not path.exists():
        raise CharacterNotFoundError(f"角色不存在: {character_id}")
    return _deserialize_card(json.loads(path.read_text(encoding="utf-8")))


async def list_characters(project_id: str) -> list[CharacterCard]:
    cards = []
    for f in _characters_dir(project_id).glob("*.json"):
        cards.append(_deserialize_card(json.loads(f.read_text(encoding="utf-8"))))
    return cards


# ---------------------------------------------------------------------------
# Scene
# ---------------------------------------------------------------------------


def _parse_created_at(raw: object, label: str = "场景创建时间") -> datetime:
    """还原时间戳，损坏值降级为当前时间。

    本函数其余字段一律用 .get(默认值) 降级，创建时间不该是唯一的硬失败点：
    手工编辑过 data_json、或早于本字段落地的旧行，会让整个 list_scenes 抛
    ValueError 五百，而不是只让这一场的排序退化。
    """
    if not raw:
        return now()
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        logger.warning("%s无法解析，按当前时间处理：%r", label, raw)
        return now()


def _deserialize_scene(data: dict) -> Scene:
    log = [
        DialogueTurn(
            turn_id=t.get("turn_id", ""),
            scene_id=t.get("scene_id", ""),
            turn_number=t.get("turn_number", 0),
            character_id=t.get("character_id", ""),
            character_name=t.get("character_name", ""),
            dialogue=t.get("dialogue"),
            action=t.get("action"),
            inner_thought=t.get("inner_thought"),
            memory_context_used=list(t.get("memory_context_used", []) or []),
            selector_notice=t.get("selector_notice", ""),
        )
        for t in (data.get("dialogue_log") or [])
    ]
    return Scene(
        scene_id=data["scene_id"],
        project_id=data["project_id"],
        branch_id=data.get("branch_id", ""),
        parent_scene_id=data.get("parent_scene_id"),
        name=data.get("name", ""),
        description=data.get("description", ""),
        participating_characters=list(data.get("participating_characters", []) or []),
        location=data.get("location", ""),
        initial_conditions=data.get("initial_conditions", {}) or {},
        max_turns=data.get("max_turns", 20),
        status=data.get("status", "pending"),
        snapshot_id_before=data.get("snapshot_id_before", ""),
        snapshot_id_after=data.get("snapshot_id_after"),
        restore_snapshot_id=data.get("restore_snapshot_id", ""),
        inherited_story_history=data.get("inherited_story_history"),
        created_at=_parse_created_at(data.get("created_at")),
        turns_completed=data.get("turns_completed", 0),
        turns_consolidated=data.get("turns_consolidated", 0),
        speaker_mode=data.get("speaker_mode", SpeakerMode.ROUND_ROBIN.value),
        dialogue_log=log,
    )


async def save_scene(scene: Scene) -> None:
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO scenes "
            "(scene_id, project_id, branch_id, parent_scene_id, name, status, created_at, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scene.scene_id,
                scene.project_id,
                scene.branch_id,
                scene.parent_scene_id,
                scene.name,
                scene.status,
                scene.created_at.isoformat(),
                to_json(scene),
            ),
        )
        await conn.commit()


async def get_scene(scene_id: str) -> Scene:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM scenes WHERE scene_id = ?", (scene_id,)
        )
        row = await cur.fetchone()
    if not row:
        raise SceneNotFoundError(f"场景不存在: {scene_id}")
    return _deserialize_scene(json.loads(row[0]))


async def list_scenes(project_id: str, branch_id: str | None = None) -> list[Scene]:
    query = "SELECT data_json FROM scenes WHERE project_id = ?"
    params: list = [project_id]
    if branch_id:
        query += " AND branch_id = ?"
        params.append(branch_id)
    query += " ORDER BY created_at"
    async with db.connect() as conn:
        cur = await conn.execute(query, tuple(params))
        rows = await cur.fetchall()
    return [_deserialize_scene(json.loads(r[0])) for r in rows]


async def list_scenes_by_status(status: str) -> list[Scene]:
    """按状态列跨项目查询场景（服务启动时对账遗留的 running 场景用）。"""
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM scenes WHERE status = ?", (status,)
        )
        rows = await cur.fetchall()
    return [_deserialize_scene(json.loads(r[0])) for r in rows]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


async def save_evaluation(evaluation: SceneEvaluation) -> None:
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO evaluations (scene_id, created_at, data_json) "
            "VALUES (?, ?, ?)",
            (evaluation.scene_id, now().isoformat(), to_json(evaluation)),
        )
        await conn.commit()


def _deserialize_evaluation(data: dict, scene_id: str) -> SceneEvaluation:
    """从 data_json 还原评估。

    旧记录没有主线度量字段，推进度必须退到 PROGRESS_UNAVAILABLE 而不是 0.0：
    后者会把“没度量过”伪装成“一点没推进”，又会让后续场次被判成停滞。
    """
    return SceneEvaluation(
        scene_id=data.get("scene_id", scene_id),
        synopsis=data.get("synopsis", ""),
        narrative_goal_score=data.get("narrative_goal_score", 0.0),
        dramatic_tension_score=data.get("dramatic_tension_score", 0.0),
        plot_deviation_score=data.get("plot_deviation_score", 0.0),
        character_consistency_score=data.get("character_consistency_score", 0.0),
        recommended_decision=data.get("recommended_decision", "next_scene"),
        rollback_suggestion=data.get("rollback_suggestion"),
        story_progress=data.get("story_progress", PROGRESS_UNAVAILABLE),
        story_progress_raw=data.get("story_progress_raw", PROGRESS_UNAVAILABLE),
        progress_stalled=bool(data.get("progress_stalled", False)),
        goal_revision=data.get("goal_revision", ""),
        is_ending_reached=bool(data.get("is_ending_reached", False)),
        ending_reason=data.get("ending_reason", ""),
        unresolved_threads=list(data.get("unresolved_threads", []) or []),
        # None 是"删除该变量"的标记，必须原样保留：压成空串会让删除意图变成
        # 一次"把变量改成空值"的更新，旧值反而永远留在世界状态里。
        world_state_delta=dict(data.get("world_state_delta") or {}),
        evaluated_snapshot_id=data.get("evaluated_snapshot_id", ""),
    )


async def get_evaluation(scene_id: str) -> SceneEvaluation | None:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM evaluations WHERE scene_id = ?", (scene_id,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    return _deserialize_evaluation(json.loads(row[0]), scene_id)


# ---------------------------------------------------------------------------
# WorldState（工单07：分支级世界变量）
# ---------------------------------------------------------------------------


def _deserialize_world_state(data: dict, project_id: str, branch_id: str) -> WorldState:
    """从 JSON 还原世界状态（§5.4 第 2 步）。

    这里就把变量压回预算（`clamp_world_variables`）：文件摆在项目目录里、明确支持
    人工编辑，而 `merge_world_variables` 只拦得住导演写进来的那条路径。手写一条
    五千字的变量、或是塞进三百条，都会绕过写入侧闸门直接进**每一场、每个角色、
    每一轮**的 system prompt。值统一转成单行字符串：写进去的数字/布尔会原样进
    角色 prompt，而下游一律按字符串拼接、按"一行一条"渲染。

    **只压不写回**：这是读路径，不该因为一次读取就改掉用户手编的文件；下一次
    合并落盘时超限的内容自然收敛。
    """
    variables, dropped = clamp_world_variables(data.get("variables"))
    if dropped:
        logger.warning(
            "分支 %s 的世界状态文件超出预算或含保留字，本次读取已忽略 %d 项：%s",
            branch_id,
            len(dropped),
            "、".join(dropped),
        )
    return WorldState(
        project_id=data.get("project_id", project_id),
        branch_id=data.get("branch_id", branch_id),
        variables=variables,
        updated_at=_parse_created_at(data.get("updated_at"), "世界状态更新时间"),
    )


async def get_world_state(project_id: str, branch_id: str) -> WorldState:
    """读取分支的世界变量。文件不存在返回空状态，不报错。

    老项目、主分支、以及本功能上线前建立的分支都走这条降级路径：
    世界变量为空 = 与本功能上线前的行为完全一致。
    """
    path = _world_state_dir(project_id) / f"{branch_id}.json"
    if not path.exists():
        return WorldState(project_id=project_id, branch_id=branch_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 世界变量是增量演化的附加层，读不出来不该让整场推演起不来
        logger.warning("分支 %s 的世界状态文件损坏，按空世界状态处理", branch_id, exc_info=True)
        return WorldState(project_id=project_id, branch_id=branch_id)
    return _deserialize_world_state(data, project_id, branch_id)


async def save_world_state(state: WorldState) -> None:
    """落盘分支世界变量。branch_id 为空时拒绝写入 —— 那会退化成项目级共享文件，
    两条 IF 线互相污染（与长期记忆的 collection 后缀同理，见 §4.2 陷阱 11）。
    """
    if not state.branch_id:
        raise ValueError("保存世界状态必须指定 branch_id")
    state.updated_at = now()
    path = _world_state_dir(state.project_id) / f"{state.branch_id}.json"
    path.write_text(to_json(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# Decision（工单13：决策幂等保护）
# ---------------------------------------------------------------------------


async def save_decision(scene_id: str, decision: DirectorDecision) -> None:
    """持久化已生效的决策结果。scene_id 为主键，重放请求据此返回相同结果。"""
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO decisions "
            "(scene_id, decision_type, next_scene_id, created_at, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                scene_id,
                decision.decision_type,
                decision.next_scene_id,
                now().isoformat(),
                to_json(decision),
            ),
        )
        await conn.commit()


async def get_decision(scene_id: str) -> DirectorDecision | None:
    """读取场景已生效的决策，不存在时返回 None。"""
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM decisions WHERE scene_id = ?", (scene_id,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    data = json.loads(row[0])
    # next_scene_config 不还原：重放场景下调用方只需要结果字段（next_scene_id 等）
    return DirectorDecision(
        decision_type=data.get("decision_type", "next_scene"),
        extra_turns=data.get("extra_turns"),
        next_scene_id=data.get("next_scene_id"),
        rollback_to_snapshot_id=data.get("rollback_to_snapshot_id"),
        new_initial_conditions=data.get("new_initial_conditions"),
        next_scene_description=data.get("next_scene_description"),
        next_participating_characters=data.get("next_participating_characters"),
        next_location=data.get("next_location"),
        next_initial_conditions=data.get("next_initial_conditions"),
        rollback_notes=data.get("rollback_notes"),
    )


async def try_mark_scene_deciding(scene_id: str) -> bool:
    """CAS 守卫：仅当场景处于 completed 状态时，将状态列置为 'deciding'。

    借助 SQLite 写锁保证跨进程/多 worker 下的原子性：并发的第二个请求
    会因状态列已变为 'deciding' 而更新 0 行，返回 False。

    注意 'deciding' 只写在 scenes 表的 status 列上（不写入 data_json），
    get_scene/list_scenes 从 data_json 反序列化，因此该瞬态值对 API
    响应不可见，也无需加入 SceneStatus 枚举。
    """
    async with db.connect() as conn:
        cur = await conn.execute(
            "UPDATE scenes SET status = 'deciding' "
            "WHERE scene_id = ? AND status = 'completed'",
            (scene_id,),
        )
        await conn.commit()
        return cur.rowcount > 0


async def clear_scene_deciding(scene_id: str) -> None:
    """释放 CAS 守卫：仅当状态列仍为 'deciding' 时恢复为 'completed'。

    条件更新使 continue 分支（save_scene 已将状态改为 pending）不被误覆盖。
    """
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE scenes SET status = 'completed' "
            "WHERE scene_id = ? AND status = 'deciding'",
            (scene_id,),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


async def save_output(output_id: str, project_id: str, fmt: str, content: str) -> None:
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO outputs (output_id, project_id, format, created_at, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (output_id, project_id, fmt, now().isoformat(), content),
        )
        await conn.commit()


async def get_output(output_id: str) -> dict | None:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT output_id, project_id, format, created_at, content FROM outputs WHERE output_id = ?",
            (output_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "output_id": row[0],
        "project_id": row[1],
        "format": row[2],
        "created_at": row[3],
        "content": row[4],
    }
