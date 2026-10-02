"""修复已有项目的世界设定可见性（工单29）。

用法：
    python -m scripts.reclassify_lore --project PROJECT_ID [--apply]

工单29 之前，构建把每条世界设定都以 `global` 复制进全部角色卡，种子里只有部分角色
知道的秘密因此对全员可见。构建代码修了，已有项目的角色卡仍带着这些条目，不会自愈。

本脚本对卡片上的非角色专属条目重新判定可见性（与构建共用 `LoreVisibilityClassifier`），
再按判定结果重新分发：公开的进全部角色卡，私有的只进知情者，隐藏的与判定失败的移除。
已经是 `character:` 的条目原样保留。

- 默认只预览，不写任何文件；加 `--apply` 才写入；
- 写入前把全部角色卡的旧设定备份到项目目录下的 `lore_backup_{时间}.json`，
  被移除的条目只在那里留存（目前没有导演侧的设定存储，见工单29 §4）；
- 每次运行都会调一次 LLM（每 20 条设定一次），判定有随机性，预览与写入是两次独立判定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from backend.config import settings
from backend.exceptions import PlotSystemError
from backend.graphrag_pipeline.pipeline import read_seed_texts
from backend.graphrag_pipeline.world_rules import (
    CALL_FAILED,
    LoreVerdict,
    LoreVisibilityClassifier,
    lore_for_character,
)
from backend.models import CharacterCard, LoreEntry
from backend.services import repository
from backend.utils.db import init_db
from backend.utils.serializer import to_dict

# 与构建时交给角色卡生成的上下文同长（pipeline.run 的 full_context）
_SEED_CHARS = 8000


@dataclass
class ReclassifyReport:
    cards: list[CharacterCard] = field(default_factory=list)
    verdicts: list[LoreVerdict] = field(default_factory=list)
    #: character_id -> 重新分发后的设定
    new_lore: dict[str, list[LoreEntry]] = field(default_factory=dict)
    backup_path: Path | None = None
    #: 有批次的判定调用失败，写入模式因此没有写
    aborted: bool = False


def _candidates(cards: list[CharacterCard]) -> list[LoreEntry]:
    """需要重新判定的条目：非角色专属的，按 lore_id 去重（旧实现给每张卡复制同一个 id）。"""
    seen: dict[str, LoreEntry] = {}
    for card in cards:
        for entry in card.world_lore_entries:
            if not entry.scope.startswith("character:"):
                seen.setdefault(entry.lore_id, entry)
    return list(seen.values())


def _redistribute(card: CharacterCard, verdicts: list[LoreVerdict]) -> list[LoreEntry]:
    kept = [e for e in card.world_lore_entries if e.scope.startswith("character:")]
    result = list(kept)
    known_ids = {e.lore_id for e in kept}
    for entry in lore_for_character(verdicts, card.character_id):
        if entry.lore_id not in known_ids:
            known_ids.add(entry.lore_id)
            result.append(entry)
    return result


def _write_backup(project_id: str, cards: list[CharacterCard]) -> Path:
    path = settings.project_dir(project_id) / f"lore_backup_{datetime.now():%Y%m%d-%H%M%S}.json"
    payload = {
        card.character_id: {
            "name": card.name,
            "world_lore_entries": [to_dict(e) for e in card.world_lore_entries],
        }
        for card in cards
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def reclassify_project(
    project_id: str,
    *,
    apply: bool = False,
    classifier: LoreVisibilityClassifier | None = None,
) -> ReclassifyReport:
    project = await repository.get_project(project_id)
    cards = sorted(await repository.list_characters(project_id), key=lambda c: c.name)
    report = ReclassifyReport(cards=cards)

    candidates = _candidates(cards)
    if not candidates:
        return report

    seed = "\n\n".join(read_seed_texts(project.seed_texts))[:_SEED_CHARS]
    classifier = classifier or LoreVisibilityClassifier()
    report.verdicts = await classifier.classify(candidates, cards, seed)
    report.new_lore = {c.character_id: _redistribute(c, report.verdicts) for c in cards}

    if apply:
        # 构建时没人能重试，判定失败只能收紧；这里有人在场，调用失败（多半是网络抖动）
        # 就整个不写、让人重试，而不是把这批设定从所有卡上撤掉。
        # "判了但判不出"（知情者对不上等）不在此列，照常按收紧处理。
        if any(v.note == CALL_FAILED for v in report.verdicts):
            report.aborted = True
            return report
        # 备份先于任何写入：备份失败就整个不写
        report.backup_path = _write_backup(project_id, cards)
        for card in cards:
            card.world_lore_entries = report.new_lore[card.character_id]
            await repository.save_character(card)
    return report


def _print_report(report: ReclassifyReport, apply: bool) -> None:
    if not report.verdicts:
        print("没有需要重新判定的设定（所有条目都已是角色专属）。")
        return

    names = {c.character_id: c.name for c in report.cards}
    label = {"public": "公开", "private": "私有", "hidden": "隐藏", "withheld": "判定失败"}
    print(f"共 {len(report.verdicts)} 条设定重新判定：\n")
    for v in report.verdicts:
        who = "、".join(names[h] for h in v.holders) if v.holders else ""
        tag = label.get(v.visibility, v.visibility)
        extra = f" → {who}" if who else (f"（{v.note}）" if v.note else "")
        print(f"[{tag}]{extra}\n    {v.entry.content[:100]}")

    print("\n各角色卡的设定条数（现在 → 修复后）：")
    for card in report.cards:
        print(f"  {card.name}: {len(card.world_lore_entries)} → {len(report.new_lore[card.character_id])}")

    if report.aborted:
        print("\n有批次的判定调用失败，为免把设定从所有角色卡上撤掉，本次未写入。请稍后重试。")
    elif apply:
        print(f"\n已写入。旧设定备份在：{report.backup_path}")
    else:
        print("\n以上为预览，未写入任何文件。确认后加 --apply 执行（会重新判定一次）。")


async def _main(project_id: str, apply: bool) -> int:
    await init_db()
    try:
        report = await reclassify_project(project_id, apply=apply)
    except PlotSystemError as exc:
        print(f"未找到项目 {project_id}：{exc}")
        return 1
    _print_report(report, apply)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="重新判定已有项目的世界设定可见性（工单29）")
    parser.add_argument("--project", required=True, help="project_id")
    parser.add_argument("--apply", action="store_true", help="写入角色卡（默认只预览）")
    args = parser.parse_args()
    return asyncio.run(_main(args.project, args.apply))


if __name__ == "__main__":
    sys.exit(main())
