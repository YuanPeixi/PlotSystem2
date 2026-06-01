import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, openSceneStream } from '@/api/client'
import type { DialogueTurn, Scene, SceneConfig, SceneEvaluation } from '@/types'

export const useSceneStore = defineStore('scenes', () => {
  const currentScene = ref<Scene | null>(null)
  const turns = ref<DialogueTurn[]>([])
  const evaluation = ref<SceneEvaluation | null>(null)
  const running = ref(false)
  const statusMsg = ref('')
  /** 自增计数，每次场景跑完 +1，可被外部 watch 用于刷新分支树/场景列表 */
  const completionTick = ref(0)
  let es: EventSource | null = null

  async function plan(projectId: string, branchId: string, goal: string): Promise<SceneConfig> {
    return api.planScene(projectId, branchId, goal)
  }

  async function createScene(projectId: string, payload: Record<string, unknown>) {
    currentScene.value = await api.createScene(projectId, payload)
    turns.value = []
    evaluation.value = null
    return currentScene.value
  }

  /**
   * 启动场景模拟并订阅 SSE。
   * @param sceneId 场景 ID
   * @param opts.keepLog true 时保留已有 turns（continue 续跑场景使用）
   * @param opts.autoStart 是否调用 /start（恢复刷新时可不重新触发）
   */
  function startSimulation(
    sceneId: string,
    opts: { keepLog?: boolean; autoStart?: boolean } = {},
  ) {
    const { keepLog = false, autoStart = true } = opts
    if (!keepLog) {
      turns.value = []
      evaluation.value = null
    }
    running.value = true
    statusMsg.value = '准备中...'
    persistTracking(sceneId)

    es?.close()
    es = openSceneStream(sceneId)
    es.addEventListener('turn', (e) => {
      turns.value.push(JSON.parse((e as MessageEvent).data))
    })
    es.addEventListener('status', (e) => {
      const d = JSON.parse((e as MessageEvent).data)
      statusMsg.value = d.status === 'completed' ? '场景完成' : '模拟中...'
      if (d.status === 'completed') {
        running.value = false
        es?.close()
        clearTracking()
        completionTick.value += 1
      }
    })
    es.addEventListener('evaluation', (e) => {
      evaluation.value = JSON.parse((e as MessageEvent).data)
    })
    es.addEventListener('error', () => {
      statusMsg.value = '连接中断'
      running.value = false
    })

    if (autoStart) return api.startScene(sceneId)
    return Promise.resolve({ status: 'subscribed' })
  }

  async function pause(sceneId: string) {
    await api.pauseScene(sceneId)
  }

  function stopStream() {
    es?.close()
    es = null
  }

  async function submitDecision(sceneId: string, payload: Record<string, unknown>) {
    const decision = await api.submitDecision(sceneId, payload)
    const nextId = (decision as Record<string, unknown>)?.next_scene_id as string | undefined
    const type = (decision as Record<string, unknown>)?.decision_type as string | undefined

    if (type === 'continue' && nextId === sceneId) {
      // 续跑同一场景：保留日志，后端已自动重新触发 run_scene，
      // 这里只需重新订阅 SSE，不要再次 POST /start
      await joinScene(sceneId, { keepLog: true, autoStart: false })
    } else if (nextId) {
      // 下一场场景：不自动开跑，等用户在前端确认/编辑后再点开始
      await loadScene(nextId)
    }
    return decision
  }

  /** 加入一个已存在的场景（获取场景元信息 + 启动模拟流） */
  async function joinScene(
    sceneId: string,
    opts: { keepLog?: boolean; autoStart?: boolean } = {},
  ) {
    currentScene.value = await api.getSceneById(sceneId)
    if (!opts.keepLog) {
      turns.value = []
      evaluation.value = null
    }
    await startSimulation(sceneId, opts)
  }

  /** 仅加载场景元信息（不订阅 SSE，不启动模拟）。 */
  async function loadScene(sceneId: string) {
    currentScene.value = await api.getSceneById(sceneId)
    turns.value = []
    evaluation.value = null
    statusMsg.value = '已规划，等待开始'
  }

  // ---- 持久化追踪：用于刷新页面后恢复正在运行/已完成的场景 ----
  const TRACK_KEY = 'plotsystem.tracking_scene_id'
  function persistTracking(sceneId: string) {
    try {
      localStorage.setItem(TRACK_KEY, sceneId)
    } catch {
      /* ignore */
    }
  }
  function clearTracking() {
    try {
      localStorage.removeItem(TRACK_KEY)
    } catch {
      /* ignore */
    }
  }
  function getTrackedSceneId(): string | null {
    try {
      return localStorage.getItem(TRACK_KEY)
    } catch {
      return null
    }
  }

  /**
   * 从后端拉取场景完整状态恢复（含 dialogue_log + 评估）。
   * 若场景仍在 running 则重新订阅 SSE；否则只把数据加载到 store。
   */
  async function restoreScene(sceneId: string) {
    const scene = await api.getSceneById(sceneId)
    currentScene.value = scene
    turns.value = [...scene.dialogue_log]
    try {
      evaluation.value = await api.getEvaluation(sceneId)
    } catch {
      evaluation.value = null
    }
    if (scene.status === 'running') {
      statusMsg.value = '重新连接中...'
      es?.close()
      es = openSceneStream(sceneId)
      es.addEventListener('turn', (e) => {
        turns.value.push(JSON.parse((e as MessageEvent).data))
      })
      es.addEventListener('status', (e) => {
        const d = JSON.parse((e as MessageEvent).data)
        statusMsg.value = d.status === 'completed' ? '场景完成' : '模拟中...'
        if (d.status === 'completed') {
          running.value = false
          es?.close()
          clearTracking()
          completionTick.value += 1
        }
      })
      es.addEventListener('evaluation', (e) => {
        evaluation.value = JSON.parse((e as MessageEvent).data)
      })
      running.value = true
    } else {
      statusMsg.value = scene.status === 'completed' ? '场景完成' : '空闲'
      clearTracking()
    }
    return scene
  }

  return {
    currentScene,
    turns,
    evaluation,
    running,
    statusMsg,
    completionTick,
    plan,
    createScene,
    startSimulation,
    joinScene,
    loadScene,
    restoreScene,
    getTrackedSceneId,
    pause,
    stopStream,
    submitDecision,
  }
})
