"""核心数据模型。

跨 API 边界的数据（请求/响应）使用 Pydantic，
内部传递的数据使用 dataclass。本文件集中定义所有领域模型，
对应 CLAUDE.md 第 4 节。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def now() -> datetime:
    """返回带时区的当前时间。"""
    return datetime.now(UTC)


def as_aware(dt: datetime) -> datetime:
    """不带时区的时间按 UTC 补上时区。

    系统写下的时间恒带时区（`now()`），手工编辑的文件常写 `2026-01-01T00:00:00`。
    两者混在一起比较会抛 TypeError —— 一个手写文件就能让整个列表五百。
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def new_id() -> str:
    """生成新的 UUID 字符串。"""
    return str(uuid.uuid4())


#: 主线推进度的"不可用"哨兵。0-1 的正常值域里没有负数，
#: 因此它不会被误当成"进度为 0"参与钳制、停滞判定与前端展示。
PROGRESS_UNAVAILABLE = -1.0

#: 未收束线索的累积上限，超出会把导演上下文吃光（工单28）
MAX_UNRESOLVED_THREADS = 20

#: 世界变量的条数上限（工单07）。它比线索更需要设限：线索只进导演上下文，
#: 而世界变量进**每一场、每个角色、每一轮**的 system prompt，且只增不减。
#: token 预算常量不放这里 —— 它要用 utils.llm.estimate_tokens，而
#: utils.llm → config → models 已成链，反向 import 会成环（见 services/world_state.py）。
MAX_WORLD_VARIABLES = 30

#: 场景固有字段在 `scene_context` 里占用的键。`CharacterAgent._scene_brief` 把它们
#: 渲染成句子（"场景名 @ 地点"），其余键才逐条列进"当前情境"。
#: 对世界变量而言这些是**保留字**：同名的世界变量会顶掉本场的设定 ——
#: 场景设在城堡、世界里存着 `location=首都`，角色与导演读到的地点就是首都。
#: 世界变量只允许**补充**场景上下文，不允许改写场景是什么。
RESERVED_SCENE_CONTEXT_KEYS = frozenset(
    {"name", "location", "description", "opening_narration"}
)


def goal_revision(narrative_goal: str) -> str:
    """主线目标的版本指纹。

    推进度是"离这个目标还有多远"，换了目标就换了尺子。没有它的话，用户
    把目标改成完全不同的一个之后，旧目标下的 0.9 会把新目标的真实进度永久钳到顶。
    """
    return hashlib.sha256(narrative_goal.strip().encode("utf-8")).hexdigest()[:16]


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


class TurnKind(str, Enum):
    """轮次的种类（工单24/20）。环境回合由环境层裁决角色动作产生，不占发言顺序、
    不计入 max_turns；PR-0 只引入字段与计数口径，此时恒为 character。"""

    CHARACTER = "character"
    ENVIRONMENT = "environment"


class DecisionType(str, Enum):
    CONTINUE = "continue"
    NEXT_SCENE = "next_scene"
    ROLLBACK = "rollback"


class DecisionSource(str, Enum):
    """决策由谁做出（工单12）。只作留痕，不参与任何逻辑判断之外的分支。"""

    HUMAN = "human"
    AUTO = "auto"


class AutoPilotStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


class OutputFormat(str, Enum):
    WEB_NOVEL = "web_novel"
    SCREENPLAY = "screenplay"
    STAGE_PLAY = "stage_play"
    SUMMARY_REPORT = "summary"
    RAW_LOG = "raw"


class BeatStatus(str, Enum):
    """分镜稿节拍状态（工单18）。"""

    PLANNED = "planned"
    DONE = "done"
    DROPPED = "dropped"


class StoryboardSource(str, Enum):
    """分镜稿改动的来源，只用于 changelog 留痕。"""

    DIRECTOR = "director"
    USER = "user"
    FORK = "fork"


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


#: 物件可见性的三种取值；`private` 的知情者由 `WorldObject.known_by` 给出。
#: 不沿用 `LoreEntry.scope` 的 `character:{id}`：设定按知情者各发一份副本，物件是项目级的
#: 单个文件，"A、B 知道、C 不知道"只能存一份名单（设计单 A11）。非法或缺失一律按 hidden。
OBJECT_VISIBILITY_GLOBAL = "global"
OBJECT_VISIBILITY_PRIVATE = "private"
OBJECT_VISIBILITY_HIDDEN = "hidden"
OBJECT_VISIBILITIES = (OBJECT_VISIBILITY_GLOBAL, OBJECT_VISIBILITY_PRIVATE, OBJECT_VISIBILITY_HIDDEN)


