"""持久化仓储：Project / Character / Scene / Evaluation / WorldState / Storyboard 的读写。

Project / Scene / Evaluation 元数据存 SQLite；
CharacterCard、分支世界变量与分支分镜稿以 JSON 文件存于项目目录（便于人工编辑与快照）。
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from backend.config import settings
from backend.exceptions import (
    CharacterNotFoundError,
    ObjectNotFoundError,
    ProjectNotFoundError,
    SceneNotFoundError,
)
from backend.models import (
    PROGRESS_UNAVAILABLE,
    BeatStatus,
    CharacterCard,
    DecisionSource,
    DialogueTurn,
    DirectorDecision,
    ForkOrigin,
    LoreEntry,
    Project,
    RelationshipState,
    Scene,
    SceneEvaluation,
    SceneLineage,
    SpeakerMode,
    StoryBeat,
    Storyboard,
    StoryboardChange,
    StoryboardPatch,
    StoryboardSource,
    StoryRecord,
    TurnKind,
    WorldObject,
    WorldState,
    goal_revision,
    now,
)
from backend.services.objects import MAX_PROJECT_OBJECTS, clamp_object
from backend.services.storyboard import clamp_storyboard
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


def _objects_dir(project_id: str) -> Path:
    d = settings.project_dir(project_id) / "objects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_text(target: Path, payload: str) -> None:
    """原子替换目标文件。临时名唯一且短于目标名（CLAUDE.md §10.1，同
    `snapshot_manager._atomic_write_json`；那边 import 了本模块，不能反向复用）。"""
    tmp = target.with_name(f".{target.stem[:16]}.{uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
# WorldObject（工单24：项目级物件，文件存储，同角色卡）
# ---------------------------------------------------------------------------

#: 手工建的文件可以叫 `crown.json`，所以不要求 UUID；但不能含路径分隔符
_OBJECT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _deserialize_object(
    data: object, object_id: str, project_id: str, character_names: set[str]
) -> WorldObject | None:
    """还原物件并压回预算。返回 None 表示这份文件不构成一个物件（非对象 / 无名称）。

    以文件名为准而不是 JSON 里的 `object_id`：手工复制一份文件改个名是最常见的编辑方式，
    信 JSON 里的 id 会让两个文件冒充同一个物件，后写者覆盖前者。
    """
    if not isinstance(data, dict):
        return None
    if data.get("object_id") not in (None, object_id):
        logger.warning("物件文件 %s 内的 object_id=%r 与文件名不一致，以文件名为准", object_id, data.get("object_id"))
    obj = WorldObject(
        object_id=object_id,
        project_id=project_id,
        name=str(data.get("name") or ""),
        aliases=data.get("aliases"),  # type: ignore[arg-type]  # 交给 clamp_object 规整
        public_description=str(data.get("public_description") or ""),
        hidden_rules=data.get("hidden_rules"),  # type: ignore[arg-type]
        visibility=data.get("visibility"),  # type: ignore[arg-type]
        revision=max(0, _safe_int(data.get("revision"), 0)),
        request_id=str(data.get("request_id") or ""),
        request_digest=str(data.get("request_digest") or ""),
        created_at=_parse_created_at(data.get("created_at"), "物件创建时间"),
        updated_at=_parse_created_at(data.get("updated_at"), "物件更新时间"),
    )
    issues = clamp_object(obj, character_names)
    if issues:
        # 只压不写回：读路径不改用户手编的文件，下一次合法写入时自然收敛
        logger.warning("物件 %s（%s）读取时已压回预算：%s", obj.name, object_id, "；".join(issues))
    if not obj.name:
        logger.warning("物件文件 %s 没有名称，已忽略", object_id)
        return None
    return obj


def _read_object_file(path: Path, project_id: str, character_names: set[str]) -> WorldObject | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 一个坏文件不能让整个物件列表五百
        logger.warning("物件文件 %s 损坏，已忽略", path.name, exc_info=True)
        return None
    return _deserialize_object(data, path.stem, project_id, character_names)


async def _character_names(project_id: str) -> set[str]:
    return {c.name.strip() for c in await list_characters(project_id) if c.name.strip()}


async def list_objects(project_id: str) -> list[WorldObject]:
    """按创建时间列出物件。超过 `MAX_PROJECT_OBJECTS` 的部分不返回（warning），
    手工往目录里塞几百个文件也不会把下游的候选集撑爆。"""
    names = await _character_names(project_id)
    objects = [
        obj
        for f in sorted(_objects_dir(project_id).glob("*.json"))
        if (obj := _read_object_file(f, project_id, names)) is not None
    ]
    objects.sort(key=lambda o: (o.created_at, o.object_id))
    if len(objects) > MAX_PROJECT_OBJECTS:
        logger.warning(
            "项目 %s 有 %d 个物件，超过上限 %d，只取最早创建的 %d 个",
            project_id, len(objects), MAX_PROJECT_OBJECTS, MAX_PROJECT_OBJECTS,
        )
        objects = objects[:MAX_PROJECT_OBJECTS]
    return objects


async def find_object(project_id: str, object_id: str) -> WorldObject | None:
    # object_id 来自 URL 路径参数，拼进文件路径前必须挡住 `../`
    if not _OBJECT_ID_RE.fullmatch(object_id or ""):
        return None
    path = _objects_dir(project_id) / f"{object_id}.json"
    if not path.is_file():
        return None
    return _read_object_file(path, project_id, await _character_names(project_id))


async def get_object(project_id: str, object_id: str) -> WorldObject:
    obj = await find_object(project_id, object_id)
    if obj is None:
        raise ObjectNotFoundError(f"物件不存在: {object_id}")
    return obj


async def save_object(obj: WorldObject) -> None:
    if not obj.project_id or not obj.object_id:
        raise ValueError("保存物件必须指定 project_id 与 object_id")
    _atomic_write_text(_objects_dir(obj.project_id) / f"{obj.object_id}.json", to_json(obj))


async def delete_object(project_id: str, object_id: str) -> bool:
    """删除物件文件，返回是否真的删了（不存在不报错：删除天然幂等）。"""
    if not _OBJECT_ID_RE.fullmatch(object_id or ""):
        return False
    path = _objects_dir(project_id) / f"{object_id}.json"
    if not path.is_file():
        return False
    path.unlink()
    return True


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


def _turn_kind(raw: object) -> str:
    """缺键（工单24/20 之前的旧轮次）即角色轮次；非法取值同样按角色轮次并 warning ——
    当成环境回合的话，它会从 max_turns 与轮询选人里消失。"""
    if raw is None:
        return TurnKind.CHARACTER.value
    # 先判类型再查集合：[] / {} 不可哈希，`in` 会抛 TypeError，一条坏轮次就让整个
    # list_scenes 五百（同陷阱 16"一条坏记录不能让 list_scenes 五百"）
    if isinstance(raw, str) and raw in {k.value for k in TurnKind}:
        return raw
    logger.warning("轮次 kind 取值非法，按角色轮次处理：%r", raw)
    return TurnKind.CHARACTER.value


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
            kind=_turn_kind(t.get("kind")),
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
        inherited_story_history=deserialize_story_history(
            data.get("inherited_story_history"), f"场景 {data['scene_id']}"
        ),
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


async def recent_scenes_on_branch(
    project_id: str, branch_id: str, limit: int = 5
) -> list[Scene]:
    """本分支最近 `limit` 场（按创建顺序，旧→新）。

    规划用它定位"接在哪一场之后"与选角兜底；**不是**规划的历史来源 ——
    前情走因果谱系（工单18 §3.3），否则分叉分支看不到分叉点之前的剧情。
    """
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM scenes WHERE project_id = ? AND branch_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, branch_id, limit),
        )
        rows = await cur.fetchall()
    return [_deserialize_scene(json.loads(r[0])) for r in reversed(rows)]


async def list_scenes_by_status(status: str) -> list[Scene]:
    """按状态列跨项目查询场景（服务启动时对账遗留的 running 场景用）。"""
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM scenes WHERE status = ?", (status,)
        )
        rows = await cur.fetchall()
    return [_deserialize_scene(json.loads(r[0])) for r in rows]


async def list_scene_lineage(project_id: str) -> list[SceneLineage]:
    """按创建顺序列出全项目场景的谱系字段（工单18 D2）。

    导演历史的回溯每次都要看全项目（手建场景靠"本分支上一场"兜底），原先走
    `list_scenes` 会把每一场的完整 `dialogue_log` 读进应用层再反序列化。这里在
    SQL 侧用 `json_extract` 只取回溯用得到的字段 —— 仍然从 `data_json` 取而不是读
    同名的索引列，守住"data_json 是唯一真相源"（§5.1）。

    `inherited_story_history` 可能是数组、null、缺键或被手改成标量：`json_extract`
    对标量返回的是裸值而非 JSON 文本，所以要配合 `json_type` 判断再决定是否解析。
    """
    field = "$.inherited_story_history"
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT scene_id, "
            "json_extract(data_json, '$.branch_id'), "
            "json_extract(data_json, '$.parent_scene_id'), "
            "json_extract(data_json, '$.name'), "
            "json_extract(data_json, '$.restore_snapshot_id'), "
            f"json_type(data_json, '{field}'), "
            f"json_extract(data_json, '{field}') "
            "FROM scenes WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        )
        rows = await cur.fetchall()
    result: list[SceneLineage] = []
    for scene_id, branch_id, parent_id, name, restore_id, history_type, history in rows:
        if history_type in ("array", "object"):
            raw: object = json.loads(history)
        elif history_type in (None, "null"):
            raw = None
        else:
            raw = history
        result.append(
            SceneLineage(
                scene_id=scene_id,
                branch_id=branch_id or "",
                parent_scene_id=parent_id,
                name=name or "",
                restore_snapshot_id=restore_id or "",
                inherited_story_history=deserialize_story_history(raw, f"场景 {scene_id}"),
            )
        )
    return result


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
        goal_missing=_goal_missing(data),
        is_ending_reached=bool(data.get("is_ending_reached", False)),
        ending_reason=data.get("ending_reason", ""),
        unresolved_threads=list(data.get("unresolved_threads", []) or []),
        # None 是"删除该变量"的标记，必须原样保留：压成空串会让删除意图变成
        # 一次"把变量改成空值"的更新，旧值反而永远留在世界状态里。
        world_state_delta=dict(data.get("world_state_delta") or {}),
        evaluated_snapshot_id=data.get("evaluated_snapshot_id", ""),
        storyboard_patch=_deserialize_storyboard_patch(data.get("storyboard_patch")),
    )


def _goal_missing(data: dict) -> bool:
    """评估时有没有主线目标。字段上线前的记录按它记下的目标版本回填。

    空目标下写入的评估，`goal_revision` 恰是空串的版本；不回填的话，正是那些
    "没填目标就跑起来"的老项目看不到无锚点提示。没有版本的更老记录不猜。
    """
    if "goal_missing" in data:
        return data["goal_missing"] is True
    revision = data.get("goal_revision", "")
    return bool(revision) and revision == goal_revision("")


def _deserialize_storyboard_patch(data: object) -> StoryboardPatch:
    """还原评估里记录的分镜稿 patch（仅供追溯；合并早已在评估完成时做完）。"""
    if not isinstance(data, dict):
        return StoryboardPatch()

    def _ids(key: str) -> list[str]:
        raw = data.get(key)
        return [str(x) for x in raw] if isinstance(raw, list) else []

    def _beats(key: str) -> list[StoryBeat]:
        raw = data.get(key)
        items = raw if isinstance(raw, list) else []
        return [b for b in (_deserialize_beat(x) for x in items) if b is not None]

    reorder = data.get("reorder")
    memo = data.get("memo")
    return StoryboardPatch(
        add=_beats("add"),
        complete=_ids("complete"),
        drop=_ids("drop"),
        update=_beats("update"),
        reorder=[str(x) for x in reorder] if isinstance(reorder, list) else None,
        memo=str(memo) if memo is not None else None,
        goal_realigned=data.get("goal_realigned") is True,
        rejected=_ids("rejected"),
    )


def _deserialize_story_record(data: object) -> StoryRecord:
    """还原导演历史里的一条记录。结构不对就抛异常，由调用方跳过这一条。

    评估内容复用 `_deserialize_evaluation`，额外补两处副本特有的防线：

    - **线索的存在性**：`unresolved_threads` 缺键或不是列表记为 `threads_known=False`，
      `_story_context` 据此继续往前找。`_deserialize_evaluation` 自己把缺键默认成 `[]`
      —— 那是 evaluations 表一侧的既有口径（工单18 D1 注明不扩大、不统一），但副本
      不能照搬：[] 在这里是"线索已清空"的权威值；
    - **推进度必须是有限实数**：副本可被人工编辑，字符串进来会在 `_story_context`
      的比较处抛 TypeError，NaN 会被当成合法进度（§4.2 陷阱 18）。降级为不可用。
    """
    if not isinstance(data, dict):
        raise TypeError(f"记录不是对象：{type(data).__name__}")
    raw_ev = data.get("evaluation")
    if not isinstance(raw_ev, dict):
        raise TypeError(f"evaluation 不是对象：{type(raw_ev).__name__}")
    scene_id = str(data.get("scene_id") or "")
    evaluation = _deserialize_evaluation(raw_ev, scene_id)
    threads_known = isinstance(raw_ev.get("unresolved_threads"), list)
    if not threads_known:
        # 字符串被 list() 拆成单字、None 被压成 []，都不是真实的线索
        evaluation.unresolved_threads = []
    if "threads_known" in data:
        # 本字段落地后写出的记录：以显式值为准（未知过的记录序列化后键是 []，
        # 只看键在不在会把它误判为已知）
        threads_known = bool(data["threads_known"]) and threads_known
    progress = evaluation.story_progress
    if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(progress):
        logger.warning("导演历史记录 %s 的推进度无效，按不可用处理：%r", scene_id, progress)
        evaluation.story_progress = PROGRESS_UNAVAILABLE
    return StoryRecord(
        scene_id=scene_id,
        name=str(data.get("name") or ""),
        evaluation=evaluation,
        threads_known=threads_known,
    )


def deserialize_story_history(raw: object, owner: str) -> list[StoryRecord] | None:
    """还原 `Scene.inherited_story_history` / `Snapshot.story_history`（工单18 D1）。

    - `None` 原样返回：它是"旧数据，请回溯推断"的哨兵，与权威空历史 `[]` 不能混同
      （§4.2 陷阱 16）；
    - 损坏的条目跳过并 warning：一条坏记录不能让整个 `list_scenes` 五百（降级要成片）；
    - 整个容器都不是列表时按权威空历史处理，而不是 None —— None 会触发回溯，
      去读**当前**的来源快照，可能越过分叉边界读进后来才发生的剧情。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        logger.warning("%s 的导演历史不是列表，按空历史处理：%r", owner, type(raw).__name__)
        return []
    records: list[StoryRecord] = []
    for i, item in enumerate(raw):
        try:
            records.append(_deserialize_story_record(item))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 的导演历史第 %d 条损坏，已跳过：%s", owner, i, exc)
    return records


