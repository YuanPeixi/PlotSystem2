"""HTTP admission and explicit schema switching for unified worlds."""

import httpx
import pytest

from backend.main import app
from backend.services import repository
from backend.snapshot import SnapshotManager


@pytest.mark.asyncio
async def test_new_project_world_commands_typed_roundtrip_and_branch_scope():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/api/v1/projects", json={"name": "Unified API"})
        assert created.status_code == 200
        project = created.json()["data"]
        assert project["world_schema_version"] == 2
        pid = project["project_id"]
        branch = await SnapshotManager(pid).ensure_main_branch()
        url = f"/api/v1/projects/{pid}/branches/{branch.branch_id}"
        read = await client.get(url + "/world-state")
        revision = read.json()["data"]
        assert revision["schema_version"] == 2
        body = {
            "request_id": "http-command",
            "revision_id": revision["revision_id"],
            "operations": [{"attribute": "restricted", "value": False, "visibility": "global"}],
        }
        saved = await client.post(url + "/world/commands", json=body)
        assert saved.status_code == 200
        assert next(iter(saved.json()["data"]["facts"].values()))["value"] is False
        replay = await client.post(url + "/world/commands", json=body)
        assert replay.json()["data"]["revision_id"] == saved.json()["data"]["revision_id"]
        conflict = await client.post(url + "/world/commands", json={**body, "request_id": "new"})
        assert conflict.status_code == 409
        malformed = await client.post(
            url + "/world/commands",
            json={
                **body,
                "request_id": "malformed",
                "revision_id": saved.json()["data"]["revision_id"],
                "operations": [{"attribute": "a", "subject_kind": []}],
            },
        )
        assert malformed.status_code == 422
        foreign = await client.get(f"/api/v1/projects/{pid}/branches/foreign/world-state")
        assert foreign.status_code == 404
        assert len((await client.get(url + "/world/events")).json()["data"]) == 1


@pytest.mark.asyncio
async def test_legacy_api_preview_does_not_switch_or_broadcast_until_explicit_migration():
    from backend.models import Project, WorldState

    pid = "legacy-world-api"
    await repository.save_project(Project(project_id=pid, name="Legacy"))
    branch = await SnapshotManager(pid).ensure_main_branch()
    await repository.save_world_state(
        WorldState(project_id=pid, branch_id=branch.branch_id, variables={"某人已知秘密": "True"})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        base = f"/api/v1/projects/{pid}"
        preview = (await client.get(base + "/world/migration-preview")).json()["data"]
        assert (await repository.get_project(pid)).world_schema_version == 1
        migrated = await client.post(
            base + "/world/migrate",
            json={
                "request_id": "api-migrate",
                "preview_revision": preview["preview_revision"],
                "branches": {branch.branch_id: []},
            },
        )
        assert migrated.status_code == 200
        state = (await client.get(base + f"/branches/{branch.branch_id}/world-state")).json()[
            "data"
        ]
        assert state["facts"] == {} and state["legacy_records"] == {"某人已知秘密": "True"}
        assert (await repository.get_project(pid)).world_schema_version == 2