@dataclass
class WorldObject:
    """场景里可被角色作用的物件（工单24）。

    物件是**被动规则**，不是角色：它不发言、没有意图，只在角色对它做动作时由环境层裁决。
    `hidden_rules`（触发条件、机关、真实功能）**只进导演与环境层**，不进任何角色、
    selector 打分或意图抽取的 prompt（契约1，设计单 R1）。

    项目级、存为 `objects/{object_id}.json`（同角色卡），支持人工编辑，因此读取侧必须
    压回预算（`services/objects.py::clamp_object`，同陷阱 19）。
    """

    object_id: str = field(default_factory=new_id)
    project_id: str = ""
    name: str = ""
    #: 预过滤靠 name 与别名做子串匹配。单字别名、与角色同名的别名会让每轮都命中，被丢弃
    aliases: list[str] = field(default_factory=list)
    #: 在场的人肉眼可见的部分；按 visibility 进角色视野（PR-2 起）
    public_description: str = ""
    hidden_rules: list[str] = field(default_factory=list)
    visibility: str = OBJECT_VISIBILITY_HIDDEN
    #: 知道它存在与外观的角色 id，仅 `private` 有意义；过滤后为空按 hidden（失败即收紧）
    known_by: list[str] = field(default_factory=list)
    #: 每次用户编辑 +1，PATCH 必须带回读取时的值（乐观并发）
    revision: int = 0
    #: 最近一次用户写入的幂等键与请求摘要（契约5）
    request_id: str = ""
    request_digest: str = ""
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


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


@dataclass
class CharacterInspection:
    """角色内部视图的统一读取结果（Inspection 层）。

    用户面板、导演智能体、总结智能体三方看的是同一份东西，共用本结构以免口径分叉。
    `unknown_facts` 仅在 include_private=True 时填充，绝不可进入角色可见上下文（契约1）。
    """

    character_id: str = ""
    name: str = ""
    persona: str = ""
    appearance: str = ""
    speech_style: str = ""
    current_emotion: str = "平静"
    current_goal: str = ""
    current_location: str = ""
    relationships: dict[str, RelationshipState] = field(default_factory=dict)
    known_facts: list[str] = field(default_factory=list)
    unknown_facts: list[str] = field(default_factory=list)
    world_lore_entries: list[LoreEntry] = field(default_factory=list)
    short_term_buffer: list[str] = field(default_factory=list)
    episodic_summary: str = ""
    long_term_hits: list[MemoryChunk] = field(default_factory=list)
    # 状态取自哪个快照；为空表示无快照可用、已退回角色卡当前值
    source_snapshot_id: str = ""
    state_source: str = "card"  # "snapshot" | "card"


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
    # 主线目标：导演的只读锚点。只有用户能改（工单28）——它既是评分基准，
    # 又允许被评方改写的话，导演会把目标改成自己刚演出来的东西，度量归零。
    narrative_goal: str = ""
    ending_criteria: str = ""  # 可选：结局判定标准（自然语言）
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass
class WorldState:
    """分支维度的全局世界变量（工单07）。

    信息不对称保证"角色不该知道的不知道"，世界状态负责"该传播的能传播"：
    季节、某势力的公开态度、某公开事件是否已发生这类**跨场次持续演变**的世界层事实。

    **它是公共可见层**：变量会进入本场全部在场角色的 system prompt，
    因此只允许存放所有角色都可感知的公开事实（契约1）。
    作用域是分支级（与长期记忆同级），两条 IF 线各自演化、互不污染。
    """

    project_id: str = ""
    branch_id: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=now)


@dataclass
class StoryBeat:
    """路线图上的一个节拍（工单18）。

    `beat_id` 是节拍的身份：形如 `b7`、分支内唯一、只由后端分配，改名/重排/改状态
    都不改变它。导演的 patch 按它定位 —— 按标题或下标定位的话，用户改一次名、插一条
    节拍，导演的"标记完成"就会落到别的节拍上，而且不报错。ID 会进 prompt，所以要短。
    """

    beat_id: str = ""
    title: str = ""
    description: str = ""
    status: str = BeatStatus.PLANNED.value
    # 在哪一场完成或放弃；只供溯源与前端展示，不进 prompt（uuid 白吃预算）
    resolved_scene_id: str = ""


