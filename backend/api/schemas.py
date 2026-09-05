"""API 请求/响应 Pydantic 模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.models import SpeakerMode

_SPEAKER_MODES = {m.value for m in SpeakerMode}


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


class ForkBranchRequest(BaseModel):
    new_conditions: dict = Field(default_factory=dict)
    branch_name: str
    director_notes: str = ""


class OutputRequest(BaseModel):
    format: str  # web_novel | screenplay | stage_play | summary | raw
    branch_id: str | None = None
    scene_ids: list[str] = Field(default_factory=list)
