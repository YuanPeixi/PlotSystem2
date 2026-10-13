"""Unified world persistence. Exposed exclusively through services.repository.

A revision, event, branch head and scene result commit in one SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from backend.config import settings
from backend.exceptions import ConflictError, InvalidRequestError
from backend.models import Scene, WorldEvent, WorldObject, WorldOperation, WorldRevision, new_id
from backend.services.world import (
    apply_operations,
    capture_current_observations,
    deserialize_revision,
)
from backend.utils import db
from backend.utils.serializer import to_dict, to_json


def _digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


async def _head(conn, project_id: str, branch_id: str):
    cursor = await conn.execute(
        "SELECT revision_id, running_scene_id, execution_token FROM world_heads "
        "WHERE project_id=? AND branch_id=?",
        (project_id, branch_id),
    )
    return await cursor.fetchone()


async def _revision(conn, project_id: str, revision_id: str) -> WorldRevision:
    cursor = await conn.execute(
        "SELECT data_json FROM world_revisions WHERE project_id=? AND revision_id=?",
        (project_id, revision_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise InvalidRequestError("世界版本不存在，不能退回读取当前世界")
    return deserialize_revision(json.loads(row[0]))


async def _store_revision(conn, revision: WorldRevision) -> None:
    await conn.execute(
        "INSERT INTO world_revisions (revision_id, project_id, data_json) VALUES (?, ?, ?)",
        (revision.revision_id, revision.project_id, to_json(revision)),
    )


async def get_world_revision(project_id: str, revision_id: str) -> WorldRevision:
    async with db.connect() as conn:
        return await _revision(conn, project_id, revision_id)


async def get_branch_world(project_id: str, branch_id: str) -> WorldRevision | None:
    async with db.connect() as conn:
        head = await _head(conn, project_id, branch_id)
        return await _revision(conn, project_id, head[0]) if head else None


async def initialize_branch_world(
    project_id: str,
    branch_id: str,
    *,
    definitions: list[WorldObject] = (),
    from_revision_id: str = "",
    legacy_records: dict | None = None,
) -> WorldRevision:
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        head = await _head(conn, project_id, branch_id)
        if head:
            return await _revision(conn, project_id, head[0])
        if from_revision_id:
            revision = await _revision(conn, project_id, from_revision_id)
        else:
            revision = WorldRevision(
                project_id=project_id,
                definitions={obj.object_id: deepcopy(obj) for obj in definitions},
                legacy_records=dict(legacy_records or {}),
            )
            await _store_revision(conn, revision)
        await conn.execute(
            "INSERT INTO world_heads (project_id, branch_id, revision_id) VALUES (?, ?, ?)",
            (project_id, branch_id, revision.revision_id),
        )
        await conn.commit()
        return revision


async def acquire_world_execution(project_id: str, branch_id: str, scene_id: str) -> str:
    token = new_id()
    async with db.connect() as conn:
        cursor = await conn.execute(
            "UPDATE world_heads SET running_scene_id=?, execution_token=? "
            "WHERE project_id=? AND branch_id=? AND running_scene_id=''",
            (scene_id, token, project_id, branch_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("同一分支已有场景在推进世界，请等待结束或从快照分叉")
        await conn.commit()
    return token


async def release_world_execution(project_id: str, branch_id: str, token: str) -> None:
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE world_heads SET running_scene_id='', execution_token='' "
            "WHERE project_id=? AND branch_id=? AND execution_token=?",
            (project_id, branch_id, token),
        )
        await conn.commit()


async def reconcile_world_executions() -> None:
    """Startup only, following this application's single-process contract."""
    async with db.connect() as conn:
        await conn.execute("UPDATE world_heads SET running_scene_id='', execution_token=''")
        await conn.commit()


