"""从同一个快照反复分叉并跑完首场，统计环境层的触发情况。可无人值守。

用法：
    python -m scripts.rerun_scene --project PROJECT_ID --snapshot SNAPSHOT_ID --times 5
        [--out 目录] [--speaker-mode round_robin]

一次成功说明不了成功率。本脚本把导演脚本固定住（分叉出的首场照搬快照所属场景的描述、参演、
在场物件与预制揭示，角色带着快照时点的记忆），只让角色与环境裁决重新掷骰，跑 N 次后给出：
几次出现公开投影、由谁的哪个动作触发、有没有裁决失败、各用了多少轮与多少 token。

- **会调 LLM、会花钱**：每次是完整的一场加一次评估；
- 串行执行，每次分叉出独立分支（同一分支同时只能有一个场景推进世界），不动来源分支；
- 每跑完一次就重写 `report.md` 与 `results.json`，中途断了已完成的部分仍在；
- 连续两次失败即停（密钥失效、额度用尽时不空烧）；
- **运行期间不要启动或重启后端**：启动对账会把正在跑的场景改成 paused（契约 9 的单进程假设）。
  检测到后端端口在监听时拒绝开跑；
- `--speaker-mode round_robin` 省掉选人调用（通常占七成 token），代价是发言顺序与原场不同。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from backend.config import settings
from backend.exceptions import PlotSystemError
from backend.models import LLMPurpose, Scene, SceneEvaluation, SceneStatus, SpeakerMode
from backend.services import orchestrator, repository
from backend.utils.db import init_db
from backend.utils.turns import count_character_turns, is_character_turn
from scripts.reference_run import render_transcript

#: 连续失败到这个数就停：多半是密钥、额度或网络问题，继续只会空烧
MAX_CONSECUTIVE_FAILURES = 2


@dataclass
class Trigger:
    """一次带公开投影的环境回合，连同引出它的那个动作。"""

    turn_number: int
    actor: str
    #: 角色写的动作原文：人读它来分辨"真做了"还是"举着就被判触发"
    action: str
    narration: str
    projection: str


@dataclass
class RunResult:
    index: int
    scene_id: str = ""
    branch_id: str = ""
    status: str = ""
    error: str = ""
    character_turns: int = 0
    environment_turns: int = 0
    #: 提到在场物件的动作各落在什么状态（resolved / failed / pending / skipped）
    object_actions: dict[str, int] = field(default_factory=dict)
    triggers: list[Trigger] = field(default_factory=list)
    private_reveals: int = 0
    story_progress: float | None = None
    recommended_decision: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_failures: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == SceneStatus.COMPLETED.value and not self.error


def summarize_scene(index: int, scene: Scene, evaluation: SceneEvaluation | None) -> RunResult:
    """从跑完（或中断）的场景里取出复测关心的数。纯函数，不读库。"""
    log = scene.dialogue_log
    by_id = {t.turn_id: t for t in log}
    result = RunResult(
        index=index,
        scene_id=scene.scene_id,
        branch_id=scene.branch_id,
        status=scene.status,
        character_turns=count_character_turns(log),
        environment_turns=sum(1 for t in log if not is_character_turn(t)),
        object_actions=dict(Counter(a.status for t in log for a in t.actions if a.object_id)),
    )
    for turn in log:
        if is_character_turn(turn):
            continue
        if turn.private_detail:
            result.private_reveals += 1
        if turn.projection:
            source = by_id.get(turn.source_turn_id)
            result.triggers.append(Trigger(
                turn_number=turn.turn_number,
                actor=source.character_name if source else "",
                action=(source.action or "") if source else "",
                narration=turn.narration or "",
                projection=turn.projection,
            ))
    for purpose, stat in scene.llm_usage.items():
        if purpose == LLMPurpose.EMBEDDING.value:
            continue  # 向量化另计价，混进来会让对话模型的开销看着翻倍
        result.prompt_tokens += stat.prompt_tokens
        result.completion_tokens += stat.completion_tokens
        result.llm_failures += stat.failures
    if evaluation is not None:
        result.story_progress = evaluation.story_progress
        result.recommended_decision = evaluation.recommended_decision
    return result


def render_report(results: list[RunResult], *, planned: int, header: dict[str, str]) -> str:
    done = [r for r in results if r.ok]
    triggered = [r for r in done if r.triggers]
    out = ["# 场景复测报告", ""]
    out += [f"- {k}：{v}" for k, v in header.items()]
    out += [
        f"- 进度：已跑 {len(results)} / {planned} 次，其中 {len(done)} 次跑完",
        f"- **出现公开投影：{len(triggered)} / {len(done)} 次**",
        f"- 有裁决失败的：{sum(1 for r in done if r.object_actions.get('failed'))} / {len(done)} 次",
        "",
        "| # | 状态 | 角色轮次 | 环境回合 | 公开投影 | 首次投影在第几轮 | 触发者 | 物件动作 | 推进度 | 输入 / 输出 token | 用时 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        first = r.triggers[0] if r.triggers else None
        actions = "，".join(f"{k} {v}" for k, v in sorted(r.object_actions.items())) or "—"
        progress = "—" if r.story_progress is None else f"{r.story_progress:.2f}"
        out.append(
            f"| {r.index} | {r.status or '未开跑'} | {r.character_turns} | {r.environment_turns} | {len(r.triggers)} "
            f"| {first.turn_number if first else '—'} | {first.actor if first else '—'} | {actions} | {progress} "
            f"| {r.prompt_tokens} / {r.completion_tokens} | {r.seconds / 60:.1f} 分 |"
        )
    out += ["", "## 每次触发的原文", "", "判定标准要比\"触发了\"严：对照动作原文与环境叙述，分清是真做了还是举着就被判触发。", ""]
    for r in results:
        out.append(f"### 第 {r.index} 次（场景 `{r.scene_id[:8]}`，对白见 `run-{r.index:02d}.md`）")
        if r.error:
            out.append(f"- 出错：{r.error}")
        if not r.triggers:
            out.append("- 没有公开投影" + (f"；私密揭示 {r.private_reveals} 次" if r.private_reveals else ""))
        for t in r.triggers:
            out += [
                f"- 第 {t.turn_number} 轮，{t.actor or '（来源轮次缺失）'}",
                f"  - 动作：{t.action}",
                f"  - 环境：{t.narration}",
                f"  - 幻象：{t.projection[:120]}{'…' if len(t.projection) > 120 else ''}",
            ]
        out.append("")
    return "\n".join(out)


def _write(out_dir: Path, results: list[RunResult], planned: int, header: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_report(results, planned=planned, header=header), encoding="utf-8")
    (out_dir / "results.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def _run_once(
    index: int, project_id: str, snapshot_id: str, label: str, speaker_mode: str, out_dir: Path
) -> RunResult:
    started = time.monotonic()
    _, scene = await orchestrator.fork_from_snapshot(
        project_id, snapshot_id, branch_name=f"复测 {label} #{index}",
        director_notes="scripts.rerun_scene 的无人值守复测，不是剧情分支",
    )
    if speaker_mode:
        scene.speaker_mode = speaker_mode
        await repository.save_scene(scene)
    error = ""
    try:
        await orchestrator.run_scene(scene.scene_id)
    except Exception as exc:  # noqa: BLE001  一次失败不能丢掉前面几次的结果
        error = f"{type(exc).__name__}: {exc}"
    scene = await repository.get_scene(scene.scene_id)
    evaluation = await repository.get_evaluation(scene.scene_id)
    result = summarize_scene(index, scene, evaluation)
    result.error = error
    result.seconds = time.monotonic() - started

    project = await repository.get_project(project_id)
    names = {c.character_id: c.name for c in await repository.list_characters(project_id)}
    object_names = {o.object_id: o.name for o in await repository.list_objects(project_id)}
    transcript = render_transcript(
        project, [scene], {scene.scene_id: evaluation} if evaluation else {}, names, object_names
    )
    (out_dir / f"run-{index:02d}.md").write_text(transcript, encoding="utf-8")
    return result


async def rerun(
    project_id: str, snapshot_id: str, times: int, out_dir: Path, *, speaker_mode: str = ""
) -> list[RunResult]:
    """串行复测 `times` 次，每次之后落一遍报告。返回全部结果（含失败的）。"""
    label = datetime.now().strftime("%m-%d %H:%M")
    header = {
        "项目": project_id,
        "快照": snapshot_id,
        "发言模式": speaker_mode or "沿用原场",
        "模型": f"角色 {settings.character_model}｜裁决 {settings.environment_model}｜选人 {settings.selector_model}",
        "环境档位": settings.ENVIRONMENT_MODE,
        "开始": datetime.now().isoformat(timespec="seconds"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[RunResult] = []
    failures = 0
    for index in range(1, times + 1):
        print(f"[{datetime.now():%H:%M:%S}] 第 {index} / {times} 次开跑", flush=True)
        try:
            result = await _run_once(index, project_id, snapshot_id, label, speaker_mode, out_dir)
        except Exception as exc:  # noqa: BLE001  分叉本身失败：没有场景可读
            result = RunResult(index=index, error=f"{type(exc).__name__}: {exc}")
        results.append(result)
        _write(out_dir, results, times, header)
        print(
            f"[{datetime.now():%H:%M:%S}] 第 {index} 次：{result.status or '未开跑'}，"
            f"公开投影 {len(result.triggers)} 次，{result.seconds / 60:.1f} 分"
            + (f"，出错：{result.error}" if result.error else ""),
            flush=True,
        )
        failures = 0 if result.ok else failures + 1
        if failures >= MAX_CONSECUTIVE_FAILURES:
            print(f"连续 {failures} 次没跑完，停止。先看 {out_dir / 'report.md'} 里的出错原因。", flush=True)
            break
    return results


def _backend_listening() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", settings.BACKEND_PORT)) == 0


async def _main(args: argparse.Namespace) -> int:
    if _backend_listening():
        print(
            f"后端正在 {settings.BACKEND_PORT} 端口运行。请先停掉它再复测：两个进程同时写同一个库，"
            "且后端重启时的对账会把正在跑的场景改成 paused。",
            file=sys.stderr,
        )
        return 2
    await init_db()
    out_dir = Path(args.out) if args.out else Path(settings.DATA_DIR) / "reruns" / datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        await repository.get_project(args.project)
    except PlotSystemError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    results = await rerun(args.project, args.snapshot, args.times, out_dir, speaker_mode=args.speaker_mode)
    done = [r for r in results if r.ok]
    print(f"跑完 {len(done)} / {args.times} 次，出现公开投影 {sum(1 for r in done if r.triggers)} 次。报告：{out_dir / 'report.md'}")
    return 0 if len(done) == args.times else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="从同一个快照反复分叉并跑完首场（调 LLM）")
    parser.add_argument("--project", required=True)
    parser.add_argument("--snapshot", required=True, help="要重跑的那一场的前置快照")
    parser.add_argument("--times", type=int, default=3)
    parser.add_argument("--out", default="", help="报告目录，缺省 data/reruns/时间戳")
    parser.add_argument(
        "--speaker-mode", default="", choices=["", *(m.value for m in SpeakerMode)],
        help="覆盖发言模式；缺省沿用原场",
    )
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
