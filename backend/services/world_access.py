"""Creator world operations and conservative explicit migration."""

from __future__ import annotations

import hashlib
import json

from backend.exceptions import BranchNotFoundError, ConflictError, InvalidRequestError
from backend.services import repository
from backend.services.world import migration_preview, parse_operations
from backend.snapshot import SnapshotManager
from backend.utils.serializer import to_dict


async def check_branch(project_id: str, branch_id: str):
    project = await repository.get_project(project_id)
    branches = await SnapshotManager(project_id).list_branches()
    if not any(branch.branch_id == branch_id for branch in branches):
        raise BranchNotFoundError("分支不属于此项目或不存在")
    return project


async def branch_world(project_id: str, branch_id: str):
    project = await check_branch(project_id, branch_id)
    if project.world_schema_version != 2:
        raise ConflictError("此项目尚未迁移到统一世界状态，请先预览迁移")
    revision = await repository.get_branch_world(project_id, branch_id)
    if revision is None:
        revision = await repository.initialize_branch_world(
            project_id, branch_id, definitions=await repository.list_objects(project_id)
        )
    return revision


async def command(project_id: str, branch_id: str, request):
    await branch_world(project_id, branch_id)
    characters = {c.character_id for c in await repository.list_characters(project_id)}
    if set(request.observers) - characters:
        raise InvalidRequestError("知情者必须是项目中的角色")
    operations = parse_operations(request.operations)
    for operation in operations:
        if isinstance(operation.value, dict) and operation.value["kind"] == "character":
            if operation.value["id"] not in characters:
                raise InvalidRequestError("属性引用了不存在的角色")
    return await repository.commit_world_event(
        project_id,
        branch_id,
        event_id=request.request_id,
        expected_revision_id=request.revision_id,
        operations=operations,
        observers=request.observers,
        source="user",
    )


async def adopt_definitions(project_id: str, branch_id: str, request):
    await branch_world(project_id, branch_id)
    return await repository.commit_world_event(
        project_id,
        branch_id,
        event_id=request.request_id,
        expected_revision_id=request.revision_id,
        operations=[],
        source="definitions",
        definitions=await repository.list_objects(project_id),
    )


async def preview_migration(project_id: str):
    project = await repository.get_project(project_id)
    definitions = await repository.list_objects(project_id)
    branches = await SnapshotManager(project_id).list_branches()
    records = {
        b.branch_id: await repository.get_legacy_world_records(project_id, b.branch_id)
        for b in branches
    }
    preview_revision = hashlib.sha256(
        json.dumps(
            {"records": records, "definitions": to_dict(definitions)},
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return (
        {
            "schema_version": project.world_schema_version,
            "preview_revision": preview_revision,
            "branches": [
                {
                    "branch_id": b.branch_id,
                    "name": b.name,
                    **migration_preview(records[b.branch_id], definitions),
                }
                for b in branches
            ],
        },
        records,
        definitions,
    )


async def migrate(project_id: str, request):
    # Migration and run initialization share the same project gate. Once running is persisted,
    # the SQLite transaction's status check prevents a schema switch for the full run.
    from backend.services import orchestrator

    async with orchestrator._branch_lock(project_id, "__world_schema__"):
        preview, records, definitions = await preview_migration(project_id)
        if preview["preview_revision"] != request.preview_revision:
            raise ConflictError("迁移预览后旧数据或物件定义已变化，请重新预览")
        if set(request.branches) - set(records):
            raise InvalidRequestError("迁移选择包含其他项目的分支")
        seeds = {
            bid: (raw, parse_operations(request.branches.get(bid, [])))
            for bid, raw in records.items()
        }
        characters = {c.character_id for c in await repository.list_characters(project_id)}
        for _, operations in seeds.values():
            for operation in operations:
                if isinstance(operation.value, dict) and operation.value["kind"] == "character":
                    if operation.value["id"] not in characters:
                        raise InvalidRequestError("迁移属性引用了不存在的角色")
        return await repository.migrate_project_worlds(
            project_id, seeds, definitions, request_id=request.request_id
        )
