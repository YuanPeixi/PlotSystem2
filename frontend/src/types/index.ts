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
  initial_conditions: Record<string, unknown>
  max_turns: number
  speaker_mode: string
  opening_narration: string
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
