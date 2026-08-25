import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, openSceneStream } from '@/api/client'
import type { DialogueTurn, Scene, SceneConfig, SceneEvaluation } from '@/types'

/** 场景已经跑完（或被中断），不应再等待流事件。 */
const TERMINAL = ['completed', 'paused']

export const useSceneStore = defineStore('scenes', () => {
  const currentScene = ref<Scene | null>(null)
  const turns = ref<DialogueTurn[]>([])
  const evaluation = ref<SceneEvaluation | null>(null)
  // 本场已生效的决策（刷新后从 GET /decision 恢复），非空时不得再提交（否则 409）
  const appliedDecision = ref<Record<string, unknown> | null>(null)
  const running = ref(false)
  const statusMsg = ref('')
  const decisionPending = ref(false)
  let es: EventSource | null = null

  async function plan(projectId: string, branchId: string, goal: string): Promise<SceneConfig> {
    return api.planScene(projectId, branchId, goal)
  }

  async function createScene(projectId: string, payload: Record<string, unknown>) {
    currentScene.value = await api.createScene(projectId, payload)
    turns.value = []
    evaluation.value = null
    appliedDecision.value = null
    return currentScene.value
  }

  /** 只订阅事件流，不触发启动。首帧 status 由后端回放当前状态。 */
  function openStream(sceneId: string, opts: { keepLog?: boolean } = {}) {
    // keepLog：续跑（continue）在同一场景上追加轮次，后端 SSE 只推送新增部分，
    // 此时必须保留已铺底的历史日志，否则界面上之前的对话会整段消失。
    if (!opts.keepLog) {
      turns.value = []
    }
    running.value = true
    statusMsg.value = '准备中...'

    es?.close()
    es = openSceneStream(sceneId)
    es.addEventListener('turn', (e) => {
      turns.value.push(JSON.parse((e as MessageEvent).data))
    })
    es.addEventListener('status', (e) => {
      const d = JSON.parse((e as MessageEvent).data)
      if (TERMINAL.includes(d.status)) {
        // 后端在订阅建立时会回放一帧当前状态：若这一场早已结束（刷新重连、
        // 或“刚好在订阅前跑完”的竞态），这里直接收敛，不会一直挂在“模拟中”。
        statusMsg.value = d.status === 'completed' ? '场景完成' : '已中断（可重新开始）'
        running.value = false
        es?.close()
        // 以后端持久化的完整日志为准做一次对账：SSE 订阅建立之前
        // （如决策触发续跑后才连上流）产生的轮次不会被推送，这里补齐。
        void reconcileLog(sceneId)
        void loadEvaluation(sceneId)
      } else {
        statusMsg.value = d.status === 'running' ? '模拟中...' : '准备中...'
        running.value = true
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

  function startSimulation(sceneId: string, opts: { keepLog?: boolean } = {}) {
    evaluation.value = null
    openStream(sceneId, opts)
    return api.startScene(sceneId)
  }

  /** 用后端持久化的 dialogue_log 覆盖本地日志，修补 SSE 期间可能遗漏的轮次。 */
  async function reconcileLog(sceneId: string) {
    try {
      const scene = await api.getSceneById(sceneId)
      if (currentScene.value?.scene_id !== sceneId) return
      currentScene.value = scene
      if ((scene.dialogue_log?.length ?? 0) >= turns.value.length) {
        turns.value = scene.dialogue_log ?? []
      }
    } catch {
      // 对账失败不影响已展示的内容，保持现状即可
    }
  }

  async function pause(sceneId: string) {
    await api.pauseScene(sceneId)
  }

  async function loadEvaluation(sceneId: string) {
    try {
      const ev = await api.getEvaluation(sceneId)
      if (currentScene.value?.scene_id === sceneId && ev) evaluation.value = ev
    } catch {
      // 评估尚未生成时后端返回 null，无需处理
    }
  }

  async function loadDecision(sceneId: string) {
    try {
      const d = await api.getDecision(sceneId)
      if (currentScene.value?.scene_id === sceneId) appliedDecision.value = d
    } catch {
      // 查不到就当作尚未决策
    }
  }

  function stopStream() {
    es?.close()
    es = null
  }

  async function submitDecision(sceneId: string, payload: Record<string, unknown>) {
    // UI 层辅助防护：请求处理期间禁用决策按钮，避免快速连点重复提交
    // （真正的幂等保证在后端：decisions 表持久化重放 + scenes.status 的 CAS
    // 条件更新，见工单13；重试命中重放时后端返回与首次相同的 next_scene_id）。
    if (decisionPending.value) return
    decisionPending.value = true
    try {
      const decision = await api.submitDecision(sceneId, payload)
      // continue / next_scene 决策返回 next_scene_id 时，自动建立对应场景的流
      const nextId = (decision as Record<string, unknown>)?.next_scene_id as string | undefined
      if (nextId) {
        await joinScene(nextId)
      }
      return decision
    } finally {
      decisionPending.value = false
    }
  }

  /** 加入一个已存在的场景并启动模拟（决策产生新场景/续跑时使用）。 */
  async function joinScene(sceneId: string) {
    const scene = await api.getSceneById(sceneId)
    currentScene.value = scene
    // 先用已持久化的对话日志铺底：continue 续跑时后端只推送新增轮次，
    // 若这里清空，用户会看到"点了继续，之前的对话全没了"。
    turns.value = scene.dialogue_log ?? []
    evaluation.value = null
    appliedDecision.value = null
    await startSimulation(sceneId, { keepLog: true })
  }

  /**
   * 打开一个已存在的场景（刷新恢复 / 从场景列表点选）。
   *
   * 与 joinScene 的区别：**绝不调用 /start**。已完成的场景重新 start 会白跑一遍
   * LLM 并覆盖快照与评估；运行中的场景只需要重新订阅事件流即可续看。
   */
  async function attachScene(sceneId: string) {
    const scene = await api.getSceneById(sceneId)
    currentScene.value = scene
    turns.value = scene.dialogue_log ?? []
    evaluation.value = null
    appliedDecision.value = null
    void loadEvaluation(sceneId)
    void loadDecision(sceneId)

    if (scene.status === 'running') {
      openStream(sceneId, { keepLog: true })
      statusMsg.value = '模拟中...（已重新连接）'
    } else {
      stopStream()
      running.value = false
      statusMsg.value =
        scene.status === 'completed'
          ? '场景完成'
          : scene.status === 'paused'
            ? '已中断（可重新开始）'
            : '未开始'
    }
    return scene
  }

  /** 对未完成的场景（pending / paused）重新发起模拟。 */
  async function resumeScene(sceneId: string) {
    const scene =
      currentScene.value?.scene_id === sceneId
        ? currentScene.value
        : await api.getSceneById(sceneId)
    currentScene.value = scene
    turns.value = scene.dialogue_log ?? []
    await startSimulation(sceneId, { keepLog: true })
  }

  return {
    currentScene,
    turns,
    evaluation,
    appliedDecision,
    running,
    statusMsg,
    decisionPending,
    plan,
    createScene,
    startSimulation,
    joinScene,
    attachScene,
    resumeScene,
    pause,
    stopStream,
    submitDecision,
  }
})
