"""把一次推演存成参考记录，或从参考记录建出同起点的新项目。

用法：
    python -m scripts.reference_run export --project PROJECT_ID --out docs/test-fixtures/reference-runs/NAME
    python -m scripts.reference_run import --from docs/test-fixtures/reference-runs/NAME [--name 项目名] [--scene N]

为什么需要它：角色卡与物件（含隐藏规则）是构建时模型生成的，换一台机器重新构建拿不到同一份，
"同一个种子"并不等于同一个起点。导出把构建产物、各场的导演脚本与对白、导出时的非密钥配置
存成可进 git 的文本；导入跳过构建，直接建出角色卡与物件逐字相同的项目。两者都不调 LLM。

- 导出不含快照、向量库与图谱（体积大，且绑定本机）。因此导入的项目从**空记忆**起步；
- `--scene N` 另建一个待开演的场景，照搬记录里第 N 场的导演脚本（描述、参演、在场物件、
  预制揭示、初始条件）。用来把导演的随机性固定住，只重跑角色与环境裁决；角色没有前几场的记忆；
- 配置取自**导出那一刻**的进程配置，不是那次推演运行时的：之间改过 `.env` 要自己核对；
- 角色卡的 `current_location` 导出时清空：它是场景运行写回的展示缓存，不属于构建产物。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from backend.config import settings
from backend.exceptions import PlotSystemError
from backend.models import (
    CharacterCard,
    DialogueTurn,
    LoreEntry,
    Project,
    ProjectStatus,
    RelationshipState,
    RevealEntry,
    Scene,
    SceneConfig,
    SceneEvaluation,
    WorldObject,
)
from backend.services import orchestrator, repository
from backend.snapshot import SnapshotManager
from backend.utils.db import init_db
from backend.utils.serializer import to_dict, to_json
from backend.utils.turns import is_character_turn, render_turn

FORMAT_VERSION = 1

#: 不进记录的配置：密钥、本机路径与只影响本机进程的项
_SETTINGS_SKIPPED = ("API_KEY", "DATA_DIR", "BACKEND_", "DEBUG", "LOG_LEVEL")


@dataclass
class ImportReport:
    project_id: str = ""
    branch_id: str = ""
    characters: int = 0
    objects: int = 0
    #: `--scene` 建出的待开演场景；没给时为空
    scene_id: str = ""
    #: 记录里的配置与本机当前配置不同的项：名字 → (记录值, 本机值)
    settings_diff: dict[str, tuple[object, object]] = field(default_factory=dict)


def exportable_settings() -> dict[str, object]:
    """可进 git 的配置：去掉密钥与本机路径，服务地址只留主机名（同回放报告的口径）。"""
    out: dict[str, object] = {}
    for name in type(settings).model_fields:
        if any(mark in name for mark in _SETTINGS_SKIPPED):
            continue
        value = getattr(settings, name)
        if name.endswith("BASE_URL"):
            value = urlparse(str(value)).hostname or ""
        out[name] = value
    return out


def _dump(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(data) + "\n", encoding="utf-8")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def _turn_lines(turn: DialogueTurn) -> list[str]:
    # 导演视角：独白与私密细节都展开
    lines = [f"{turn.turn_number:02d}. {render_turn(turn, inner_thought=True, private_detail=True).strip()}"]
    if is_character_turn(turn):
        acted = [
            f"{a.verb or '?'} → {a.status}{f'（{a.skip_reason}）' if a.skip_reason else ''}"
            for a in turn.actions
            if a.object_id
        ]
        if acted:
            lines.append(f"    - 动作意图：{'；'.join(acted)}")
    for label, notice in (("输出", turn.output_notice), ("选人", turn.selector_notice)):
        if notice:
            lines.append(f"    - {label}提示：{notice}")
    return lines


def render_transcript(
    project: Project,
    scenes: list[Scene],
    evaluations: dict[str, SceneEvaluation],
    names: dict[str, str],
    object_names: dict[str, str],
) -> str:
    """给人读的全程记录（导演视角，含独白、私密细节与预制揭示）。"""
    out = [f"# {project.name}：全程记录（导演视角）", ""]
    for number, scene in enumerate(scenes, 1):
        out += [f"## 第 {number} 场　{scene.name}", ""]
        out.append(f"- 地点：{scene.location}")
        out.append(f"- 参演：{'、'.join(names.get(c, c) for c in scene.participating_characters)}")
        out.append(f"- 在场物件：{'、'.join(object_names.get(o, o) for o in scene.objects_present) or '（无）'}")
        out.append(f"- 状态：{scene.status}，{len(scene.dialogue_log)} 轮（上限 {scene.max_turns} 个角色轮次）")
        out += ["", "**导演写的场景描述**（会进每个在场角色的 system）：", "", scene.description, ""]
        for object_id, entries in scene.environment_script.items():
            for entry in entries:
                actor = names.get(entry.actor_id, entry.actor_id) or "任何人"
                out.append(
                    f"- 预制揭示｜{object_names.get(object_id, object_id)}｜执行者 {actor}｜受众 {entry.audience}"
                    f"｜触发：{entry.condition}｜内容：{entry.content}"
                )
        if scene.environment_script:
            out.append("")
        opening = scene.initial_conditions.get("opening_narration")
        if opening:
            out += [f"> {opening}", ""]
        for turn in scene.dialogue_log:
            out += _turn_lines(turn)
        evaluation = evaluations.get(scene.scene_id)
        if evaluation:
            out += [
                "",
                f"**评估**：目标 {evaluation.narrative_goal_score}｜张力 {evaluation.dramatic_tension_score}"
                f"｜一致性 {evaluation.character_consistency_score}｜偏离 {evaluation.plot_deviation_score}"
                f"｜推进度 {evaluation.story_progress}｜建议 {evaluation.recommended_decision}",
            ]
            if evaluation.synopsis:
                out += ["", evaluation.synopsis]
        out.append("")
    return "\n".join(out)


async def export_run(project_id: str, out_dir: Path) -> list[Scene]:
    """把项目的构建产物与全部场次写进 `out_dir`，返回导出的场景（按创建时间）。"""
    project = await repository.get_project(project_id)
    cards = await repository.list_characters(project_id)
    objects = await repository.list_objects(project_id)
    scenes = sorted(await repository.list_scenes(project_id), key=lambda s: s.created_at)
    evaluations = await repository.get_evaluations([s.scene_id for s in scenes])

    seeds = []
    for raw in project.seed_texts:
        src = Path(raw)
        if not src.exists():
            continue
        dest = out_dir / "seed" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        seeds.append(src.name)

    _dump(out_dir / "project.json", {
        "format_version": FORMAT_VERSION,
        "source_project_id": project.project_id,
        "name": project.name,
        "description": project.description,
        "narrative_goal": project.narrative_goal,
        "ending_criteria": project.ending_criteria,
        "world_schema_version": project.world_schema_version,
        "seed_files": seeds,
    })
    _dump(out_dir / "settings.json", exportable_settings())

    for card in cards:
        card.current_location = ""
        _dump(out_dir / "characters" / f"{card.character_id}.json", card)
    for obj in objects:
        _dump(out_dir / "objects" / f"{obj.object_id}.json", obj)

    for number, scene in enumerate(scenes, 1):
        _dump(out_dir / "scenes" / f"{number:02d}.json", {
            "scene": scene,
            "evaluation": evaluations.get(scene.scene_id),
            "decision": await repository.get_decision(scene.scene_id),
        })
    for branch_id in dict.fromkeys(s.branch_id for s in scenes):
        _dump(out_dir / "storyboard" / f"{branch_id}.json", await repository.get_storyboard(project_id, branch_id))

    transcript = render_transcript(
        project, scenes, evaluations,
        {c.character_id: c.name for c in cards}, {o.object_id: o.name for o in objects},
    )
    (out_dir / "transcript.md").write_text(transcript, encoding="utf-8")
    return scenes


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def _card(data: dict, project_id: str) -> CharacterCard:
    known = {f.name for f in fields(CharacterCard)}
    card = CharacterCard(**{k: v for k, v in data.items() if k in known})
    card.project_id = project_id
    card.world_lore_entries = [LoreEntry(**e) for e in data.get("world_lore_entries") or []]
    card.relationships = {
        k: RelationshipState(**v) for k, v in (data.get("relationships") or {}).items()
    }
    return card


def _object(data: dict, project_id: str) -> WorldObject:
    known = {f.name for f in fields(WorldObject)}
    obj = WorldObject(**{
        k: (datetime.fromisoformat(v) if k in {"created_at", "updated_at"} else v)
        for k, v in data.items()
        if k in known
    })
    obj.project_id = project_id
    return obj


def _scene_config(data: dict) -> SceneConfig:
    """记录里一场的导演脚本。开场白已在 `initial_conditions` 里，不另传。"""
    return SceneConfig(
        name=data["name"],
        description=data.get("description", ""),
        participating_characters=list(data.get("participating_characters") or []),
        location=data.get("location", ""),
        objects_present=list(data.get("objects_present") or []),
        environment_script={
            oid: [RevealEntry(**e) for e in entries]
            for oid, entries in (data.get("environment_script") or {}).items()
        },
        initial_conditions=dict(data.get("initial_conditions") or {}),
        max_turns=int(data.get("max_turns") or settings.DEFAULT_MAX_TURNS),
        speaker_mode=data.get("speaker_mode") or settings.DEFAULT_SPEAKER_MODE,
    )


async def import_run(src_dir: Path, *, name: str = "", scene_number: int = 0) -> ImportReport:
    """从记录建一个新项目：角色卡、物件、主分支。角色与物件沿用记录里的 ID（揭示脚本、
    知情者名单、私有设定的 scope 都按 ID 引用），项目 ID 是新的，可重复导入。"""
    meta = _load(src_dir / "project.json")
    if meta.get("format_version") != FORMAT_VERSION:
        raise PlotSystemError(f"不认识的记录格式版本：{meta.get('format_version')!r}")
    scene_data = None
    if scene_number:
        scene_path = src_dir / "scenes" / f"{scene_number:02d}.json"
        if not scene_path.exists():
            raise PlotSystemError(f"记录里没有第 {scene_number} 场")
        scene_data = _load(scene_path)["scene"]

    project = Project(
        name=name or meta["name"],
        description=meta.get("description", ""),
        narrative_goal=meta.get("narrative_goal", ""),
        ending_criteria=meta.get("ending_criteria", ""),
        world_schema_version=int(meta.get("world_schema_version", 2)),
        status=ProjectStatus.READY.value,
    )
    seed_dir = settings.project_dir(project.project_id) / "seed_texts"
    seed_dir.mkdir(parents=True, exist_ok=True)
    for filename in meta.get("seed_files", []):
        dest = seed_dir / filename
        dest.write_bytes((src_dir / "seed" / filename).read_bytes())
        project.seed_texts.append(str(dest))
    await repository.save_project(project)

    report = ImportReport(project_id=project.project_id)
    for path in sorted((src_dir / "characters").glob("*.json")):
        await repository.save_character(_card(_load(path), project.project_id))
        report.characters += 1
    for path in sorted((src_dir / "objects").glob("*.json")):
        await repository.save_object(_object(_load(path), project.project_id))
        report.objects += 1
    branch = await SnapshotManager(project.project_id).ensure_main_branch()
    report.branch_id = branch.branch_id

    if scene_data is not None:
        scene = await orchestrator.create_scene_from_config(
            project.project_id, branch.branch_id, _scene_config(scene_data)
        )
        report.scene_id = scene.scene_id

    settings_path = src_dir / "settings.json"
    if settings_path.exists():
        current = to_dict(exportable_settings())
        for key, recorded in _load(settings_path).items():
            if key in current and current[key] != recorded:
                report.settings_diff[key] = (recorded, current[key])
    return report


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


async def _main(args: argparse.Namespace) -> int:
    await init_db()
    try:
        if args.command == "export":
            scenes = await export_run(args.project, Path(args.out))
            print(f"已导出 {len(scenes)} 场到 {args.out}")
            return 0
        report = await import_run(Path(args.source), name=args.name, scene_number=args.scene)
    except PlotSystemError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    print(f"已建项目 {report.project_id}（角色 {report.characters}、物件 {report.objects}，主分支 {report.branch_id}）")
    if report.scene_id:
        print(f"已建待开演场景 {report.scene_id}（照搬记录第 {args.scene} 场的导演脚本）")
    if report.settings_diff:
        print("本机配置与记录不同（按记录复现需先改 .env）：")
        for key, (recorded, current) in report.settings_diff.items():
            print(f"  {key}: 记录 {recorded!r}，本机 {current!r}")
    else:
        print("本机配置与记录一致")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 / 导入推演参考记录（不调 LLM）")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="把一个项目的构建产物与全部场次导出成文本")
    exp.add_argument("--project", required=True)
    exp.add_argument("--out", required=True)
    imp = sub.add_parser("import", help="从记录建一个同起点的新项目")
    imp.add_argument("--from", dest="source", required=True)
    imp.add_argument("--name", default="", help="新项目名，缺省沿用记录里的")
    imp.add_argument("--scene", type=int, default=0, help="另建一个照搬记录第 N 场导演脚本的待开演场景")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
