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
  turns_completed: number
  speaker_mode: string
  dialogue_log: DialogueTurn[]
  created_at?: string
}

/** 快照元信息（GET /projects/{id}/snapshots 的列表项，不含角色状态明细）。 */
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
