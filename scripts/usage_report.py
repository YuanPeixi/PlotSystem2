"""按场景与用途列出 LLM 调用次数与 token（工单25）。

用法：
    python -m scripts.usage_report --project PROJECT_ID [--branch BRANCH_ID]

**只读**，不调 LLM。数据来自两处：
- `Scene.llm_usage`：场景运行期间的调用（角色、selector、意图抽取、压缩、embedding……），
  continue 的多段已累加；
- `SceneEvaluation.llm_usage`：该场最近一份评估花掉的调用。continue 后评估会被覆盖，
  因此只反映最新那一次评估。

token 一列带"≈"的表示其中有调用按 `estimate_tokens` 估算（服务商没返回 usage）。
构建、规划、输出生成不属于任何场景，不在这里（工单25 §3.2）。
计数字段上线前跑过的场景没有记录，显示为"无记录"。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping

from backend.exceptions import PlotSystemError
from backend.models import LLMPurpose, LLMUsageStat
from backend.services import repository
from backend.utils.db import init_db
from backend.utils.usage import UsageMeter, total

_ORDER = [p.value for p in LLMPurpose]


def _row(label: str, stat: LLMUsageStat) -> str:
    approx = "≈" if stat.estimated_calls else " "
    extra = []
    if stat.failures:
        extra.append(f"失败 {stat.failures}")
    if stat.retries:
        extra.append(f"重试 {stat.retries}")
    tail = f"  （{'，'.join(extra)}）" if extra else ""
    return (
        f"    {label:<15}{stat.calls:>6} 次  输入{approx}{stat.prompt_tokens:>9}  "
        f"输出{approx}{stat.completion_tokens:>8}  {stat.seconds:>8.1f}s{tail}"
    )


def _print_block(title: str, stats: Mapping[str, LLMUsageStat]) -> None:
    print(f"  {title}")
    if not stats:
        print("    无记录")
        return
    known = [p for p in _ORDER if p in stats]
    unknown = sorted(p for p in stats if p not in _ORDER)
    for purpose in known + unknown:
        print(_row(purpose, stats[purpose]))
    if len(stats) > 1:
        print(_row("小计", total(stats)))


async def _main(project_id: str, branch_id: str | None) -> int:
    await init_db()
    try:
        await repository.get_project(project_id)
    except PlotSystemError as exc:
        print(f"未找到项目 {project_id}：{exc}")
        return 1
    scenes = await repository.list_scenes(project_id, branch_id)
    evaluations = await repository.get_evaluations([s.scene_id for s in scenes])
    scene_sum, eval_sum = UsageMeter(), UsageMeter()
    for scene in scenes:
        evaluation = evaluations.get(scene.scene_id)
        print(f"\n场景「{scene.name}」（{scene.scene_id[:8]}，{scene.status}，{scene.turns_completed} 轮）")
        _print_block("运行", scene.llm_usage)
        _print_block("评估", evaluation.llm_usage if evaluation else {})
        scene_sum.seed(scene.llm_usage)
        if evaluation:
            eval_sum.seed(evaluation.llm_usage)

    print("\n== 合计 ==")
    _print_block("运行", scene_sum.snapshot())
    _print_block("评估", eval_sum.snapshot())
    both = UsageMeter(scene_sum.snapshot())
    both.seed(eval_sum.snapshot())
    print(_row("总计", total(both.snapshot())))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="按场景与用途列出 LLM 调用与 token（工单25，只读）")
    parser.add_argument("--project", required=True, help="project_id")
    parser.add_argument("--branch", default=None, help="只统计这条分支")
    args = parser.parse_args()
    return asyncio.run(_main(args.project, args.branch))


if __name__ == "__main__":
    sys.exit(main())