async def get_evaluation(scene_id: str) -> SceneEvaluation | None:
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT data_json FROM evaluations WHERE scene_id = ?", (scene_id,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    return _deserialize_evaluation(json.loads(row[0]), scene_id)


#: 单条 SQL 的 IN 列表长度上限。老版本 SQLite 的绑定参数上限是 999，留足余量。
_IN_CHUNK = 500


async def get_evaluations(scene_ids: list[str]) -> dict[str, SceneEvaluation]:
    """批量读取评估（工单18 D2），返回 scene_id → 评估；没有评估的场景不出现在结果里。

    谱系回溯原先对每个祖先逐个 `get_evaluation`，50 场就是 50 次单行查询。
    """
    ids = list(dict.fromkeys(scene_ids))
    result: dict[str, SceneEvaluation] = {}
    if not ids:
        return result
    async with db.connect() as conn:
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start : start + _IN_CHUNK]
            placeholders = ", ".join("?" * len(chunk))
            cur = await conn.execute(
                f"SELECT scene_id, data_json FROM evaluations WHERE scene_id IN ({placeholders})",
                tuple(chunk),
            )
            for scene_id, data_json in await cur.fetchall():
                result[scene_id] = _deserialize_evaluation(json.loads(data_json), scene_id)
    return result


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
# Storyboard（工单18：分支级导演分镜稿）
# ---------------------------------------------------------------------------


