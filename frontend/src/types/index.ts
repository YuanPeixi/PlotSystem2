// 前端 TypeScript 类型定义（对应后端模型）

export interface ApiResponse<T = unknown> {
  success: boolean
  data: T
  error: string | null
  timestamp: string
}

export interface Project {
  project_id: string
  name: string
  description: string
  seed_texts: string[]
  status: string
  /** 主线目标：导演的只读锚点，只能由用户修改 */
  narrative_goal: string
  ending_criteria: string
}

export interface RelationshipState {
  target_character_id: string
  relation_type: string
  strength: number
  notes: string
}

export interface LoreEntry {
  lore_id: string
  content: string
  keywords: string[]
  scope: string
  priority: number
}

export interface CharacterCard {
  character_id: string
  project_id: string
  name: string
  persona: string
  appearance: string
  speech_style: string
  world_lore_entries: LoreEntry[]
  known_facts: string[]
  unknown_facts: string[]
  relationships: Record<string, RelationshipState>
  current_emotion: string
  current_goal: string
  current_location: string
}

export interface MemoryChunk {
  text: string
  score: number
  metadata: Record<string, unknown>
}

/** Inspection 层返回的角色内部视图（导演视角）。 */
export interface CharacterInspection {
  character_id: string
  name: string
  persona: string
  appearance: string
  speech_style: string
  current_emotion: string
  current_goal: string
  current_location: string
  relationships: Record<string, RelationshipState>
  known_facts: string[]
  unknown_facts: string[]
  world_lore_entries: LoreEntry[]
  short_term_buffer: string[]
  episodic_summary: string
  long_term_hits: MemoryChunk[]
  source_snapshot_id: string
  state_source: 'snapshot' | 'card'
}

export interface DialogueTurn {
  turn_id: string
  scene_id: string
  turn_number: number
  character_id: string
  character_name: string
  dialogue: string | null
  action: string | null
  inner_thought: string | null
  selector_notice?: string
  /** 回复被截掉续写时的说明（工单30）：`标签：详情`，冒号前作角色名后的黄字，整句作悬停说明 */
  output_notice?: string
  // 环境回合（工单24/20）不占发言顺序、不计入 max_turns；旧数据缺省即 character
  kind?: 'character' | 'environment'
  /** 每个 *动作* 段的意图（工单24，record / adjudicate 档才有）。只给导演/用户看，不进角色上下文 */
  actions?: ActionIntent[]
  /** 环境回合的公开叙述（进"目前对话"） */
  narration?: string | null
  /** 环境回合的私密细节：只给导演/用户与 perceived_by 的记忆，不进"目前对话" */
  private_detail?: string | null
  perceived_by?: string[]
  /** 环境回合的来源：哪个角色轮次的第几个动作段 */
  source_turn_id?: string
  source_action_index?: number
  /** 服务端每改写一次已落盘的轮次就 +1；SSE turn_update 按它判新旧 */
  revision?: number
}

export interface ActionIntent {
  index: number
  text: string
  object_id: string
  verb: string
  detail: string
  status: 'recorded' | 'pending' | 'resolved' | 'failed' | 'skipped'
  skip_reason: '' | 'no_object' | 'over_limit' | 'not_attempt' | 'invalid_object' | 'extract_failed' | 'quota'
}

export interface Scene {
  scene_id: string
  project_id: string
  branch_id: string
  parent_scene_id: string | null
  name: string
  description: string
  participating_characters: string[]
  location: string
  /** 本场在场物件的 ID（工单24） */
  objects_present: string[]
  initial_conditions: Record<string, unknown>
  max_turns: number
  status: string
  snapshot_id_before: string
  snapshot_id_after: string | null
  restore_snapshot_id: string
  inherited_story_history?: StoryRecord[] | null
  turns_completed: number
  speaker_mode: string
  dialogue_log: DialogueTurn[]
  created_at?: string
  /** 本场运行期间的 LLM 调用计数，按用途分（工单25）；评估的调用在 SceneEvaluation 上 */
  llm_usage?: Record<string, LLMUsageStat>
  /** 场景内的物件公开状态（工单20）：物件 ID → { 属性: 值 }；null = 本场清除了该属性 */
  environment_state?: Record<string, Record<string, string | null>>
}