@dataclass
class ForkOrigin:
    """本分支从哪里分叉、改了什么（工单18 §3.5）。分叉时按确定性模板生成，不调 LLM。"""

    source_branch_id: str = ""
    source_branch_name: str = ""
    source_snapshot_id: str = ""
    source_snapshot_label: str = ""
    conditions: dict[str, str] = field(default_factory=dict)
    director_notes: str = ""


@dataclass
class StoryboardChange:
    """分镜稿的一条改动留痕。限条数，不进 prompt。"""

    source: str = StoryboardSource.DIRECTOR.value
    scene_id: str = ""
    summary: str = ""
    at: datetime = field(default_factory=now)
    # 用户编辑的幂等键与请求摘要（契约5）。响应丢失时客户端拿不到新节拍的 ID，
    # 只能原样重发，"内容与当前相同"判不出这种重放，只能靠它。导演与分叉的条目留空
    request_id: str = ""
    request_digest: str = ""


@dataclass
class Storyboard:
    """分支级导演分镜稿（工单18）：路线图 + 长期备忘 + 分叉说明。

    三层目标模型里的第 2 层：主线目标（第 1 层）只读、只有用户能改；本场意图
    （第 3 层）一次性；分镜稿是导演唯一的**持久可写空间**，记录"这条分支打算
    怎么走到目标"。

    **只进导演 prompt**（红线 R1）：它含导演对全部角色 `unknown_facts` 的安排，
    进了任何角色或 selector 的上下文，角色就"知道剧本"了（契约1）。
    存放照搬 `WorldState`：权威值是 `storyboard/{branch_id}.json`，快照带时点副本。
    """

    project_id: str = ""
    branch_id: str = ""
    outline: list[StoryBeat] = field(default_factory=list)
    memo: str = ""
    # 这份路线图对照的是哪个版本的主线目标（models.goal_revision）。写回必须有确认，
    # 见 services/storyboard.py；与当前目标不一致时 prompt 标注"基于旧版主线目标"
    goal_revision: str = ""
    fork_origin: ForkOrigin | None = None
    changelog: list[StoryboardChange] = field(default_factory=list)
    # 单调递增的修订号：导演合并与用户写入都推进，用作 PUT 的并发版本。
    # 不用 updated_at —— 精度与时钟都不可靠
    revision: int = 0
    # 下一个待分配的节拍序号。删除后的 ID 不复用：否则一条基于旧稿的 patch
    # 会命中同名的新节拍
    next_beat_seq: int = 1
    updated_at: datetime = field(default_factory=now)


@dataclass
class StoryboardPatch:
    """导演随评估产出的分镜稿修改（工单18 §3.4）。

    所有操作都是**相对于导演读到的那一版**给出的，合并时逐条校验前提仍成立
    （`services.storyboard.merge_storyboard_patch`）。字段全空 = 本场不改分镜稿。
    """

    add: list[StoryBeat] = field(default_factory=list)  # 只取 title / description
    complete: list[str] = field(default_factory=list)  # beat_id
    drop: list[str] = field(default_factory=list)  # beat_id
    update: list[StoryBeat] = field(default_factory=list)  # 按 beat_id 改写标题/说明
    # 全部 planned 节拍 ID 的新顺序；None = 不重排
    reorder: list[str] | None = None
    # 新的完整备忘；None = 不改
    memo: str | None = None
    # 导演显式确认"已按当前主线目标重排"。只有它能让 goal_revision 前进
    goal_realigned: bool = False
    # 解析器因格式无效丢弃的操作（带原因）。合并时计为被跳过、并挡住本次目标确认：
    # 静默丢掉的话，一个只剩 goal_realigned 的 patch 就像"没改路线图、确认已适配"
    rejected: list[str] = field(default_factory=list)


@dataclass
class StoryboardMerge:
    """`services.storyboard.merge_storyboard_patch` 的结果（运行时，不落库）。"""

    storyboard: Storyboard
    applied: list[str] = field(default_factory=list)
    # 前提在当前稿上已不成立、或导演给了未知 ID 而被跳过的操作（带原因）
    skipped: list[str] = field(default_factory=list)
    # 超出预算被淘汰的节拍
    evicted: list[str] = field(default_factory=list)