async def commit_world_event(
    project_id: str,
    branch_id: str,
    *,
    event_id: str,
    expected_revision_id: str,
    operations: list[WorldOperation],
    source: str = "user",
    source_id: str = "",
    observers: list[str] = (),
    narration: str = "",
    scene: Scene | None = None,
    execution_token: str = "",
    allowed_objects: set[str] | None = None,
    definitions: list[WorldObject] | None = None,
) -> WorldRevision:
    if not event_id or not expected_revision_id:
        raise InvalidRequestError("世界提交必须带事件幂等键与读取时的版本")
    if source not in {"environment", "director", "user", "definitions", "migration", "observation"}:
        raise InvalidRequestError("世界提交来源不合法")
    observers = sorted(set(observers))
    payload = {
        "operations": [to_dict(o) for o in operations],
        "source": source,
        "source_id": source_id,
        "observers": observers,
        "narration": narration,
        "definitions": [to_dict(d) for d in definitions] if definitions is not None else None,
        "allowed_objects": sorted(allowed_objects or set()),
    }
    digest = _digest(payload)
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "SELECT request_digest, data_json FROM world_events "
            "WHERE project_id=? AND branch_id=? AND event_id=?",
            (project_id, branch_id, event_id),
        )
        replay = await cursor.fetchone()
        if replay:
            if replay[0] != digest:
                raise ConflictError("同一事件幂等键不能用于不同的世界变化")
            event = json.loads(replay[1])
            return await _revision(conn, project_id, event["revision_id"])
        head = await _head(conn, project_id, branch_id)
        if head is None or head[0] != expected_revision_id:
            raise ConflictError("世界状态已经改变，请重新读取后提交")
        if head[1]:
            if (
                not execution_token
                or execution_token != head[2]
                or scene is None
                or scene.scene_id != head[1]
            ):
                raise ConflictError("分支场景正在运行，不能在外部改写世界")
        elif execution_token:
            raise ConflictError("场景执行标识已失效，拒绝迟到的世界写入")
        current = await _revision(conn, project_id, head[0])
        character_dir = settings.project_dir(project_id) / "characters"
        for observer in observers:
            if (
                not isinstance(observer, str)
                or any(c in observer for c in "/\\")
                or not (character_dir / f"{observer}.json").is_file()
            ):
                raise InvalidRequestError("世界观察者不是项目中的角色")
        for operation in operations:
            if isinstance(operation.value, dict) and operation.value.get("kind") == "character":
                cid = operation.value.get("id", "")
                if (
                    not isinstance(cid, str)
                    or any(c in cid for c in "/\\")
                    or not (character_dir / f"{cid}.json").is_file()
                ):
                    raise InvalidRequestError("世界属性引用了不存在的角色")
        if definitions is not None:
            current.definitions = {obj.object_id: deepcopy(obj) for obj in definitions}
        revision = apply_operations(
            current,
            operations,
            event_id,
            observers,
            source=source,
            allowed_objects=allowed_objects,
            source_branch_id=branch_id,
        )
        if scene is not None:
            capture_current_observations(
                revision, scene.participating_characters, scene.objects_present, scene.location
            )
        event = WorldEvent(
            event_id=event_id,
            project_id=project_id,
            branch_id=branch_id,
            base_revision_id=head[0],
            revision_id=revision.revision_id,
            source=source,
            source_id=source_id,
            scene_id=scene.scene_id if scene else "",
            operations=deepcopy(operations),
            observers=observers,
            narration=narration,
        )
        await _store_revision(conn, revision)
        await conn.execute(
            "INSERT INTO world_events VALUES (?, ?, ?, ?, ?)",
            (project_id, branch_id, event_id, digest, to_json(event)),
        )
        await conn.execute(
            "UPDATE world_heads SET revision_id=? WHERE project_id=? AND branch_id=?",
            (revision.revision_id, project_id, branch_id),
        )
        if scene is not None:
            # Serialize a copy; failed transactions must not mutate caller state.
            persisted = deepcopy(scene)
            persisted.world_revision_id = revision.revision_id
            for turn in persisted.dialogue_log:
                if turn.world_event_id == event_id:
                    turn.world_revision_id = revision.revision_id
            cursor = await conn.execute(
                "UPDATE scenes SET data_json=?, status=? WHERE scene_id=? AND project_id=? AND branch_id=?",
                (to_json(persisted), persisted.status, scene.scene_id, project_id, branch_id),
            )
            if cursor.rowcount != 1:
                raise InvalidRequestError("世界提交关联的场景不存在或不属于此分支")
        await conn.commit()
        if scene is not None:
            scene.world_revision_id = revision.revision_id
            for turn in scene.dialogue_log:
                if turn.world_event_id == event_id:
                    turn.world_revision_id = revision.revision_id
        return revision