/** 某一用途的 LLM 调用计数（工单25）。只增观测。 */
export interface LLMUsageStat {
  calls: number
  failures: number
  retries: number
  prompt_tokens: number
  completion_tokens: number
  /** token 为估算值的调用数（服务商没返回 usage） */
  estimated_calls: number
  seconds: number
}

/** 导演历史的一条记录（仅导演可见，不进入角色上下文）。 */
export interface StoryRecord {
  scene_id: string
  name: string
  evaluation: SceneEvaluation
  /** false = 旧副本里线索缺失/损坏，回溯时会继续往前找；[] 的线索配 true 表示已清空 */
  threads_known: boolean
}

/** 快照元信息（GET /projects/{id}/snapshots 的列表项，不含角色状态明细与导演历史）。 */
export interface SnapshotMeta {
  snapshot_id: string
  scene_id: string
  branch_id: string
  label: string
  created_at: string
  character_count: number
}

export interface SceneConfig {
  name: string
  description: string
  participating_characters: string[]
  location: string
  objects_present: string[]
  initial_conditions: Record<string, unknown>
  max_turns: number
  speaker_mode: string
  opening_narration: string
}

/**
 * 项目级物件（工单24）。hidden_rules 只给导演与用户看，绝不进角色上下文。
 * private 时 known_by 是知道它存在与外观的角色 id。
 */
export interface WorldObject {
  object_id: string
  project_id: string
  name: string
  aliases: string[]
  public_description: string
  hidden_rules: string[]
  visibility: 'global' | 'private' | 'hidden'
  known_by: string[]
  revision: number
  created_at?: string
  updated_at?: string
}

/** 物件的可编辑字段。预算由后端校验，超限 422 不截断。 */
export type WorldObjectFields = Pick<
  WorldObject,
  'name' | 'aliases' | 'public_description' | 'hidden_rules' | 'visibility' | 'known_by'
>

/** 新建物件：request_id 是幂等键，物件 ID 由它确定性生成。 */
export interface WorldObjectCreate extends WorldObjectFields {
  request_id: string
}

/** 修改物件：revision 是读取时的修订号，不匹配 409；request_id 命中视为重放。 */
export interface WorldObjectUpdate extends WorldObjectFields {
  revision: number
  request_id: string
}

export interface SceneEvaluation {
  scene_id: string
  synopsis: string
  narrative_goal_score: number
  dramatic_tension_score: number
  plot_deviation_score: number
  character_consistency_score: number
  recommended_decision: string
  rollback_suggestion: Record<string, unknown> | null
  /** 0-1 主线推进度；负值 = 本场未度量到，不是“进度为 0” */
  story_progress: number
  story_progress_raw: number
  progress_stalled: boolean
  /** 评估时项目没有主线目标：目标达成 / 主线偏离没有参照，推进度不度量 */
  goal_missing?: boolean
  is_ending_reached: boolean
  ending_reason: string
  unresolved_threads: string[]
  /** 本场对分支世界变量的增量修改；值为 null 表示该变量已失效被删除 */
  world_state_delta: Record<string, string | null>
  /** 本场评估对应的结束态快照；分叉时用来识别被续跑覆盖的旧评估（空 = 旧记录） */
  evaluated_snapshot_id: string
  /** 本场导演对分镜稿的修改（已由后端逐条校验后合并；这里仅供追溯） */
  storyboard_patch?: StoryboardPatch
  /** 产出这份评估花掉的 LLM 调用（工单25） */
  llm_usage?: Record<string, LLMUsageStat>
}

export type BeatStatus = 'planned' | 'done' | 'dropped'

/** 路线图上的一个节拍。beat_id 是身份：改名、重排、改状态都不变，只由后端分配。 */
export interface StoryBeat {
  beat_id: string
  title: string
  description: string
  status: BeatStatus
  /** 在哪一场完成或放弃 */
  resolved_scene_id: string
}

export interface ForkOrigin {
  source_branch_id: string
  source_branch_name: string
  source_snapshot_id: string
  source_snapshot_label: string
  conditions: Record<string, string>
  director_notes: string
}

export interface StoryboardChange {
  source: 'director' | 'user' | 'fork'
  scene_id: string
  summary: string
  at: string
  /** 用户编辑的幂等键（导演与分叉条目为空） */
  request_id?: string
  request_digest?: string
}

