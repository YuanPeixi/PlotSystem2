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

export interface DialogueTurn {
  turn_id: string
  scene_id: string
  turn_number: number
  character_id: string
  character_name: string
  dialogue: string | null
  action: string | null
  inner_thought: string | null
}

export interface Scene {
  scene_id: string
  project_id: string
  branch_id: string
  name: string
  description: string
  participating_characters: string[]
  location: string
  initial_conditions: Record<string, unknown>
  max_turns: number
  status: string
  snapshot_id_before: string
  snapshot_id_after: string | null
  turns_completed: number
  dialogue_log: DialogueTurn[]
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
  lore_count?: number
}