@dataclass
class StoryboardView:
    """分镜稿 + 用户确认"已按当前目标重排"所需的目标上下文（运行时组装，不落库）。

    目标原文必须随分镜稿一起返回：确认框旁显示的得是**这次响应里**的目标，
    PUT 带回的 `goal_revision_seen` 才对得上用户实际看到的那一版。
    """

    storyboard: Storyboard
    narrative_goal: str = ""
    goal_revision: str = ""  # 当前主线目标的版本
    goal_stale: bool = False


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
    # selector 选人降级时的短提示，供前端在角色名后灰字展示；正常为空串
    selector_notice: str = ""
    # 计数一律走 utils/turns 的 character_turns，不要直接 len(dialogue_log)
    kind: str = TurnKind.CHARACTER.value


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
    #: 本场在场物件的 ID（工单24）。只是候选名单：隐藏规则不随它进任何角色上下文。
    #: 新建场景的每条路径都要搬运它（同陷阱 3 / 13），漏一条就是"那一场物件凭空消失"
    objects_present: list[str] = field(default_factory=list)
    initial_conditions: dict = field(default_factory=dict)
    max_turns: int = 20
    status: str = SceneStatus.PENDING.value
    snapshot_id_before: str = ""
    snapshot_id_after: str | None = None
    # 回滚重演场景的来源快照 ID（工協14）。与 snapshot_id_before 分开：
    # 后者为空才能让 SceneEngine 为重演重新打快照，而这里需要记住
    # "运行时记忆（短期缓冲/事件摘要）应从哪个快照回填"。
    restore_snapshot_id: str = ""
    # None = 旧数据尚未冻结；[] = 权威的空历史。只供导演，不进入角色上下文。
    inherited_story_history: list[StoryRecord] | None = None
    turns_completed: int = 0
    # 已固化进长期记忆的轮次数（水位线）。固化只发生在场景正常结束时，
    # 崩溃/异常中断后续跑要靠它区分“哪些已落盘的轮次还没进过记忆”。
    turns_consolidated: int = 0
    speaker_mode: str = SpeakerMode.ROUND_ROBIN.value
    dialogue_log: list[DialogueTurn] = field(default_factory=list)
    created_at: datetime = field(default_factory=now)


@dataclass
class SceneLineage:
    """回溯导演历史所需的场景字段投影（工单18 D2）。

    谱系回溯只看因果链与冻结副本，连带反序列化每一场的完整 `dialogue_log` 是纯开销。
    **只读投影，不可存回**：它缺字段，拿去 `save_scene` 会把整场对白抹掉，
    所以刻意做成独立类型而不是"半填的 Scene"。
    """

    scene_id: str = ""
    branch_id: str = ""
    parent_scene_id: str | None = None
    name: str = ""
    restore_snapshot_id: str = ""
    inherited_story_history: list[StoryRecord] | None = None


@dataclass
class SceneConfig:
    """导演规划场景时产生的配置。"""

    name: str = ""
    description: str = ""
    participating_characters: list[str] = field(default_factory=list)
    location: str = ""
    objects_present: list[str] = field(default_factory=list)
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
    # --- 主线度量（工单28）。负值 = 不可用，绝不能当成 0 参与比较 ---
    story_progress: float = PROGRESS_UNAVAILABLE  # 0-1，沿因果谱系单调钳制后的值
    story_progress_raw: float = PROGRESS_UNAVAILABLE  # 导演本场原始自评，仅供观测
    progress_stalled: bool = False  # 本场自评未超过历史最高值
    # 本场推进度是对照哪个版本的主线目标给出的（空 = 旧记录，不参与钳制）
    goal_revision: str = ""
    # 评估时项目没有主线目标：目标达成 / 主线偏离没有参照，推进度不度量，
    # 目标达成分也不进决策阈值。老记录按 goal_revision 是否为空目标的版本回填
    goal_missing: bool = False
    is_ending_reached: bool = False
    ending_reason: str = ""
    unresolved_threads: list[str] = field(default_factory=list)
    # 本场对世界变量的增量修改（工单07）。值为 None 表示"该变量不再成立，删除它"，
    # 因此反序列化必须保留 None，不能当成空串。空 dict = 本场没有改变世界层事实。
    world_state_delta: dict[str, str | None] = field(default_factory=dict)
    # 本场评估是针对哪个结束态快照给出的。`evaluations` 以 scene_id 为主键且
    # INSERT OR REPLACE，一场只留最新一份：continue 续跑会覆盖掉旧评估。
    # 归属戳随历史副本保留用于追溯；分叉实际继承快照的历史副本（空 = 旧记录）。
    evaluated_snapshot_id: str = ""
    # 本场对分镜稿的修改（工单18）。解析失败时必须为空，与 world_state_delta 同理。
    # 导演历史副本（StoryRecord）里会清掉它：副本只供回溯梗概/进度/线索
    storyboard_patch: StoryboardPatch = field(default_factory=StoryboardPatch)