export interface StoryboardPatch {
  add: Array<Pick<StoryBeat, 'title' | 'description'>>
  complete: string[]
  drop: string[]
  update: Array<Pick<StoryBeat, 'beat_id' | 'title' | 'description'>>
  reorder: string[] | null
  memo: string | null
  goal_realigned: boolean
  /** 解析时因格式无效被丢弃的操作（带原因）；非空时本次目标确认不成立 */
  rejected: string[]
}

/** 分支级导演分镜稿（GET /projects/{pid}/branches/{bid}/storyboard）。仅导演/用户可见。 */
export interface Storyboard {
  project_id: string
  branch_id: string
  outline: StoryBeat[]
  memo: string
  goal_revision: string
  fork_origin: ForkOrigin | null
  changelog: StoryboardChange[]
  /** 并发版本：PUT 时原样带回，不匹配返回 409 */
  revision: number
  next_beat_seq: number
  updated_at: string
  /** 路线图基于旧版主线目标（后端按当前目标算出） */
  goal_stale: boolean
  /** 这次响应时的主线目标原文与版本：确认"已按当前目标重排"时显示前者、带回后者 */
  narrative_goal: string
  current_goal_revision: string
}

/** PUT 分镜稿的请求体。已有节拍带回 beat_id，新节拍不带。 */
export interface StoryboardUpdate {
  outline: Array<{ beat_id?: string; title: string; description: string; status: BeatStatus }>
  memo: string
  revision: number
  confirm_goal: boolean
  /** confirm_goal 时必填：用户看到的目标版本（读取响应里的 current_goal_revision） */
  goal_revision_seen: string
  /** 幂等键：同一份内容的重试沿用同一个，内容改过就换新的 */
  request_id: string
}

/** 分支级世界变量（GET /projects/{pid}/branches/{bid}/world-state）。 */
export interface WorldState {
  project_id: string
  branch_id: string
  variables: Record<string, string>
  updated_at: string
}

export interface Branch {
  branch_id: string
  project_id: string
  parent_branch_id: string | null
  fork_from_snapshot_id: string | null
  name: string
  scenes: string[]
  director_notes: string
}

export interface BranchTreeNode {
  branch: Branch
  children: BranchTreeNode[]
}

export interface BranchTree {
  project_id: string
  roots: BranchTreeNode[]
}

/** POST /snapshots/{id}/fork 的返回体：新分支及其首场 pending 场景（不自动开跑）。 */
export interface ForkResult {
  branch: Branch
  scene: Scene
}

/** AutoPilot 执行过的一次自动决策（工单12）。 */
export interface AutoPilotStep {
  scene_id: string
  decision_type: 'continue' | 'next_scene' | 'rollback'
  next_scene_id: string
  next_branch_id: string
  at: string
}

/** 停止原因，与 backend/services/autopilot.py 的常量一一对应。 */
export type AutoPilotStopReason =
  | 'max_steps'
  | 'rollback_limit'
  | 'evaluation_unavailable'
  | 'ending_reached'
  | 'interrupted'
  | 'scene_failed'
  | 'decision_failed'
  | 'human_took_over'
  | 'user_stopped'

/** 一次自动推演会话。只在后端进程内存里，重启即消失（GET 返回 null）。 */
export interface AutoPilotSession {
  session_id: string
  project_id: string
  request_id: string
  max_steps: number
  max_consecutive_rollbacks: number
  status: 'running' | 'stopped'
  /** running 时：running_scene（等这一场跑完）/ deciding（导演决策中） */
  phase: '' | 'running_scene' | 'deciding'
  current_scene_id: string
  steps_taken: number
  consecutive_rollbacks: number
  stop_reason: AutoPilotStopReason | ''
  stop_message: string
  steps: AutoPilotStep[]
  started_at: string
  updated_at: string
}

export interface GraphNode {
  id: string
  label: string
  nodeType: string
}

export interface GraphEdge {
  source: string
  target: string
  relType: string
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface BuildStatus {
  stage: string
  progress: number
  entity_count?: number
  relation_count?: number
  character_count?: number
  character_done?: number
  character_total?: number
  lore_count?: number
}
