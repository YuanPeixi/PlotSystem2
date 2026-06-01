import axios from 'axios'
import type {
  ApiResponse,
  BranchTree,
  BuildStatus,
  CharacterCard,
  GraphData,
  Project,
  Scene,
  SceneConfig,
  SceneEvaluation,
} from '@/types'

const API_BASE = '/api/v1'

const http = axios.create({ baseURL: API_BASE, timeout: 600000 })

async function unwrap<T>(promise: Promise<{ data: ApiResponse<T> }>): Promise<T> {
  const resp = await promise
  if (!resp.data.success) {
    throw new Error(resp.data.error || '请求失败')
  }
  return resp.data.data
}

export const api = {
  // 项目
  listProjects: () => unwrap<Project[]>(http.get('/projects')),
  createProject: (name: string, description = '') =>
    unwrap<Project>(http.post('/projects', { name, description })),
  getProject: (id: string) => unwrap<Project>(http.get(`/projects/${id}`)),
  deleteProject: (id: string) => unwrap(http.delete(`/projects/${id}`)),

  uploadSeed: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return unwrap<{ path: string; size: number }>(
      http.post(`/projects/${id}/seed`, form),
    )
  },
  build: (id: string) => unwrap<{ status: string }>(http.post(`/projects/${id}/build`)),
  buildStatus: (id: string) => unwrap<BuildStatus>(http.get(`/projects/${id}/build/status`)),

  // 图谱
  getGraph: (id: string) => unwrap<GraphData>(http.get(`/projects/${id}/graph`)),

  // 角色
  listCharacters: (id: string) => unwrap<CharacterCard[]>(http.get(`/projects/${id}/characters`)),
  getCharacter: (id: string, cid: string) =>
    unwrap<CharacterCard>(http.get(`/projects/${id}/characters/${cid}`)),
  updateCharacter: (id: string, cid: string, patch: Partial<CharacterCard>) =>
    unwrap<CharacterCard>(http.patch(`/projects/${id}/characters/${cid}`, patch)),

  // 场景
  planScene: (id: string, branch_id: string, narrative_goal: string) =>
    unwrap<SceneConfig>(http.post(`/projects/${id}/scenes/plan`, { branch_id, narrative_goal })),
  createScene: (id: string, payload: Record<string, unknown>) =>
    unwrap<Scene>(http.post(`/projects/${id}/scenes`, payload)),
  listScenes: (id: string, branchId?: string) =>
    unwrap<Scene[]>(
      http.get(`/projects/${id}/scenes`, { params: branchId ? { branch_id: branchId } : {} }),
    ),
  getScene: (id: string, sid: string) =>
    unwrap<Scene>(http.get(`/projects/${id}/scenes/${sid}`)),
  getSceneById: (sid: string) => unwrap<Scene>(http.get(`/scenes/${sid}`)),
  startScene: (sid: string) => unwrap<{ status: string }>(http.post(`/scenes/${sid}/start`)),
  pauseScene: (sid: string) => unwrap<{ paused: boolean }>(http.post(`/scenes/${sid}/pause`)),
  sceneLog: (sid: string) => unwrap(http.get(`/scenes/${sid}/log`)),

  // 导演
  getEvaluation: (sid: string) =>
    unwrap<SceneEvaluation | null>(http.get(`/scenes/${sid}/evaluation`)),
  submitDecision: (sid: string, payload: Record<string, unknown>) =>
    unwrap(http.post(`/scenes/${sid}/decision`, payload)),

  // 分支/快照
  getBranches: (id: string) => unwrap<BranchTree>(http.get(`/projects/${id}/branches`)),
  listSnapshots: (id: string) => unwrap<unknown[]>(http.get(`/projects/${id}/snapshots`)),
  forkBranch: (id: string, snapshotId: string, payload: Record<string, unknown>) =>
    unwrap(http.post(`/snapshots/${snapshotId}/fork?project_id=${id}`, payload)),

  // 输出
  generateOutput: (id: string, payload: Record<string, unknown>) =>
    unwrap<{ output_id: string; format: string; content: string }>(
      http.post(`/projects/${id}/output`, payload),
    ),
}

/** 打开场景 SSE 流。返回 EventSource。 */
export function openSceneStream(sceneId: string): EventSource {
  return new EventSource(`${API_BASE}/scenes/${sceneId}/stream`)
}
