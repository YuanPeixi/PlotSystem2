"""统计 record 档下的动作意图命中率与抽取调用次数（工单24 PR-1b）。

用法：
    python -m scripts.action_stats --project PROJECT_ID [--branch BRANCH_ID]

**只读**，不调 LLM。按场景读 `dialogue_log` 里每一轮的 `actions`：

- 动作段总数 / 预过滤命中段数（`skip_reason` 不是 `no_object` 的）；
- **抽取调用次数** = 有命中段的轮次数（一轮最多一次调用）；
- 各 `skip_reason` 的分布、`recorded` 数，以及按物件统计的记录次数。

`ENVIRONMENT_MODE` 按段冻结（设计单 A21）：同一场的前后两段可能处于不同档位，off 档跑出来的
轮次 `actions` 为空，不计入任何分母 —— "段数"只统计 record 档下产生的轮次。
token 用量见 `python -m scripts.usage_report`（工单25，按用途计 `action_extract`）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field

from backend.exceptions import PlotSystemError
from backend.models import ActionSkipReason, ActionStatus, Scene
from backend.services import repository
from backend.utils.db import init_db
from backend.utils.turns import is_character_turn


@dataclass
class SceneStats:
    scene_id: str
    name: str
    recorded_turns: int = 0  # record 档下产生、带 actions 的角色轮次
    segments: int = 0
    hits: int = 0
    calls: int = 0
    recorded: int = 0
    reasons: Counter = field(default_factory=Counter)
    by_object: Counter = field(default_factory=Counter)

    def add(self, other: SceneStats) -> None:
        self.recorded_turns += other.recorded_turns
        self.segments += other.segments
        self.hits += other.hits
        self.calls += other.calls
        self.recorded += other.recorded
        self.reasons.update(other.reasons)
        self.by_object.update(other.by_object)


def scene_stats(scene: Scene) -> SceneStats:
    stats = SceneStats(scene.scene_id, scene.name)
    for turn in scene.dialogue_log:
        if not is_character_turn(turn) or not turn.actions:
            continue
        stats.recorded_turns += 1
        stats.segments += len(turn.actions)
        hit_here = 0
        for intent in turn.actions:
            if intent.status == ActionStatus.RECORDED.value:
                stats.recorded += 1
                stats.by_object[intent.object_id] += 1
            else:
                stats.reasons[intent.skip_reason or "unknown"] += 1
            if intent.skip_reason != ActionSkipReason.NO_OBJECT.value:
                hit_here += 1
        stats.hits += hit_here
        # over_limit 的段落没送出去，但只要有一段送了，这一轮就有且只有一次调用
        sent = sum(
            1 for i in turn.actions
            if i.skip_reason not in (ActionSkipReason.NO_OBJECT.value, ActionSkipReason.OVER_LIMIT.value)
        )
        stats.calls += 1 if sent else 0
    return stats


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "—"


def _print(stats: SceneStats, names: dict[str, str], title: str) -> None:
    print(f"\n{title}")
    if not stats.recorded_turns:
        print("  没有 record 档下产生的轮次。")
        return
    print(
        f"  轮次 {stats.recorded_turns}，动作段 {stats.segments}，"
        f"命中物件 {stats.hits}（{_pct(stats.hits, stats.segments)}），"
        f"抽取调用 {stats.calls}（每轮 {stats.calls / stats.recorded_turns:.2f} 次）"
    )
    print(f"  记录为尝试 {stats.recorded}（占命中 {_pct(stats.recorded, stats.hits)}）")
    for reason in ActionSkipReason:
        if stats.reasons[reason.value]:
            print(f"    {reason.value}: {stats.reasons[reason.value]}")
    if stats.reasons["unknown"]:
        print(f"    （无原因）: {stats.reasons['unknown']}")
    for oid, n in stats.by_object.most_common():
        print(f"    物件 {names.get(oid, oid)}：{n}")


async def _main(project_id: str, branch_id: str | None) -> int:
    await init_db()
    try:
        await repository.get_project(project_id)
    except PlotSystemError as exc:
        print(f"未找到项目 {project_id}：{exc}")
        return 1
    names = {o.object_id: o.name for o in await repository.list_objects(project_id)}
    total = SceneStats("", "合计")
    for scene in await repository.list_scenes(project_id, branch_id):
        stats = scene_stats(scene)
        if stats.recorded_turns:
            _print(stats, names, f"场景「{scene.name}」（{scene.scene_id[:8]}）")
        total.add(stats)
    _print(total, names, "== 合计 ==")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="统计动作意图命中率与抽取调用次数（工单24，只读）")
    parser.add_argument("--project", required=True, help="project_id")
    parser.add_argument("--branch", default=None, help="只统计这条分支")
    args = parser.parse_args()
    return asyncio.run(_main(args.project, args.branch))


if __name__ == "__main__":
    sys.exit(main())
