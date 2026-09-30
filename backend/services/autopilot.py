"""AutoPilot 会话的停止判定与记账（工单12）。

一层纯函数：不碰 IO、不调 LLM。IO 与调度在 `orchestrator` 里（契约8：跨模块编排
只在那里），这里只回答两个问题：这一场收场后还能不能自动往下走；导演给出的这个
决策能不能自动执行。

停止判定分两段，是因为回滚的上限必须在**执行之前**拦下 —— 回滚一执行就是一条新分支，
事后才发现超限，分支已经建出来了。决策本身来自 `DirectorAgent.make_decision` 的规则化
推荐（不调 LLM），所以能先算出来、判完再交给 `apply_decision`。
"""

from __future__ import annotations

from backend.agents.director_agent import is_evaluation_unavailable
from backend.models import (
    AutoPilotSession,
    AutoPilotStatus,
    AutoPilotStep,
    DecisionType,
    DirectorDecision,
    SceneEvaluation,
    SceneStatus,
    now,
)
from backend.scene_engine.termination import INTERRUPTED_REASON

# 停止原因。值进 API 与 SSE，前端据此给出提示，改名要同步 types/index.ts
MAX_STEPS = "max_steps"
ROLLBACK_LIMIT = "rollback_limit"
EVALUATION_UNAVAILABLE = "evaluation_unavailable"
ENDING_REACHED = "ending_reached"
INTERRUPTED = "interrupted"
SCENE_FAILED = "scene_failed"
DECISION_FAILED = "decision_failed"
HUMAN_TOOK_OVER = "human_took_over"
USER_STOPPED = "user_stopped"

STOP_MESSAGES: dict[str, str] = {
    MAX_STEPS: "已达到设定的自动推演步数",
    ROLLBACK_LIMIT: "导演连续建议回滚，已达上限；每次回滚都会新建分支，请人工判断",
    EVALUATION_UNAVAILABLE: "本场评估未生成或无法解析，无人值守时不按保守默认继续",
    ENDING_REACHED: "导演判定已抵达结局",
    INTERRUPTED: "场景被手动中断",
    SCENE_FAILED: "场景运行失败",
    DECISION_FAILED: "自动决策执行失败",
    HUMAN_TOOK_OVER: "这一场已有人工决策",
    USER_STOPPED: "已手动停止自动推演",
}

PHASE_RUNNING_SCENE = "running_scene"
PHASE_DECIDING = "deciding"


def is_running(session: AutoPilotSession | None) -> bool:
    return session is not None and session.status == AutoPilotStatus.RUNNING.value


def stop_reason_after_scene(
    session: AutoPilotSession,
    final_status: str,
    terminated_reason: str,
    evaluation: SceneEvaluation | None,
) -> str:
    """一场收场后是否该停。返回停止原因，空串表示可以进入决策。

    `evaluation` 为 None 表示评估或其后的世界变量/分镜稿补写失败 —— 那一步失败时
    评估可能已经落库，但分支状态没跟上，接着自动推演等于在半截状态上往下演。
    """
    if final_status != SceneStatus.COMPLETED.value:
        return SCENE_FAILED
    if terminated_reason == INTERRUPTED_REASON:
        return INTERRUPTED
    # make_decision 对不可用的评估会退回保守默认决策，人工值守时这是合理的兜底，
    # 无人值守时就是拿一份没有信息量的评估去建场景
    if evaluation is None or is_evaluation_unavailable(evaluation):
        return EVALUATION_UNAVAILABLE
    # 界面上结局只是提示、不锁按钮（用户可能不认同）；无人值守时没人来"不认同"
    if evaluation.is_ending_reached:
        return ENDING_REACHED
    if session.steps_taken >= session.max_steps:
        return MAX_STEPS
    return ""


def stop_reason_for_decision(session: AutoPilotSession, decision: DirectorDecision) -> str:
    """导演给出的决策能否自动执行。在 `apply_decision` 之前判定。"""
    if (
        decision.decision_type == DecisionType.ROLLBACK.value
        and session.consecutive_rollbacks >= session.max_consecutive_rollbacks
    ):
        return ROLLBACK_LIMIT
    return ""


def record_step(
    session: AutoPilotSession,
    scene_id: str,
    decision: DirectorDecision,
    next_branch_id: str,
) -> None:
    """记下一次已执行的自动决策。continue 同样算一步（它同样烧一轮 LLM）。"""
    session.steps_taken += 1
    if decision.decision_type == DecisionType.ROLLBACK.value:
        session.consecutive_rollbacks += 1
    else:
        session.consecutive_rollbacks = 0
    session.steps.append(
        AutoPilotStep(
            scene_id=scene_id,
            decision_type=decision.decision_type,
            next_scene_id=decision.next_scene_id or "",
            next_branch_id=next_branch_id,
        )
    )
    session.current_scene_id = decision.next_scene_id or scene_id
    session.updated_at = now()


def mark_stopped(session: AutoPilotSession, reason: str, detail: str = "") -> None:
    session.status = AutoPilotStatus.STOPPED.value
    session.phase = ""
    session.stop_reason = reason
    message = STOP_MESSAGES.get(reason, reason)
    session.stop_message = f"{message}：{detail}" if detail else message
    session.updated_at = now()
