"""API 请求/响应 Pydantic 模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


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
    narrative_goal: str


class CreateSceneRequest(BaseModel):
    branch_id: str
    name: str
    description: str = ""
    participating_characters: list[str] = Field(default_factory=list)
    location: str = ""
    initial_conditions: dict = Field(default_factory=dict)
    max_turns: int = 12
    opening_narration: str = ""
    speaker_mode: str = "round_robin"


class DecisionRequest(BaseModel):
    decision_type: str  # continue | next_scene | rollback
    extra_turns: int | None = None
    next_scene_description: str | None = None
    rollback_snapshot_id: str | None = None
    new_initial_conditions: dict | None = None
    rollback_notes: str | None = None


class ForkBranchRequest(BaseModel):
    new_conditions: dict = Field(default_factory=dict)
    branch_name: str
    director_notes: str = ""


class OutputRequest(BaseModel):
    format: str  # web_novel | screenplay | stage_play | summary | raw
    branch_id: str | None = None
    scene_ids: list[str] = Field(default_factory=list)
