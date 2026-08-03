import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, openSceneStream } from '@/api/client'
import type {
  DialogueTurn,
  DirectorDecision,
  Scene,
  SceneConfig,
  SceneEvaluation,
} from '@/types'

export const useSceneStore = defineStore('scenes', () => {
  const currentScene = ref<Scene | null>(null)
  const scenesInBranch = ref<Scene[]>([])
  const turns = ref<DialogueTurn[]>([])
  const evaluation = ref<SceneEvaluation | null>(null)
  const currentDecision = ref<DirectorDecision | null>(null)
  const running = ref(false)
  const creating = ref(false)
  const starting = ref(false)
  const statusMsg = ref('')
  const decisionPending = ref(false)
  const listLoading = ref(false)
  const sceneLoading = ref(false)
  const listError = ref('')
  const sceneError = ref('')
  let es: EventSource | null = null

  function syncSceneInList(scene: Scene) {
    const index = scenesInBranch.value.findIndex((item) => item.scene_id === scene.scene_id)
    if (index >= 0) scenesInBranch.value[index] = { ...scene }
  }

  async function plan(projectId: string, branchId: string, goal: string): Promise<SceneConfig> {
    return api.planScene(projectId, branchId, goal)
  }

  async function createScene(projectId: string, payload: Record<string, unknown>) {
    if (creating.value) return null
    creating.value = true
    try {
      currentScene.value = await api.createScene(projectId, payload)
      turns.value = []
      evaluation.value = null
      currentDecision.value = null
      return currentScene.value
    } finally {
      creating.value = false
    }
  }

  async function loadByBranch(projectId: string, branchId: string) {
    listLoading.value = true
    listError.value = ''
    scenesInBranch.value = []
    try {
      scenesInBranch.value = await api.listScenes(projectId, branchId)
      return scenesInBranch.value
    } catch (error) {
      listError.value = error instanceof Error ? error.message : '场景列表加载失败'
      throw error
    } finally {
      listLoading.value = false
    }
  }

  function subscribeToScene(sceneId: string) {
    stopStream()
    running.value = true
    statusMsg.value = '连接场景...'

    es = openSceneStream(sceneId)
    es.addEventListener('turn', (e) => {
      const turn = JSON.parse((e as MessageEvent).data) as DialogueTurn
      if (!turns.value.some((item) => item.turn_id === turn.turn_id)) {
        turns.value.push(turn)
        if (currentScene.value?.scene_id === sceneId) {
          currentScene.value.turns_completed = Math.max(
            currentScene.value.turns_completed,
            turn.turn_number,
          )
          currentScene.value.status = 'running'
          syncSceneInList(currentScene.value)
        }
      }
    })
    es.addEventListener('status', (e) => {
      const d = JSON.parse((e as MessageEvent).data)
      statusMsg.value = d.status === 'completed' ? '场景完成' : '模拟中...'
      if (currentScene.value?.scene_id === sceneId) {
        currentScene.value.status = d.status
        syncSceneInList(currentScene.value)
      }
      if (d.status === 'completed') {
        running.value = false
        es?.close()
        void reconcileScene(sceneId)
      }
    })
    es.addEventListener('evaluation', (e) => {
      evaluation.value = JSON.parse((e as MessageEvent).data)
    })
    es.addEventListener('error', () => {
      statusMsg.value = '连接中断'
      running.value = false
    })
  }

  async function startSimulation(sceneId: string) {
    if (starting.value || running.value) return
    if (currentScene.value?.scene_id !== sceneId) await loadScene(sceneId)
    if (currentScene.value?.status !== 'pending') return
    starting.value = true
    evaluation.value = null
    currentDecision.value = null
    subscribeToScene(sceneId)
    statusMsg.value = '准备中...'
    try {
      const result = await api.startScene(sceneId)
      if (currentScene.value?.scene_id === sceneId) {
        currentScene.value.status = 'running'
        syncSceneInList(currentScene.value)
      }
      statusMsg.value = result.status === 'already_running' ? '模拟中...' : '场景已启动'
    } catch (error) {
      stopStream()
      throw error
    } finally {
      starting.value = false
    }
  }

  async function reconcileScene(sceneId: string) {
    try {
      const scene = await api.getSceneById(sceneId)
      if (currentScene.value?.scene_id !== sceneId) return
      currentScene.value = scene
      turns.value = scene.dialogue_log ?? []
      syncSceneInList(scene)
      ;[evaluation.value, currentDecision.value] = await Promise.all([
        api.getEvaluation(sceneId),
        api.getDecision(sceneId),
      ])
    } catch {
      // SSE 已展示内容仍可用，完整对账留待下次刷新。
    }
  }

  async function loadScene(sceneId: string) {
    stopStream()
    sceneLoading.value = true
    sceneError.value = ''
    currentScene.value = null
    turns.value = []
    evaluation.value = null
    currentDecision.value = null
    try {
      const scene = await api.getSceneById(sceneId)
      currentScene.value = scene
      turns.value = scene.dialogue_log ?? []
      syncSceneInList(scene)
      ;[evaluation.value, currentDecision.value] = await Promise.all([
        api.getEvaluation(sceneId),
        api.getDecision(sceneId),
      ])
      statusMsg.value = scene.status === 'completed' ? '只读 · 已完成' : scene.status
      if (scene.status === 'running') subscribeToScene(sceneId)
      return scene
    } catch (error) {
      sceneError.value = error instanceof Error ? error.message : '场景加载失败'
      throw error
    } finally {
      sceneLoading.value = false
    }
  }

  async function pause(sceneId: string) {
    await api.pauseScene(sceneId)
  }

  function clearSelection() {
    stopStream()
    currentScene.value = null
    turns.value = []
    evaluation.value = null
    currentDecision.value = null
    sceneError.value = ''
    statusMsg.value = ''
  }

  function stopStream() {
    es?.close()
    es = null
    running.value = false
  }

  async function submitDecision(sceneId: string, payload: Record<string, unknown>) {
    // UI 层辅助防护：请求处理期间禁用决策按钮，避免快速连点重复提交
    // （真正的幂等保证在后端：decisions 表持久化重放 + scenes.status 的 CAS
    // 条件更新，见工单13；重试命中重放时后端返回与首次相同的 next_scene_id）。
    if (decisionPending.value) return
    decisionPending.value = true
    try {
      const decision = await api.submitDecision(sceneId, payload)
      currentDecision.value = decision
      return decision
    } finally {
      decisionPending.value = false
    }
  }

  return {
    currentScene,
    scenesInBranch,
    turns,
    evaluation,
    currentDecision,
    running,
    creating,
    starting,
    statusMsg,
    decisionPending,
    listLoading,
    sceneLoading,
    listError,
    sceneError,
    plan,
    createScene,
    loadByBranch,
    loadScene,
    clearSelection,
    subscribeToScene,
    startSimulation,
    pause,
    stopStream,
    submitDecision,
  }
})
