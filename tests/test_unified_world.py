"""Unified world acceptance: time, perception, commits, forks and migration."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.character_agent import CharacterAgent
from backend.agents.environment_agent import EnvironmentAgent, parse_world_adjudication
from backend.config import settings
from backend.exceptions import ConflictError, InvalidRequestError, LLMError
from backend.memory import MemoryManager
from backend.models import (
    ActionIntent,
    CharacterCard,
    DialogueTurn,
    Project,
    Scene,
    SceneConfig,
    WorldObject,
    WorldOperation,
    WorldRevision,
)
from backend.scene_engine import SceneEngine
from backend.services import events, orchestrator, repository, world_access
from backend.services.world import (
    apply_operations,
    fact_key,
    object_state,
    parse_operations,
    render_world_for_character,
)
from backend.snapshot import SnapshotManager
from backend.utils.serializer import to_dict

CROWN = WorldObject(
    object_id="crown", name="玻璃王冠", visibility="global", hidden_rules=["佩戴触发投影"]
)
NICHE = WorldObject(object_id="niche", name="暗格", visibility="private", known_by=["a"])


def op(attribute="open", value=True, oid="niche", **kwargs):
    return WorldOperation(
        subject_kind="object",
        subject_id=oid,
        attribute=attribute,
        value=value,
        visibility="observable",
        **kwargs,
    )


def root(pid="world-test"):
    return WorldRevision(
        project_id=pid, definitions={o.object_id: deepcopy(o) for o in [CROWN, NICHE]}
    )


def test_private_current_state_and_historical_observation_are_separate():
    first = apply_operations(
        root(), [op()], "e1", ["a", "b"], source="environment", allowed_objects={"niche"}
    )
    second = apply_operations(
        first, [op(value=False)], "e2", ["a"], source="environment", allowed_objects={"niche"}
    )
    # B learned about the niche once. Away from it, knowledge is the earlier observation.
    away = render_world_for_character(second, "b", [])
    assert "此前获知" in away and "是" in away and "否" not in away
    # Seeing the discovered object again provides current state.
    present = render_world_for_character(second, "b", ["niche"])
    assert "否" in present and "此前获知" not in present
    assert render_world_for_character(second, "c", ["niche"]) == ""
    assert object_state(second, "niche") == {"open": False}


def test_unset_is_an_observation_and_never_the_same_as_false_or_unknown():
    first = apply_operations(root(), [op(value=False)], "e1", ["a"])
    second = apply_operations(first, [op(op="unset")], "e2", ["a"])
    assert object_state(first, "niche")["open"] is False
    assert object_state(second, "niche") == {}
    assert second.observations["a"][fact_key("object", "niche", "open")].deleted
    assert "已被清除" in render_world_for_character(second, "a", [])


def test_global_deletion_invalidates_old_observation_without_leaking_hidden_facts():
    first = apply_operations(
        root(), [WorldOperation(attribute="season", value="冬", visibility="global")], "e1", ["a"]
    )
    second = apply_operations(
        first, [WorldOperation(op="unset", attribute="season", visibility="global")], "e2", []
    )
    assert "已被清除" in render_world_for_character(second, "a", [])
    hidden = apply_operations(
        second, [WorldOperation(attribute="secret", value="secret", visibility="hidden")], "e3", []
    )
    assert "secret" not in render_world_for_character(hidden, "a", [])


def test_display_budget_and_rename_never_change_authoritative_facts():
    revision = root()
    for i in range(50):
        revision = apply_operations(
            revision, [op(attribute=f"a{i}", value="长内容" * 20)], f"e{i}", ["a"]
        )
    before = to_dict(revision)
    assert "预算" in render_world_for_character(revision, "a", ["niche"], budget_tokens=40)
    assert to_dict(revision) == before and len(revision.facts) == 50
    revision.definitions["niche"].name = "石龛"
    assert "石龛" in render_world_for_character(revision, "a", ["niche"], budget_tokens=10000)
    assert len(object_state(revision, "niche")) == 50


@pytest.mark.parametrize(
    "raw",
    [
        [{"attribute": "a", "value": [1]}],
        [{"attribute": "a", "value": {"kind": "character", "id": ""}}],
        [{"attribute": "a", "visibility": "everyone"}],
        [{"attribute": "a", "value": float("inf")}],
        [{"attribute": "a\nb"}],
        [{"attribute": "a"}, {"attribute": "a"}],
    ],
)
def test_invalid_commands_are_rejected_as_a_whole(raw):
    with pytest.raises(InvalidRequestError):
        parse_operations(raw)


def test_environment_scope_and_director_authority_are_enforced():
    with pytest.raises(InvalidRequestError):
        apply_operations(root(), [op()], "e", [], source="environment", allowed_objects={"crown"})
    with pytest.raises(InvalidRequestError):
        apply_operations(root(), [op()], "e", [], source="director")


def test_unified_adjudication_rejects_partial_or_contradictory_results():
    valid = {
        "executable": True,
        "triggered": False,
        "narration": "暗格开了。",
        "operations": [to_dict(op())],
    }
    assert parse_world_adjudication(json.dumps(valid), {"niche"}) is not None
    assert parse_world_adjudication(json.dumps(valid), {"crown"}) is None
    valid["operations"].append({"attribute": "bad", "value": []})
    assert parse_world_adjudication(json.dumps(valid), {"niche"}) is None
    valid["operations"] = []
    valid["executable"] = False
    valid["triggered"] = True
    assert parse_world_adjudication(json.dumps(valid), {"niche"}) is None


async def stored_world(pid):
    await repository.save_project(Project(project_id=pid, name=pid, world_schema_version=2))
    sm = SnapshotManager(pid)
    branch = await sm.ensure_main_branch()
    for oid in ("a", "b", "c"):
        await repository.save_character(CharacterCard(character_id=oid, project_id=pid, name=oid))
    definitions = [WorldObject(**{**to_dict(o), "project_id": pid}) for o in [CROWN, NICHE]]
    revision = await repository.initialize_branch_world(
        pid, branch.branch_id, definitions=definitions
    )
    return branch, revision, sm


@pytest.mark.asyncio
async def test_atomic_commit_replay_digest_and_stale_version():
    pid = "u-atomic"
    branch, revision, _ = await stored_world(pid)
    source = DialogueTurn(
        turn_id="t-atomic", actions=[ActionIntent(object_id="niche", status="resolved")]
    )
    reaction = DialogueTurn(
        turn_id="e-atomic", kind="environment", narration="打开", world_event_id="e-atomic"
    )
    scene = Scene(
        scene_id="s-atomic",
        project_id=pid,
        branch_id=branch.branch_id,
        dialogue_log=[source, reaction],
    )
    await repository.save_scene(scene)
    kwargs = dict(
        event_id="e-atomic",
        expected_revision_id=revision.revision_id,
        operations=[op()],
        source="environment",
        observers=["a"],
        allowed_objects={"niche"},
        scene=scene,
    )
    committed = await repository.commit_world_event(pid, branch.branch_id, **kwargs)
    persisted = await repository.get_scene(scene.scene_id)
    assert persisted.world_revision_id == committed.revision_id
    assert persisted.dialogue_log[1].world_revision_id == committed.revision_id
    assert object_state(await repository.get_branch_world(pid, branch.branch_id), "niche") == {
        "open": True
    }
    replay = await repository.commit_world_event(pid, branch.branch_id, **kwargs)
    assert replay.revision_id == committed.revision_id
    assert len(await repository.list_world_events(pid, branch.branch_id)) == 1
    with pytest.raises(ConflictError):
        await repository.commit_world_event(
            pid, branch.branch_id, **{**kwargs, "operations": [op(value=False)]}
        )
    with pytest.raises(ConflictError):
        await repository.commit_world_event(
            pid, branch.branch_id, **{**kwargs, "event_id": "later"}
        )


@pytest.mark.asyncio
async def test_transaction_failure_does_not_leave_head_event_or_facts():
    pid = "u-rollback"
    branch, revision, _ = await stored_world(pid)
    missing = Scene(scene_id="missing", project_id=pid, branch_id=branch.branch_id)
    with pytest.raises(InvalidRequestError):
        await repository.commit_world_event(
            pid,
            branch.branch_id,
            event_id="bad",
            expected_revision_id=revision.revision_id,
            operations=[op()],
            scene=missing,
        )
    assert (
        await repository.get_branch_world(pid, branch.branch_id)
    ).revision_id == revision.revision_id
    assert await repository.list_world_events(pid, branch.branch_id) == []


@pytest.mark.asyncio
async def test_branch_execution_blocks_other_scene_external_write_and_late_token():
    pid = "u-execution"
    branch, revision, _ = await stored_world(pid)
    token = await repository.acquire_world_execution(pid, branch.branch_id, "running")
    with pytest.raises(ConflictError):
        await repository.acquire_world_execution(pid, branch.branch_id, "other")
    with pytest.raises(ConflictError):
        await repository.commit_world_event(
            pid,
            branch.branch_id,
            event_id="external",
            expected_revision_id=revision.revision_id,
            operations=[op()],
        )
    await repository.release_world_execution(pid, branch.branch_id, "wrong")
    with pytest.raises(ConflictError):
        await repository.acquire_world_execution(pid, branch.branch_id, "other")
    await repository.reconcile_world_executions()
    with pytest.raises(ConflictError):
        await repository.commit_world_event(
            pid,
            branch.branch_id,
            event_id="late",
            expected_revision_id=revision.revision_id,
            operations=[op()],
            execution_token=token,
        )
    new = await repository.acquire_world_execution(pid, branch.branch_id, "other")
    await repository.release_world_execution(pid, branch.branch_id, new)


@pytest.mark.asyncio
async def test_snapshot_fork_inherits_state_observations_and_old_rule_definitions():
    pid = "u-fork"
    branch, revision, sm = await stored_world(pid)
    first = await repository.commit_world_event(
        pid,
        branch.branch_id,
        event_id="open",
        expected_revision_id=revision.revision_id,
        operations=[op()],
        observers=["a"],
    )
    scene = Scene(
        scene_id="s-u-fork", project_id=pid, branch_id=branch.branch_id, objects_present=["niche"]
    )
    await repository.save_scene(scene)
    snapshot = await sm.create_snapshot(
        scene_id=scene.scene_id,
        branch_id=branch.branch_id,
        character_states={},
        world_revision_id=first.revision_id,
    )
    definitions = list(first.definitions.values())
    definitions[0].hidden_rules = ["new-rule"]
    await repository.commit_world_event(
        pid,
        branch.branch_id,
        event_id="edit",
        expected_revision_id=first.revision_id,
        operations=[op(value=False)],
        definitions=definitions,
    )
    fork, _ = await orchestrator.fork_from_snapshot(pid, snapshot.snapshot_id, "IF")
    inherited = await repository.get_branch_world(pid, fork.branch_id)
    assert inherited.revision_id == first.revision_id
    assert object_state(inherited, "niche")["open"] is True
    assert (
        inherited.observations["a"]
        and inherited.definitions["crown"].hidden_rules == CROWN.hidden_rules
    )
    await repository.commit_world_event(
        pid,
        fork.branch_id,
        event_id="close",
        expected_revision_id=inherited.revision_id,
        operations=[op(value=False)],
    )
    assert (
        await repository.get_world_revision(pid, snapshot.world_revision_id)
    ).facts == first.facts
    assert inherited.facts[fact_key("object", "niche", "open")].source_branch_id == branch.branch_id
    inherited_events = await repository.list_world_events(pid, fork.branch_id)
    assert {e["event_id"] for e in inherited_events} == {"open", "close"}
    assert "edit" not in {e["event_id"] for e in inherited_events}


@pytest.mark.asyncio
async def test_migration_preview_quarantines_unknown_records_and_replays_switch():
    pid = "u-migrate"
    await repository.save_project(Project(project_id=pid, name=pid))
    branch = await SnapshotManager(pid).ensure_main_branch()
    from backend.models import WorldState

    await repository.save_world_state(
        WorldState(
            project_id=pid,
            branch_id=branch.branch_id,
            variables={"季节": "冬", "王后已知暗格存在": "True", "有人公开提及夹层": "True"},
        )
    )
    preview, _, _ = await world_access.preview_migration(pid)
    assert {e["category"] for e in preview["branches"][0]["entries"]} >= {
        "knowledge_candidate",
        "disclosure_candidate",
    }
    from backend.api.schemas import WorldMigrationRequest

    request = WorldMigrationRequest(
        request_id="migrate",
        preview_revision=preview["preview_revision"],
        branches={
            branch.branch_id: [
                {"attribute": "season", "label": "季节", "value": "冬", "visibility": "global"}
            ]
        },
    )
    heads = await world_access.migrate(pid, request)
    assert await world_access.migrate(pid, request) == heads
    world = await repository.get_branch_world(pid, branch.branch_id)
    assert len(world.legacy_records) == 3 and len(world.facts) == 1
    assert "已知" not in render_world_for_character(world, "nobody", [])
    assert (await repository.get_project(pid)).world_schema_version == 2


@pytest.mark.asyncio
async def test_real_orchestrator_commits_before_sse_and_survives_failed_evaluation(monkeypatch):
    pid = "u-orchestrator"
    branch, revision, sm = await stored_world(pid)
    scene = Scene(
        scene_id="s-u-orch",
        project_id=pid,
        branch_id=branch.branch_id,
        participating_characters=["a"],
        objects_present=["crown"],
        max_turns=1,
    )
    await repository.save_scene(scene)
    monkeypatch.setattr(settings, "ENVIRONMENT_MODE", "adjudicate")
    calls = []

    async def llm(messages, *, purpose, **kwargs):
        calls.append((purpose.value, messages))
        if purpose.value == "character":
            return "*戴上玻璃王冠*"
        if purpose.value == "action_extract":
            return '[{"index":0,"object":"o1","is_attempt":true,"verb":"戴上","detail":""}]'
        if purpose.value == "adjudicate":
            return json.dumps(
                {
                    "executable": True,
                    "triggered": True,
                    "narration": "王冠亮起。",
                    "operations": [to_dict(op(attribute="light", value=True, oid="crown"))],
                }
            )
        raise AssertionError(purpose)

    async def publish(sid, kind, data):
        if kind == "turn" and data.get("kind") == "environment":
            persisted = await repository.get_scene(sid)
            head = await repository.get_branch_world(pid, branch.branch_id)
            assert persisted.world_revision_id == head.revision_id == data["world_revision_id"]
            assert persisted.dialogue_log[-1].world_event_id == data["world_event_id"]

    monkeypatch.setattr(events, "publish", publish)
    monkeypatch.setattr(CharacterAgent, "retrieve_relevant_memory", AsyncMock(return_value=[]))

    class FailingDirector:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate_scene(self, *args, **kwargs):
            raise LLMError("evaluation unavailable")

    monkeypatch.setattr(orchestrator, "DirectorAgent", FailingDirector)
    sites = (
        "backend.agents.character_agent.chat_safe",
        "backend.scene_engine.action_intents.chat_safe",
        "backend.agents.environment_agent.chat_safe",
    )
    from contextlib import ExitStack

    with ExitStack() as stack:
        for site in sites:
            stack.enter_context(patch(site, llm))
        await orchestrator.run_scene(scene.scene_id)
    persisted = await repository.get_scene(scene.scene_id)
    assert persisted.status == "completed" and len(persisted.dialogue_log) == 2
    assert persisted.environment_state == {} and object_state(
        await repository.get_branch_world(pid, branch.branch_id), "crown"
    ) == {"light": True}
    snapshot = await sm.get_snapshot(persisted.snapshot_id_after)
    assert snapshot.world_revision_id == persisted.world_revision_id
    assert (
        await sm.get_snapshot(persisted.snapshot_id_before)
    ).world_revision_id == revision.revision_id
    # Next scene reads the committed state, with no evaluated delta required.
    world = await repository.get_branch_world(pid, branch.branch_id)
    agent = CharacterAgent(
        CharacterCard(character_id="a", project_id=pid, name="a"), MemoryManager("a", pid)
    )
    engine = SceneEngine(
        Scene(project_id=pid, branch_id=branch.branch_id),
        SceneConfig(),
        [agent],
        sm,
        world_revision=world,
        objects=list(world.definitions.values()),
    )
    extras = engine._respond_extras(agent)
    assert "light" in extras["environment"]
    assert not any("light" in str(value) for value in engine._scene_context().values())
    adjudicate_prompt = next(m[0]["content"] for purpose, m in calls if purpose == "adjudicate")
    assert "known_facts" not in adjudicate_prompt


@pytest.mark.asyncio
async def test_reveal_generation_never_receives_other_secrets_or_script_content(monkeypatch):
    from backend.models import RevealEntry

    seen = []

    async def llm(messages, **kwargs):
        seen.append(messages[0]["content"])
        if len(seen) == 1:
            return '{"executable":true,"triggered":true,"narration":"光芒一闪。","operations":[],"reveal_effect":"actor_memory"}'
        return "当事人记得的童年。"

    monkeypatch.setattr("backend.agents.environment_agent.chat_safe", llm)
    agent = EnvironmentAgent()
    result = await agent.adjudicate_world(
        CROWN,
        "a",
        "A",
        ActionIntent(text="佩戴"),
        root(),
        reveals=[RevealEntry(condition="其他条件", content="script-secret")],
        actor_material="actor-known",
    )
    assert result.private_detail == "当事人记得的童年。"
    assert "script-secret" not in seen[0] and "actor-known" not in seen[0]
    assert (
        "actor-known" in seen[1]
        and "佩戴触发投影" not in seen[1]
        and "script-secret" not in seen[1]
    )


@pytest.mark.asyncio
async def test_a_new_direct_observation_is_saved_for_later_scenes():
    from backend.services.world import capture_current_observations

    pid = "u-perception"
    branch, revision, _ = await stored_world(pid)
    first = await repository.commit_world_event(
        pid,
        branch.branch_id,
        event_id="first",
        expected_revision_id=revision.revision_id,
        operations=[op()],
        observers=["a"],
    )
    second = await repository.commit_world_event(
        pid,
        branch.branch_id,
        event_id="second",
        expected_revision_id=first.revision_id,
        operations=[op(value=False)],
    )
    staged = deepcopy(second)
    assert capture_current_observations(staged, ["a"], ["niche"])
    scene = Scene(
        scene_id="s-u-perception",
        project_id=pid,
        branch_id=branch.branch_id,
        participating_characters=["a"],
        objects_present=["niche"],
    )
    await repository.save_scene(scene)
    token = await repository.acquire_world_execution(pid, branch.branch_id, scene.scene_id)
    observed = await repository.commit_world_event(
        pid,
        branch.branch_id,
        event_id="observe",
        expected_revision_id=second.revision_id,
        operations=[],
        source="observation",
        scene=scene,
        execution_token=token,
    )
    await repository.release_world_execution(pid, branch.branch_id, token)
    assert observed.observations["a"][fact_key("object", "niche", "open")].fact.value is False
    assert "否" in render_world_for_character(observed, "a", [])
    assert not capture_current_observations(deepcopy(observed), ["a"], ["niche"])


@pytest.mark.asyncio
async def test_private_generation_failure_preserves_public_adjudication(monkeypatch):
    calls = 0

    async def llm(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"executable":true,"triggered":true,"narration":"光芒一闪。","operations":[],"reveal_effect":"actor_memory"}'
        raise LLMError("private generation unavailable")

    monkeypatch.setattr("backend.agents.environment_agent.chat_safe", llm)
    result = await EnvironmentAgent().adjudicate_world(
        CROWN, "a", "A", ActionIntent(text="戴上"), root(), actor_material="known"
    )
    assert result is not None and result.narration == "光芒一闪。" and result.private_detail == ""


@pytest.mark.asyncio
async def test_failed_environment_commit_keeps_action_pending_and_no_reaction_in_memory(
    monkeypatch,
):
    pid = "u-commit-failure"
    branch, revision, sm = await stored_world(pid)
    source = DialogueTurn(
        turn_id="t-failure",
        character_id="a",
        character_name="A",
        actions=[ActionIntent(object_id="crown", text="佩戴王冠", status="pending")],
    )
    scene = Scene(
        scene_id="s-u-failure",
        project_id=pid,
        branch_id=branch.branch_id,
        participating_characters=["a"],
        objects_present=["crown"],
        dialogue_log=[source],
    )
    from backend.models import WorldAdjudication

    adjudicator = EnvironmentAgent()
    monkeypatch.setattr(
        adjudicator,
        "adjudicate_world",
        AsyncMock(return_value=WorldAdjudication(narration="亮起", operations=[op(oid="crown")])),
    )
    agent = CharacterAgent(
        CharacterCard(character_id="a", project_id=pid, name="A"), MemoryManager("a", pid)
    )
    monkeypatch.setattr(agent, "retrieve_relevant_memory", AsyncMock(return_value=[]))

    async def failing_commit(*args):
        raise OSError("disk failure")

    engine = SceneEngine(
        scene,
        SceneConfig(),
        [agent],
        sm,
        objects=[CROWN],
        environment_mode="adjudicate",
        adjudicator=adjudicator,
        world_revision=revision,
        world_commit=failing_commit,
    )
    turns = [source]
    with pytest.raises(OSError):
        await engine._adjudicate_pending(source, turns, [], None, None)
    assert len(turns) == 1 and source.actions[0].status == "pending" and source.revision == 0
    assert scene.world_revision_id == revision.revision_id
    assert agent.memory.short_term.recent() == []


@pytest.mark.asyncio
async def test_director_proposals_apply_only_at_next_scene_not_to_completed_snapshot(monkeypatch):
    pid = "u-director-transition"
    branch, revision, sm = await stored_world(pid)
    scene = Scene(
        scene_id="s-u-transition", project_id=pid, branch_id=branch.branch_id, status="completed"
    )
    snapshot = await sm.create_snapshot(
        scene_id=scene.scene_id,
        branch_id=branch.branch_id,
        character_states={},
        world_revision_id=revision.revision_id,
    )
    scene.snapshot_id_before = snapshot.snapshot_id
    scene.snapshot_id_after = snapshot.snapshot_id
    scene.world_revision_id = revision.revision_id
    await repository.save_scene(scene)
    from backend.models import DirectorDecision, SceneEvaluation

    await repository.save_evaluation(
        SceneEvaluation(
            scene_id=scene.scene_id,
            world_operations=[
                WorldOperation(attribute="season", label="季节", value="冬", visibility="global")
            ],
        )
    )
    monkeypatch.setattr(
        orchestrator, "plan_scene", AsyncMock(return_value=SceneConfig(name="next"))
    )
    decision = await orchestrator.apply_decision(
        scene.scene_id, DirectorDecision(decision_type="next_scene")
    )
    assert decision.next_scene_id
    head = await repository.get_branch_world(pid, branch.branch_id)
    assert head.facts[fact_key("world", "world", "season")].value == "冬"
    assert (await sm.get_snapshot(snapshot.snapshot_id)).world_revision_id == revision.revision_id
    replay = await orchestrator.apply_decision(
        scene.scene_id, DirectorDecision(decision_type="next_scene")
    )
    assert replay.next_scene_id == decision.next_scene_id
    assert len(await repository.list_world_events(pid, branch.branch_id)) == 1


def test_environment_internal_state_is_authoritative_but_never_broadcast():
    operation = op(attribute="secret_counter", value=3)
    operation.visibility = "hidden"
    revision = apply_operations(
        root(),
        [operation],
        "hidden-change",
        ["a", "b"],
        source="environment",
        allowed_objects={"niche"},
    )
    assert object_state(revision, "niche")["secret_counter"] == 3
    assert revision.observations == {}
    assert render_world_for_character(revision, "a", ["niche"]) == ""
    assert render_world_for_character(revision, "b", []) == ""
    parsed = parse_world_adjudication(
        json.dumps(
            {
                "executable": True,
                "triggered": True,
                "narration": "微光一闪。",
                "operations": [to_dict(operation)],
            }
        ),
        {"niche"},
    )
    assert parsed is not None


def test_ambiguous_legacy_object_names_are_not_auto_mapped():
    from backend.services.world import migration_preview

    duplicate = WorldObject(object_id="other-niche", name=NICHE.name)
    preview = migration_preview({"暗格·开启": "True"}, [NICHE, duplicate])
    assert preview["entries"][0]["suggestion"] is None


def test_returning_observer_discovers_removal_without_receiving_unseen_changes_early():
    from backend.services.world import capture_current_observations

    first = apply_operations(root(), [op()], "open", ["a"])
    removed = apply_operations(first, [op(op="unset")], "remove", [])
    assert "是" in render_world_for_character(removed, "a", [])
    assert "已被清除" in render_world_for_character(removed, "a", ["niche"])
    assert capture_current_observations(removed, ["a"], ["niche"])
    assert "已被清除" in render_world_for_character(removed, "a", [])
    assert object_state(removed, "niche") == {}


def test_reveal_effect_cannot_smuggle_free_text_from_hidden_rules():
    result = {
        "executable": True,
        "triggered": True,
        "narration": "光一闪",
        "operations": [],
        "reveal_effect": "actor_memory，王后的秘密是某事",
    }
    assert parse_world_adjudication(json.dumps(result), {"crown"}) is None


@pytest.mark.asyncio
async def test_invalid_environment_reference_fails_once_without_accepting_narration(monkeypatch):
    pid = "u-admission-failure"
    branch, revision, sm = await stored_world(pid)
    source = DialogueTurn(
        turn_id="t-admission",
        character_id="a",
        character_name="A",
        actions=[ActionIntent(object_id="crown", text="佩戴王冠", status="pending")],
    )
    scene = Scene(
        project_id=pid,
        branch_id=branch.branch_id,
        participating_characters=["a"],
        objects_present=["crown"],
    )
    from backend.models import WorldAdjudication

    adjudicator = EnvironmentAgent()
    monkeypatch.setattr(
        adjudicator,
        "adjudicate_world",
        AsyncMock(
            return_value=WorldAdjudication(
                narration="王冠换了持有者",
                operations=[
                    op(
                        attribute="holder",
                        value={"kind": "character", "id": "missing"},
                        oid="crown",
                    )
                ],
            )
        ),
    )
    actor = CharacterAgent(
        CharacterCard(character_id="a", project_id=pid, name="A"), MemoryManager("a", pid)
    )
    monkeypatch.setattr(actor, "retrieve_relevant_memory", AsyncMock(return_value=[]))

    async def commit(*args):
        raise InvalidRequestError("角色不存在")

    failed = AsyncMock()
    engine = SceneEngine(
        scene,
        SceneConfig(),
        [actor],
        sm,
        objects=[CROWN],
        environment_mode="adjudicate",
        adjudicator=adjudicator,
        world_revision=revision,
        world_commit=commit,
    )
    turns = [source]
    await engine._adjudicate_pending(source, turns, [], None, failed)
    assert len(turns) == 1 and source.actions[0].status == "failed" and source.revision == 1
    assert actor.memory.short_term.recent() == []
    failed.assert_awaited_once_with(source, None)
    await engine._adjudicate_pending(source, turns, [], None, failed)
    adjudicator.adjudicate_world.assert_awaited_once()
