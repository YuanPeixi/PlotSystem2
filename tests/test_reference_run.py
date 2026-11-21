"""参考记录的导出 / 导入（`scripts.reference_run`）。

起点可复现的前提是构建产物逐字相同：角色卡的已知 / 未知事实、私有设定的 scope、
物件的隐藏规则与知情者。这里钉住往返不丢这些，以及导出物里不带密钥。
"""

from __future__ import annotations

import json

from backend.config import settings
from backend.models import (
    ActionIntent,
    ActionStatus,
    CharacterCard,
    DialogueTurn,
    LoreEntry,
    Project,
    ProjectStatus,
    RevealEntry,
    Scene,
    SceneStatus,
    TurnKind,
    WorldObject,
    new_id,
)
from backend.services import repository
from backend.snapshot import SnapshotManager
from scripts import reference_run

_REVEAL = "雨夜，她把襁褓递出宫墙。"


async def _recorded_project(tmp_path) -> tuple[Project, CharacterCard, WorldObject, Scene]:
    project = Project(name="玻璃王冠", narrative_goal="让王冠投出王后的记忆", world_schema_version=2)
    seed = tmp_path / "seed.txt"
    seed.write_text("王冠只读取记忆。", encoding="utf-8")
    project.seed_texts = [str(seed)]
    await repository.save_project(project)

    queen = CharacterCard(
        project_id=project.project_id,
        name="塞芙拉",
        persona="过度自信于掌控",
        known_facts=["伊莎贝尔并非国王亲生"],
        unknown_facts=["账页藏在王冠底座"],
        current_location="仪式大厅",
    )
    queen.world_lore_entries = [
        LoreEntry(content="粮仓短缺是她安排的", keywords=["粮仓"], scope=f"character:{queen.character_id}")
    ]
    await repository.save_character(queen)

    crown = WorldObject(
        project_id=project.project_id,
        name="玻璃王冠",
        aliases=["王冠"],
        public_description="一顶玻璃王冠。",
        hidden_rules=["佩戴者最强烈的记忆会投射给在场所有人"],
        visibility="private",
        known_by=[queen.character_id],
    )
    await repository.save_object(crown)

    branch = await SnapshotManager(project.project_id).ensure_main_branch()
    source = DialogueTurn(
        turn_id=new_id(), turn_number=1, character_id=queen.character_id, character_name="塞芙拉",
        action="将王冠举至鬓边", dialogue="看着。", inner_thought="我压得住",
        actions=[ActionIntent(index=0, text="将王冠举至鬓边", object_id=crown.object_id, verb="戴上",
                              status=ActionStatus.RESOLVED.value)],
    )
    env = DialogueTurn(
        turn_id=new_id(), turn_number=2, kind=TurnKind.ENVIRONMENT.value, character_name="环境",
        narration="王冠泛起微光。", projection=_REVEAL,
    )
    scene = Scene(
        project_id=project.project_id,
        branch_id=branch.branch_id,
        name="正午加冕",
        description="王后亲自试戴验冠。",
        participating_characters=[queen.character_id],
        location="仪式大厅",
        objects_present=[crown.object_id],
        environment_script={crown.object_id: [RevealEntry(
            condition="塞芙拉戴上王冠", content=_REVEAL, actor_id=queen.character_id, audience="present",
        )]},
        initial_conditions={"time": "正午", "opening_narration": "钟声撞过十二下。"},
        max_turns=24,
        speaker_mode="selector",
        status=SceneStatus.COMPLETED.value,
        dialogue_log=[source, env],
    )
    await repository.save_scene(scene)
    return project, queen, crown, scene


async def test_round_trip_rebuilds_the_same_starting_point(tmp_path):
    project, queen, crown, _ = await _recorded_project(tmp_path)
    out = tmp_path / "record"

    await reference_run.export_run(project.project_id, out)
    report = await reference_run.import_run(out, name="复现")

    assert report.project_id != project.project_id
    copy = await repository.get_project(report.project_id)
    assert (copy.name, copy.status, copy.world_schema_version) == ("复现", ProjectStatus.READY.value, 2)
    assert copy.narrative_goal == project.narrative_goal
    assert [open(p, encoding="utf-8").read() for p in copy.seed_texts] == ["王冠只读取记忆。"]

    [card] = await repository.list_characters(report.project_id)
    assert card.character_id == queen.character_id
    assert (card.known_facts, card.unknown_facts) == (queen.known_facts, queen.unknown_facts)
    # 私有设定按角色 ID 定 scope：ID 换了就谁也看不到这条设定
    assert card.world_lore_entries[0].scope == f"character:{queen.character_id}"
    # 场景写回的展示缓存不属于起点
    assert card.current_location == ""

    [obj] = await repository.list_objects(report.project_id)
    assert (obj.object_id, obj.hidden_rules, obj.visibility, obj.known_by) == (
        crown.object_id, crown.hidden_rules, "private", [queen.character_id],
    )
    assert report.branch_id and not report.scene_id
    assert await repository.list_scenes(report.project_id) == []


async def test_scene_option_recreates_the_directors_script(tmp_path):
    project, queen, crown, recorded = await _recorded_project(tmp_path)
    out = tmp_path / "record"
    await reference_run.export_run(project.project_id, out)

    report = await reference_run.import_run(out, scene_number=1)

    scene = await repository.get_scene(report.scene_id)
    assert (scene.status, scene.dialogue_log) == (SceneStatus.PENDING.value, [])
    assert (scene.description, scene.max_turns, scene.speaker_mode) == ("王后亲自试戴验冠。", 24, "selector")
    assert scene.objects_present == [crown.object_id]
    assert scene.initial_conditions == recorded.initial_conditions
    [entry] = scene.environment_script[crown.object_id]
    # 绑定与受众丢了，揭示就退成"谁戴都触发、只给执行者"
    assert (entry.actor_id, entry.audience, entry.content) == (queen.character_id, "present", _REVEAL)


async def test_export_is_readable_and_carries_no_secrets(tmp_path, monkeypatch):
    project, _, _, _ = await _recorded_project(tmp_path)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-must-not-leak")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://user:sk-in-url@openrouter.ai/api/v1")
    out = tmp_path / "record"

    await reference_run.export_run(project.project_id, out)

    exported = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert "sk-must-not-leak" not in exported and "sk-in-url" not in exported
    recorded = json.loads((out / "settings.json").read_text(encoding="utf-8"))
    assert recorded["LLM_BASE_URL"] == "openrouter.ai"
    assert not [k for k in recorded if "API_KEY" in k or "DATA_DIR" in k]

    transcript = (out / "transcript.md").read_text(encoding="utf-8")
    # 导演视角：独白、公开投射与预制揭示的绑定都在
    assert "[我压得住]" in transcript and f"（幻象：{_REVEAL}）" in transcript
    assert "执行者 塞芙拉｜受众 present" in transcript and "戴上 → resolved" in transcript


async def test_import_reports_settings_that_differ(tmp_path, monkeypatch):
    project, _, _, _ = await _recorded_project(tmp_path)
    out = tmp_path / "record"
    monkeypatch.setattr(settings, "ENVIRONMENT_MODE", "adjudicate")
    await reference_run.export_run(project.project_id, out)

    monkeypatch.setattr(settings, "ENVIRONMENT_MODE", "off")
    report = await reference_run.import_run(out)

    assert report.settings_diff == {"ENVIRONMENT_MODE": ("adjudicate", "off")}
