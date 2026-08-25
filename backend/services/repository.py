"""持久化仓储：Project / Character / Scene / Evaluation 的读写。

Project / Scene / Evaluation 元数据存 SQLite；
CharacterCard 以 JSON 文件存于项目目录（便于人工编辑与快照）。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.config import settings
from backend.exceptions import (
    CharacterNotFoundError,
    ProjectNotFoundError,
    SceneNotFoundError,
)
from backend.models import (
    CharacterCard,
    DialogueTurn,
    DirectorDecision,
    LoreEntry,
    Project,
    RelationshipState,
    Scene,
    SceneEvaluation,
    SpeakerMode,
    now,
)
from backend.utils import db
from backend.utils.serializer import to_json


def _characters_dir(project_id: str) -> Path:
    d = settings.project_dir(project_id) / "characters"
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


async def get_project(project_id: str) -> Project:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM projects WHERE project_id = ?", (project_id,)
        )
        row = await cur.fetchone()
    if not row:
        raise ProjectNotFoundError(f"项目不存在: {project_id}")
    data = json.loads(row[0])
    return Project(
        project_id=data["project_id"],
        name=data["name"],
        description=data.get("description", ""),
        seed_texts=list(data.get("seed_texts", []) or []),
        status=data.get("status", "initializing"),
    )


async def list_projects() -> list[Project]:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM projects ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
    projects = []
    for (data_json,) in rows:
        data = json.loads(data_json)
        projects.append(
            Project(
                project_id=data["project_id"],
                name=data["name"],
                description=data.get("description", ""),
                seed_texts=list(data.get("seed_texts", []) or []),
                status=data.get("status", "initializing"),
            )
        )
    return projects


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
        turns_completed=data.get("turns_completed", 0),
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


async def get_evaluation(scene_id: str) -> SceneEvaluation | None:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM evaluations WHERE scene_id = ?", (scene_id,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    data = json.loads(row[0])
    return SceneEvaluation(
        scene_id=data.get("scene_id", scene_id),
        synopsis=data.get("synopsis", ""),
        narrative_goal_score=data.get("narrative_goal_score", 0.0),
        dramatic_tension_score=data.get("dramatic_tension_score", 0.0),
        plot_deviation_score=data.get("plot_deviation_score", 0.0),
        character_consistency_score=data.get("character_consistency_score", 0.0),
        recommended_decision=data.get("recommended_decision", "next_scene"),
        rollback_suggestion=data.get("rollback_suggestion"),
    )


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
