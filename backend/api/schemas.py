"""API 请求/响应 Pydantic 模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.models import BeatStatus, SpeakerMode

_SPEAKER_MODES = {m.value for m in SpeakerMode}
_BEAT_STATUSES = {s.value for s in BeatStatus}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ApiResponse(BaseModel):
    """通用响应包装。"""

    success: bool = True
    data: Any = None
    error: str | None = None
    timestamp: str = Field(default_factory=_now_iso)

    @classmethod
    def ok(cls, data: Any = None) -> ApiResponse:
        return cls(success=True, data=data, error=None)

    @classmethod
    def fail(cls, error: str) -> ApiResponse:
        return cls(success=False, data=None, error=error)


# ---- 请求体 ----


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""
    narrative_goal: str = ""
    ending_criteria: str = ""


class UpdateProjectRequest(BaseModel):
    """项目属性的人工编辑。None = 不改该字段。

    主线目标只能从这里改（工单28）：建项目时种子文本还没上传、角色还没抽出来，
    目标往往写不准；但导演侧的任何路径都不得写回它。
    """

    name: str | None = None
    description: str | None = None
    narrative_goal: str | None = None
    ending_criteria: str | None = None


class UpdateCharacterRequest(BaseModel):
    persona: str | None = None
    appearance: str | None = None
    speech_style: str | None = None
    known_facts: list[str] | None = None
    unknown_facts: list[str] | None = None
    current_emotion: str | None = None
    current_goal: str | None = None
    current_location: str | None = None


class PlanSceneRequest(BaseModel):
    branch_id: str
    # 本场意图（第三层）。主线目标固定读 project.narrative_goal，不从请求体进来，
    # 否则锚点会被逐场传入的临时目标架空（工单28）。
    scene_intent: str = ""


class CreateSceneRequest(BaseModel):
    branch_id: str
    name: str
    description: str = ""
    participating_characters: list[str] = Field(default_factory=list)
    location: str = ""
    initial_conditions: dict = Field(default_factory=dict)
    max_turns: int = 12
    opening_narration: str = ""
    # 留空则采用 settings.DEFAULT_SPEAKER_MODE（.env 配置）
    speaker_mode: str = ""

    @field_validator("speaker_mode")
    @classmethod
    def _validate_speaker_mode(cls, v: str) -> str:
        # 非法值若放行会在引擎里静默退化成 round_robin，用户无从察觉
        if v and v not in _SPEAKER_MODES:
            raise ValueError(f"speaker_mode 必须是 {sorted(_SPEAKER_MODES)} 之一，收到 {v!r}")
        return v


class DecisionRequest(BaseModel):
    decision_type: str  # continue | next_scene | rollback
    extra_turns: int | None = None
    next_scene_description: str | None = None
    rollback_snapshot_id: str | None = None
    new_initial_conditions: dict | None = None
    rollback_notes: str | None = None
    # --- next_scene 分支的人工可编辑覆盖字段，均为 None/空时保持现有“AI 自动决定”行为 ---
    next_participating_characters: list[str] | None = None
    next_location: str | None = None
    next_initial_conditions: dict | None = None


class StartAutoPilotRequest(BaseModel):
    """开启自动推演（工单12）。上下限按配置在业务层校验（422）。"""

    scene_id: str
    # None = 取 .env 里的默认值
    max_steps: int | None = None
    max_consecutive_rollbacks: int | None = None
    # 幂等键（契约5）：网络重放不能开出第二个会话、也不能重复开演
    request_id: str = Field(..., min_length=1, max_length=64)

class ForkBranchRequest(BaseModel):
    new_conditions: dict = Field(default_factory=dict)
    branch_name: str
    director_notes: str = ""


class StoryBeatInput(BaseModel):
    """用户编辑的一个节拍。已有节拍必须原样带回 beat_id，新节拍留空由后端分配。"""

    beat_id: str = ""
    title: str
    description: str = ""
    status: str = BeatStatus.PLANNED.value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        if v not in _BEAT_STATUSES:
            raise ValueError(f"status 必须是 {sorted(_BEAT_STATUSES)} 之一，收到 {v!r}")
        return v


class UpdateStoryboardRequest(BaseModel):
    """用户整份替换分镜稿的路线图与备忘（工单18 §3.6）。

    `revision` 是读取时拿到的修订号，不匹配返回 409。goal_revision / fork_origin /
    changelog 由后端维护，这里刻意没有这些字段。
    """

    outline: list[StoryBeatInput] = Field(default_factory=list)
    memo: str = ""
    revision: int
    # 显式确认"已按当前主线目标重排"，不带则不动 goal_revision
    confirm_goal: bool = False
    # 读取时响应里的 current_goal_revision；confirm_goal 时必填，后端写回的就是它
    goal_revision_seen: str = Field("", max_length=64)
    # 幂等键（契约5）：同一份内容的重试沿用同一个，换了内容就换新的
    request_id: str = Field("", max_length=64)


class OutputRequest(BaseModel):
    format: str  # web_novel | screenplay | stage_play | summary | raw
    branch_id: str | None = None
    scene_ids: list[str] = Field(default_factory=list)
