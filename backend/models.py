"""核心数据模型。

跨 API 边界的数据（请求/响应）使用 Pydantic，
内部传递的数据使用 dataclass。本文件集中定义所有领域模型，
对应 CLAUDE.md 第 4 节。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def now() -> datetime:
    """返回带时区的当前时间。"""
    return datetime.now(UTC)


def new_id() -> str:
    """生成新的 UUID 字符串。"""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class ProjectStatus(str, Enum):
    INITIALIZING = "initializing"
    READY = "ready"
    SIMULATING = "simulating"


class SceneStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


class SpeakerMode(str, Enum):
    ROUND_ROBIN = "round_robin"
    SELECTOR = "selector"
    RANDOM = "random"


class DecisionType(str, Enum):
    CONTINUE = "continue"
    NEXT_SCENE = "next_scene"
    ROLLBACK = "rollback"


class OutputFormat(str, Enum):
    WEB_NOVEL = "web_novel"
    SCREENPLAY = "screenplay"
    STAGE_PLAY = "stage_play"
    SUMMARY_REPORT = "summary"
    RAW_LOG = "raw"


# ---------------------------------------------------------------------------
# 基础值对象
# ---------------------------------------------------------------------------


@dataclass
class RelationshipState:
    """两个角色之间的关系状态。"""

    target_character_id: str
    relation_type: str = "neutral"  # 如 friend / enemy / family / lover
    strength: float = 0.0  # -1.0 ~ 1.0
    notes: str = ""


@dataclass
class LoreEntry:
    """世界观条目（SillyTavern 风格的 lorebook 条目）。"""

    lore_id: str = field(default_factory=new_id)
    content: str = ""
    keywords: list[str] = field(default_factory=list)
    scope: str = "global"  # "global" | "character:{id}"
    priority: int = 5  # 1-10


# ---------------------------------------------------------------------------
# 实体（GraphRAG 提取结果）
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    """GraphRAG 提取出的实体。"""

    entity_id: str = field(default_factory=new_id)
    name: str = ""
    entity_type: str = "Concept"  # Character | Location | Event | Concept
    description: str = ""


@dataclass
class Relation:
    """实体间关系。"""

    source_id: str = ""
    target_id: str = ""
    relation_type: str = "RELATED_TO"
    description: str = ""
    strength: float = 0.5


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------


@dataclass
class CharacterCard:
    """角色卡。信息不对称的核心载体。"""

    character_id: str = field(default_factory=new_id)
    project_id: str = ""
    name: str = ""
    # Persona
    persona: str = ""
    appearance: str = ""
    speech_style: str = ""
    # 世界观注入
    world_lore_entries: list[LoreEntry] = field(default_factory=list)
    # 信息不对称（关键）
    known_facts: list[str] = field(default_factory=list)
    unknown_facts: list[str] = field(default_factory=list)  # 仅导演可见
    # 关系
    relationships: dict[str, RelationshipState] = field(default_factory=dict)
    # 当前状态
    current_emotion: str = "平静"
    current_goal: str = ""
    current_location: str = ""


@dataclass
class CharacterState:
    """角色在某一时刻的快照状态。"""

    character_id: str
    current_emotion: str = "平静"
    current_goal: str = ""
    current_location: str = ""
    relationships: dict[str, RelationshipState] = field(default_factory=dict)
    long_term_memory_snapshot: str = ""  # ChromaDB 集合序列化路径
    episodic_summary: str = ""
    short_term_buffer: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------


@dataclass
class Project:
    """推演项目。"""

    project_id: str = field(default_factory=new_id)
    name: str = ""
    description: str = ""
    seed_texts: list[str] = field(default_factory=list)
    status: str = ProjectStatus.INITIALIZING.value
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


# ---------------------------------------------------------------------------
# 对话与场景
# ---------------------------------------------------------------------------


@dataclass
class DialogueTurn:
    """单个对话轮次，按格式分类记录。"""

    turn_id: str = field(default_factory=new_id)
    scene_id: str = ""
    turn_number: int = 0
    character_id: str = ""
    character_name: str = ""
    dialogue: str | None = None
    action: str | None = None
    inner_thought: str | None = None
    timestamp: datetime = field(default_factory=now)
    memory_context_used: list[str] = field(default_factory=list)


@dataclass
class Scene:
    """场景。"""

    scene_id: str = field(default_factory=new_id)
    project_id: str = ""
    branch_id: str = ""
    parent_scene_id: str | None = None
    name: str = ""
    description: str = ""
    participating_characters: list[str] = field(default_factory=list)
    location: str = ""
    initial_conditions: dict = field(default_factory=dict)
    max_turns: int = 20
    status: str = SceneStatus.PENDING.value
    snapshot_id_before: str = ""
    snapshot_id_after: str | None = None
    turns_completed: int = 0
    dialogue_log: list[DialogueTurn] = field(default_factory=list)
    created_at: datetime = field(default_factory=now)


@dataclass
class SceneConfig:
    """导演规划场景时产生的配置。"""

    name: str = ""
    description: str = ""
    participating_characters: list[str] = field(default_factory=list)
    location: str = ""
    initial_conditions: dict = field(default_factory=dict)
    max_turns: int = 20
    speaker_mode: str = SpeakerMode.ROUND_ROBIN.value
    opening_narration: str = ""  # 导演提供的开场描述


@dataclass
class SceneResult:
    """场景执行结果。"""

    scene_id: str
    dialogue_log: list[DialogueTurn]
    snapshot_id_before: str
    snapshot_id_after: str
    turns_completed: int
    terminated_reason: str = ""


# ---------------------------------------------------------------------------
# 导演评估与决策
# ---------------------------------------------------------------------------


@dataclass
class SceneEvaluation:
    """导演对场景的评估结果。"""

    scene_id: str = ""
    synopsis: str = ""
    narrative_goal_score: float = 0.0  # 0-10
    dramatic_tension_score: float = 0.0  # 0-10
    plot_deviation_score: float = 0.0  # 0-10, 0=完全一致
    character_consistency_score: float = 0.0  # 0-10
    recommended_decision: str = DecisionType.NEXT_SCENE.value
    rollback_suggestion: dict | None = None


@dataclass
class DirectorDecision:
    """导演最终决策。"""

    decision_type: str = DecisionType.NEXT_SCENE.value
    extra_turns: int | None = None
    next_scene_config: SceneConfig | None = None
    rollback_to_snapshot_id: str | None = None
    new_initial_conditions: dict | None = None
    rollback_notes: str | None = None


# ---------------------------------------------------------------------------
# 快照与分支
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """场景模拟前/后的完整快照。"""

    snapshot_id: str = field(default_factory=new_id)
    scene_id: str = ""
    branch_id: str = ""
    label: str = ""
    created_at: datetime = field(default_factory=now)
    character_states: dict[str, CharacterState] = field(default_factory=dict)
    scene_context: dict = field(default_factory=dict)
    graph_checkpoint: str = ""
    chroma_checkpoint: str = ""


@dataclass
class Branch:
    """分支。"""

    branch_id: str = field(default_factory=new_id)
    project_id: str = ""
    parent_branch_id: str | None = None
    fork_from_snapshot_id: str | None = None
    fork_conditions: dict = field(default_factory=dict)
    name: str = ""
    scenes: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=now)
    director_notes: str = ""


@dataclass
class BranchTreeNode:
    """分支树节点（用于前端可视化）。"""

    branch: Branch
    children: list[BranchTreeNode] = field(default_factory=list)


@dataclass
class BranchTree:
    """项目完整分支树。"""

    project_id: str = ""
    roots: list[BranchTreeNode] = field(default_factory=list)


@dataclass
class MemoryChunk:
    """记忆检索返回的片段。"""

    text: str = ""
    score: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class MemorySnapshot:
    """记忆系统快照。"""

    character_id: str = ""
    short_term_buffer: list[str] = field(default_factory=list)
    episodic_summary: str = ""
    chroma_export_path: str = ""