def _safe_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _deserialize_beat(data: object) -> StoryBeat | None:
    if not isinstance(data, dict):
        return None
    return StoryBeat(
        beat_id=str(data.get("beat_id") or ""),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        status=str(data.get("status") or BeatStatus.PLANNED.value),
        resolved_scene_id=str(data.get("resolved_scene_id") or ""),
    )


def _deserialize_fork_origin(data: object) -> ForkOrigin | None:
    if not isinstance(data, dict):
        return None
    conditions = data.get("conditions")
    return ForkOrigin(
        source_branch_id=str(data.get("source_branch_id") or ""),
        source_branch_name=str(data.get("source_branch_name") or ""),
        source_snapshot_id=str(data.get("source_snapshot_id") or ""),
        source_snapshot_label=str(data.get("source_snapshot_label") or ""),
        conditions=dict(conditions) if isinstance(conditions, dict) else {},
        director_notes=str(data.get("director_notes") or ""),
    )


def deserialize_storyboard(data: object, project_id: str, branch_id: str) -> Storyboard:
    """从 JSON 还原分镜稿（§5.4 第 2 步），并当场压回预算（工单18 §3.2 读取侧闸门）。

    分支文件与快照副本共用。文件摆在项目目录里、可被人工编辑，任何字段都可能是
    错的类型：一律降级成默认值，坏节拍跳过 —— 分镜稿是导演的附加记忆，读不出来
    不该让整场推演起不来。**只压不写回**：读路径不改用户手编的文件。
    """
    if not isinstance(data, dict):
        data = {}
    outline_raw = data.get("outline")
    beats = [
        b for b in (_deserialize_beat(x) for x in (outline_raw if isinstance(outline_raw, list) else []))
        if b is not None
    ]
    changelog_raw = data.get("changelog")
    changelog = [
        StoryboardChange(
            source=str(c.get("source") or StoryboardSource.DIRECTOR.value),
            scene_id=str(c.get("scene_id") or ""),
            summary=str(c.get("summary") or ""),
            at=_parse_created_at(c.get("at"), "分镜稿改动时间"),
            request_id=str(c.get("request_id") or ""),
            request_digest=str(c.get("request_digest") or ""),
        )
        for c in (changelog_raw if isinstance(changelog_raw, list) else [])
        if isinstance(c, dict)
    ]
    board = Storyboard(
        project_id=str(data.get("project_id") or project_id),
        branch_id=str(data.get("branch_id") or branch_id),
        outline=beats,
        memo=str(data.get("memo") or ""),
        goal_revision=str(data.get("goal_revision") or ""),
        fork_origin=_deserialize_fork_origin(data.get("fork_origin")),
        changelog=changelog,
        revision=_safe_int(data.get("revision"), 0),
        next_beat_seq=_safe_int(data.get("next_beat_seq"), 1),
        updated_at=_parse_created_at(data.get("updated_at"), "分镜稿更新时间"),
    )
    issues = clamp_storyboard(board)
    if issues:
        logger.warning(
            "分支 %s 的分镜稿超出预算或形状不合法，本次读取已修正：%s",
            branch_id,
            "；".join(issues),
        )
    return board


def _storyboard_path(project_id: str, branch_id: str) -> Path:
    d = settings.project_dir(project_id) / "storyboard"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{branch_id}.json"


async def get_storyboard(project_id: str, branch_id: str) -> Storyboard:
    """读取分支的分镜稿。文件不存在 = 空分镜稿，不是错误（同 world-state）。"""
    path = _storyboard_path(project_id, branch_id)
    if not path.exists():
        return Storyboard(project_id=project_id, branch_id=branch_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("分支 %s 的分镜稿文件损坏，按空分镜稿处理", branch_id, exc_info=True)
        return Storyboard(project_id=project_id, branch_id=branch_id)
    return deserialize_storyboard(data, project_id, branch_id)


async def save_storyboard(board: Storyboard) -> None:
    """落盘分支分镜稿。branch_id 为空时拒绝写入（同 `save_world_state`）。"""
    if not board.branch_id:
        raise ValueError("保存分镜稿必须指定 branch_id")
    board.updated_at = now()
    _storyboard_path(board.project_id, board.branch_id).write_text(
        to_json(board), encoding="utf-8"
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
        source=data.get("source") or DecisionSource.HUMAN.value,
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
