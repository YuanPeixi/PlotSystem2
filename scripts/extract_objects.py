"""给已有项目补抽物件（工单24）。

用法：
    python -m scripts.extract_objects --project PROJECT_ID [--apply]

工单24 之前构建的项目没有物件表。本脚本从种子文本抽取物件、判定可见性（与构建共用
`build_objects`），只**新增**项目里还没有的物件：名称或别名与已有物件撞上的跳过，
不覆盖用户手改过的内容，因此不需要备份。

- 默认只预览，不写任何文件；加 `--apply` 才写入；
- 任一段抽取或任一批可见性判定的调用失败，就整个不写，让人重试 —— 构建时没人能重试，
  只能收紧；这里有人在场，写一半的物件表比不写更难收拾；
- 每次运行都会调 LLM，预览与写入是两次独立抽取，结果可能不同。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from backend.exceptions import PlotSystemError
from backend.graphrag_pipeline.pipeline import build_objects, read_seed_texts
from backend.graphrag_pipeline.world_rules import (
    CALL_FAILED,
    LoreVisibilityClassifier,
    ObjectExtractor,
)
from backend.models import CharacterCard, WorldObject
from backend.services import repository
from backend.services.objects import MAX_PROJECT_OBJECTS, select_new_objects
from backend.utils.db import init_db

# 与构建时的 full_context 同长（pipeline.run）
_SEED_CHARS = 8000


@dataclass
class ExtractReport:
    cards: list[CharacterCard] = field(default_factory=list)
    existing: list[WorldObject] = field(default_factory=list)
    #: 将要新建的物件（预览）/ 已新建的物件（写入）
    fresh: list[WorldObject] = field(default_factory=list)
    #: 与已有物件同名而跳过的名称
    skipped: list[str] = field(default_factory=list)
    #: 超出项目上限而没有保存的名称
    over_limit: list[str] = field(default_factory=list)
    #: 抽取或判定有调用失败，写入模式因此没有写
    aborted: bool = False
    failures: list[str] = field(default_factory=list)


async def extract_project_objects(
    project_id: str,
    *,
    apply: bool = False,
    extractor: ObjectExtractor | None = None,
    classifier: LoreVisibilityClassifier | None = None,
) -> ExtractReport:
    project = await repository.get_project(project_id)
    cards = sorted(await repository.list_characters(project_id), key=lambda c: c.name)
    report = ExtractReport(cards=cards, existing=await repository.list_objects(project_id))

    texts = read_seed_texts(project.seed_texts)
    if not texts:
        report.failures.append("项目没有可读的种子文本")
        report.aborted = apply
        return report
    seed = "\n\n".join(texts)[:_SEED_CHARS]
    extraction, verdicts = await build_objects(
        project_id,
        texts,
        cards,
        seed,
        extractor or ObjectExtractor(),
        classifier or LoreVisibilityClassifier(),
    )
    if extraction.failed_chunks:
        report.failures.append(f"{extraction.failed_chunks} 段种子文本的物件抽取调用失败")
    failed_verdicts = sum(1 for v in verdicts if v.note == CALL_FAILED)
    if failed_verdicts:
        report.failures.append(f"{failed_verdicts} 个物件的可见性判定调用失败")

    report.fresh, report.skipped = select_new_objects(report.existing, extraction.objects)
    room = max(0, MAX_PROJECT_OBJECTS - len(report.existing))
    report.over_limit = [o.name for o in report.fresh[room:]]
    report.fresh = report.fresh[:room]

    if apply:
        if report.failures:
            report.aborted = True
            return report
        for obj in report.fresh:
            await repository.save_object(obj)
    return report


def _print_report(report: ExtractReport, apply: bool) -> None:
    names = {c.character_id: c.name for c in report.cards}

    def _who(visibility: str) -> str:
        if visibility == "global":
            return "公开"
        if visibility.startswith("character:"):
            return f"仅 {names.get(visibility.split(':', 1)[1], '?')}"
        return "隐藏"

    print(f"项目已有物件 {len(report.existing)} 个。")
    if report.fresh:
        print(f"\n新抽出 {len(report.fresh)} 个物件：\n")
        for obj in report.fresh:
            print(f"[{_who(obj.visibility)}] {obj.name}（别名：{'、'.join(obj.aliases) or '无'}）")
            print(f"    外观：{obj.public_description[:100] or '（无）'}")
            for rule in obj.hidden_rules:
                print(f"    规则：{rule[:100]}")
    else:
        print("\n没有抽出新的物件。")
    if report.skipped:
        print(f"\n与已有物件同名、已跳过：{'、'.join(report.skipped)}")
    if report.over_limit:
        print(f"\n超过每个项目 {MAX_PROJECT_OBJECTS} 个的上限、未保存：{'、'.join(report.over_limit)}")
    for failure in report.failures:
        print(f"\n⚠ {failure}")

    if report.aborted:
        print("\n有调用失败，为免写下半份物件表，本次未写入。请稍后重试。")
    elif apply:
        print(f"\n已写入 {len(report.fresh)} 个物件。")
    else:
        print("\n以上为预览，未写入任何文件。确认后加 --apply 执行（会重新抽取一次）。")


async def _main(project_id: str, apply: bool) -> int:
    await init_db()
    try:
        report = await extract_project_objects(project_id, apply=apply)
    except PlotSystemError as exc:
        print(f"未找到项目 {project_id}：{exc}")
        return 1
    _print_report(report, apply)
    return 1 if report.aborted else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="给已有项目补抽物件（工单24）")
    parser.add_argument("--project", required=True, help="project_id")
    parser.add_argument("--apply", action="store_true", help="写入物件文件（默认只预览）")
    args = parser.parse_args()
    return asyncio.run(_main(args.project, args.apply))


if __name__ == "__main__":
    sys.exit(main())