@dataclass
class StoryRecord:
    """导演历史里的一条记录：某一场及其评估（工单18 D1）。

    `Scene.inherited_story_history` 与 `Snapshot.story_history` 的元素类型。
    字段名曾只以字符串字面量存在，改一处键名就会让推进度静默退回不可用。
    """

    scene_id: str = ""
    name: str = ""
    evaluation: SceneEvaluation = field(default_factory=SceneEvaluation)
    # 这条记录是否带有权威的线索状态。旧副本里 unresolved_threads 缺键或不是列表时
    # 为 False，回溯要继续往前找；显式 [] 表示"线索已清空"，为 True。
    # SceneEvaluation 的默认值就是 []，不单独记这一位，类型化会把"缺键"压成
    # "已清空"，让更早的未收束线索从导演视野里消失（§4.2 陷阱 17）。
    threads_known: bool = True


@dataclass
class DirectorDecision:
    """导演最终决策。"""

    decision_type: str = DecisionType.NEXT_SCENE.value
    extra_turns: int | None = None
    next_scene_config: SceneConfig | None = None
    next_scene_id: str | None = None          # apply_decision 后填入：新场景或继续场景的 ID
    rollback_to_snapshot_id: str | None = None
    new_initial_conditions: dict | None = None
    # --- next_scene 分支的人工可编辑覆盖字段（均为 None 时保持 AI 自动规划行为）---
    next_scene_description: str | None = None
    next_participating_characters: list[str] | None = None
    next_location: str | None = None
    next_initial_conditions: dict | None = None
    next_objects_present: list[str] | None = None
    rollback_notes: str | None = None
    # 人工提交 / AutoPilot 自动执行（工单12）。旧记录没有这个键，按人工处理
    source: str = DecisionSource.HUMAN.value


@dataclass
class AutoPilotStep:
    """AutoPilot 执行过的一次自动决策。"""

    scene_id: str = ""
    decision_type: str = ""
    next_scene_id: str = ""
    next_branch_id: str = ""
    at: datetime = field(default_factory=now)


@dataclass
class AutoPilotSession:
    """一次自动推演会话（工单12）。**只存在于进程内存，不落库。**

    用户对某一场开启，按导演的规则化决策连续推进，触发任一停止条件即交还人工。
    不落库是刻意的：进程重启后场景会被对账成 paused（6.4），若会话还在，
    重启就等于无人值守地继续烧 LLM；而写进 Project 又会与用户编辑主线目标
    的整份覆盖写互相抹掉（PATCH 是读-改-写）。与 `_active_scenes` 同属契约9。
    """

    session_id: str = field(default_factory=new_id)
    project_id: str = ""
    # 幂等键（契约5）：同键重放返回同一个会话，不重复开启
    request_id: str = ""
    max_steps: int = 5
    max_consecutive_rollbacks: int = 2
    status: str = AutoPilotStatus.RUNNING.value
    # running 时的细分：running_scene（等这一场跑完）/ deciding（导演决策中）
    phase: str = ""
    current_scene_id: str = ""
    steps_taken: int = 0
    # 连续自动回滚次数：回滚每次都会新建分支，不设限时评分长期偏低会一路长出分支。
    # 不能按"回滚到同一快照"判断 —— 重演场景开跑会重打前置快照，目标 ID 每次都不同
    consecutive_rollbacks: int = 0
    stop_reason: str = ""
    stop_message: str = ""
    steps: list[AutoPilotStep] = field(default_factory=list)
    started_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


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
    # 该时点的世界变量副本（工单07）。分叉据此让新分支继承世界状态 ——
    # 世界状态是分支级文件，不随快照目录走，不冻结进来就会"一分叉世界重置"。
    world_state_variables: dict[str, str] = field(default_factory=dict)
    # 时点化的导演评估副本。不能通过 scene_id 回读后来被 continue 覆盖的评估。
    story_history: list[StoryRecord] | None = None
    # 时点化的分镜稿副本（工单18）。None = 本功能上线前的旧快照，分叉时以空稿起步，
    # 绝不回读来源分支的当前分镜稿（那是分叉之后才写的，会越过继承边界）
    storyboard: Storyboard | None = None


@dataclass
class Branch:
    """分支。"""

    branch_id: str = field(default_factory=new_id)
    project_id: str = ""
    parent_branch_id: str | None = None
    fork_from_snapshot_id: str | None = None
    # 溯源元数据：权威值在首场 Scene 的 initial_conditions（工单08 I5），此处仅供分支树展示
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
    short_term_meta: list[dict] = field(default_factory=list)
    episodic_summary: str = ""
    chroma_export_path: str = ""