async def list_world_events(project_id: str, branch_id: str, limit: int = 100) -> list[dict]:
    """Include inherited events up to the fork version, never later events on the source branch."""
    async with db.connect() as conn:
        cursor = await conn.execute(
            "WITH RECURSIVE lineage(revision_id) AS ("
            " SELECT revision_id FROM world_heads WHERE project_id=? AND branch_id=?"
            " UNION ALL"
            " SELECT json_extract(r.data_json, '$.parent_revision_id') FROM world_revisions r"
            " JOIN lineage l ON r.revision_id=l.revision_id WHERE r.project_id=?"
            " AND json_extract(r.data_json, '$.parent_revision_id') != ''"
            ") SELECT e.data_json FROM world_events e WHERE e.project_id=?"
            " AND json_extract(e.data_json, '$.revision_id') IN (SELECT revision_id FROM lineage)"
            " ORDER BY e.rowid DESC LIMIT ?",
            (project_id, branch_id, project_id, project_id, max(1, min(limit, 500))),
        )
        return [json.loads(row[0]) for row in await cursor.fetchall()]


async def migrate_project_worlds(
    project_id: str,
    seeds: dict[str, tuple[dict, list[WorldOperation]]],
    definitions: list[WorldObject],
    *,
    request_id: str,
) -> dict[str, str]:
    """Explicit migration: switch all branches and project schema in one transaction."""
    if not request_id:
        raise InvalidRequestError("迁移必须提供幂等键")
    digest = _digest(
        {
            "seeds": {
                bid: {"legacy": records, "operations": [to_dict(o) for o in ops]}
                for bid, (records, ops) in seeds.items()
            },
            "definitions": [to_dict(o) for o in definitions],
        }
    )
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "SELECT data_json FROM projects WHERE project_id=?", (project_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise InvalidRequestError("迁移项目不存在")
        project = json.loads(row[0])
        cursor = await conn.execute(
            "SELECT request_digest, data_json FROM world_events "
            "WHERE project_id=? AND branch_id='' AND event_id=?",
            (project_id, request_id),
        )
        replay = await cursor.fetchone()
        if replay:
            if replay[0] != digest:
                raise ConflictError("迁移幂等键已用于另一份迁移")
            return json.loads(replay[1])["branches"]
        if project.get("world_schema_version", 1) == 2:
            raise ConflictError("项目已经使用统一世界状态")
        cursor = await conn.execute(
            "SELECT 1 FROM scenes WHERE project_id=? AND status IN ('running','deciding') LIMIT 1",
            (project_id,),
        )
        if await cursor.fetchone():
            raise ConflictError("运行或决策中的项目不能迁移")
        cursor = await conn.execute(
            "SELECT branch_id FROM branches WHERE project_id=?", (project_id,)
        )
        if {row[0] for row in await cursor.fetchall()} != set(seeds):
            raise ConflictError("迁移期间分支已变化，请重新预览")
        heads = {}
        for bid, (records, operations) in seeds.items():
            root = WorldRevision(
                project_id=project_id,
                definitions={o.object_id: deepcopy(o) for o in definitions},
                legacy_records=dict(records),
            )
            revision = apply_operations(
                root, operations, request_id, [], source="migration", source_branch_id=bid
            )
            # Root is persisted so lineage always resolves.
            await _store_revision(conn, root)
            await _store_revision(conn, revision)
            await conn.execute(
                "INSERT INTO world_heads (project_id, branch_id, revision_id) VALUES (?, ?, ?)",
                (project_id, bid, revision.revision_id),
            )
            event = WorldEvent(
                event_id=request_id,
                project_id=project_id,
                branch_id=bid,
                base_revision_id=root.revision_id,
                revision_id=revision.revision_id,
                source="migration",
                source_id=project_id,
                operations=deepcopy(operations),
            )
            await conn.execute(
                "INSERT INTO world_events VALUES (?, ?, ?, ?, ?)",
                (project_id, bid, request_id, digest, to_json(event)),
            )
            heads[bid] = revision.revision_id
        project["world_schema_version"] = 2
        await conn.execute(
            "UPDATE projects SET data_json=? WHERE project_id=?",
            (json.dumps(project, ensure_ascii=False), project_id),
        )
        await conn.execute(
            "INSERT INTO world_events VALUES (?, '', ?, ?, ?)",
            (project_id, request_id, digest, json.dumps({"branches": heads}, ensure_ascii=False)),
        )
        await conn.commit()
        return heads
