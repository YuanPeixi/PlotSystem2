import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, openSceneStream } from '@/api/client'
import type { AutoPilotSession, DialogueTurn, Scene, SceneConfig, SceneEvaluation } from '@/types'

/** 场景已经跑完（或被中断），不应再等待流事件。 */
const TERMINAL = ['completed', 'paused']
/** AutoPilot 的兜底轮询间隔：从已完成的场景开启时，下一步只能靠它得知 */
const AUTOPILOT_POLL_MS = 3000

function newRequestId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
  )
}

export const useSceneStore = defineStore('scenes', () => {
  const currentScene = ref<Scene | null>(null)
  const turns = ref<DialogueTurn[]>([])
  const evaluation = ref<SceneEvaluation | null>(null)
  // 本场已生效的决策（刷新后从 GET /decision 恢复），非空时不得再提交（否则 409）
  const appliedDecision = ref<Record<string, unknown> | null>(null)
  const running = ref(false)
  const statusMsg = ref('')
  // 后端推送的最近一条业务失败原因（scene_error），供界面展示
  const lastError = ref('')
  const decisionPending = ref(false)
  // 该项目最近一次自动推演会话（工单12）。只在后端进程内存里，重启后为 null
  const autopilot = ref<AutoPilotSession | null>(null)
  // 每跟随到一场就 +1：页面据此同步分支选择与 URL（自动回滚会换分支）
  const autopilotFollows = ref(0)
  let autopilotTimer: ReturnType<typeof setInterval> | null = null
  // 当前导演页绑定的项目；别的项目的会话一律不收
  let autopilotProject = ''
  // 生命周期版本：绑定/离页时前进，此前发出的请求回来后一律作废
  let autopilotEpoch = 0
  // 会话要求舞台去、但还没成功到达的场景；到达即清空（见 applyAutopilot）
  let followTarget = ''
  let followInFlight = ''
  // 跟随请求的序号：更晚发出的跟随让更早的作废，旧响应不得把舞台切回去
  let followSeq = 0
  // 开启请求的幂等键：参数不变的重试（上次失败/超时）沿用同一个，后端认出重放
  let pendingStart: { key: string; requestId: string } | null = null
  let es: EventSource | null = null

  async function plan(
    projectId: string,
    branchId: string,
    sceneIntent: string,
  ): Promise<SceneConfig> {
    return api.planScene(projectId, branchId, sceneIntent)
  }

  /**
   * 只落库，不碰舞台：创建请求在途时用户可能已取消或切走，
   * 由调用方判断这份结果还该不该上台（再调 startNewScene）。
   */
  async function createScene(projectId: string, payload: Record<string, unknown>): Promise<Scene> {
    return api.createScene(projectId, payload)
  }

  /** 把刚建好的场景设为当前并开演。 */
  async function startNewScene(scene: Scene) {
    currentScene.value = scene
    turns.value = []
    appliedDecision.value = null
    return startSimulation(scene.scene_id)
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
    const source = openSceneStream(sceneId)
    es = source
    // 事件处理器一律先校验 source 仍是当前连接：快速切换场景时旧流的终态事件
    // 会关掉新流，旧流的 turn 也会串到新场景的日志里。
    const stale = () => es !== source
    source.addEventListener('turn', (e) => {
      if (stale()) return
      const turn: DialogueTurn = JSON.parse((e as MessageEvent).data)
      // 逐轮落盘先于 SSE 推送，铺底用的 GET 可能已经包含这一轮，按 turn_id 去重
      if (turns.value.some((t) => t.turn_id === turn.turn_id)) return
      turns.value.push(turn)
    })
    source.addEventListener('status', (e) => {
      if (stale()) return
      const d = JSON.parse((e as MessageEvent).data)
      if (TERMINAL.includes(d.status)) {
        // 后端在订阅建立时会回放一帧当前状态：若这一场早已结束（刷新重连、
        // 或“刚好在订阅前跑完”的竞态），这里直接收敛，不会一直挂在“模拟中”。
        statusMsg.value = d.status === 'completed' ? '场景完成' : '已中断（可重新开始）'
        running.value = false
        source.close()
        // 以后端持久化的完整日志为准做一次对账：SSE 订阅建立之前
        // （如决策触发续跑后才连上流）产生的轮次不会被推送，这里补齐。
        void reconcileLog(sceneId)
        void loadEvaluation(sceneId)
      } else {
        statusMsg.value = d.status === 'running' ? '模拟中...' : '准备中...'
        running.value = true
      }
    })
    source.addEventListener('evaluation', (e) => {
      if (stale()) return
      evaluation.value = JSON.parse((e as MessageEvent).data)
    })
    // 自动决策的结果先于终态帧到达：下一场是哪个只能从这里得知
    source.addEventListener('autopilot', (e) => {
      if (stale()) return
      applyAutopilot(JSON.parse((e as MessageEvent).data))
    })
    // 后端的业务失败走 scene_error；同名的 'error' 是 EventSource 原生的连接错误，
    // 两者合并处理会把失败原因吞掉（紧跟着的 status 还会把提示覆盖）。
    source.addEventListener('scene_error', (e) => {
      if (stale()) return
      const d = JSON.parse((e as MessageEvent).data)
      lastError.value = d.message || '场景运行出错'
      if (d.fatal) statusMsg.value = `已中断：${lastError.value}`
    })
    source.addEventListener('error', () => {
      if (stale()) return
      statusMsg.value = '连接中断'
      running.value = false
    })
    return source
  }

  /**
   * 开流并请求启动。返回是否启动成功；失败时不抛出，原因写进 lastError。
   *
   * 流必须先于 /start 建立（否则会漏掉开头几轮），但 /start 失败时后端场景仍是
   * pending，SSE 首帧回放 pending 会让界面永远停在"准备中"、running 恒为真，
   * 连重试按钮都不出现。所以失败时要把流关掉、状态复位，让场景回到可开演。
   */
  async function startSimulation(sceneId: string, opts: { keepLog?: boolean } = {}): Promise<boolean> {
    evaluation.value = null
    lastError.value = ''
    const source = openStream(sceneId, opts)
    try {
      await api.startScene(sceneId)
      return true
    } catch (err) {
      // 期间已切到别的场景：新流不归这次失败管
      if (es === source) {
        stopStream()
        running.value = false
        statusMsg.value = '启动失败'
        lastError.value = err instanceof Error ? err.message : '启动失败，请重试'
      }
      return false
    }
  }

  /** 用后端持久化的 dialogue_log 覆盖本地日志，修补 SSE 期间可能遗漏或重复的轮次。 */
  async function reconcileLog(sceneId: string) {
    try {
      const scene = await api.getSceneById(sceneId)
      if (currentScene.value?.scene_id !== sceneId) return
      currentScene.value = scene
      // 终态时后端日志已经是完整的真相源，无条件以它为准（早先按数组长度
      // 比较会在本地多出重复轮次时拒绝修正）。
      turns.value = scene.dialogue_log ?? []
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

  /** 清空当前场景（切换到没有场景的分支时用，避免日志/决策跨分支残留）。 */
  function clearScene() {
    stopStream()
    currentScene.value = null
    turns.value = []
    evaluation.value = null
    appliedDecision.value = null
    running.value = false
    statusMsg.value = ''
    lastError.value = ''
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
    return showAttachedScene(await api.getSceneById(sceneId))
  }

  /** attachScene 的提交半段：拿到场景之后才改舞台与流，调用方可在两者之间校验结果是否过期。 */
  function showAttachedScene(scene: Scene) {
    const sceneId = scene.scene_id
    currentScene.value = scene
    turns.value = scene.dialogue_log ?? []
    evaluation.value = null
    appliedDecision.value = null
    lastError.value = ''
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

  // ---- AutoPilot（工单12）----

  /**
   * 收下一份会话状态（SSE 与轮询两条路都走这里）。
   *
   * 只在会话**前进**时跟随（换了场景、多了一步、或刚开启的新会话），而不是"当前场景
   * 与会话不一致就跟"：否则用户在自动推演期间点开别的场景翻看，轮询每 3 秒就把他拽回去。
   * 前进时记下 followTarget，直到真正到达才清掉：跟随失败后，下一次轮询带回的是同一步，
   * 不算前进，只靠"前进"判断就再也不会重试。
   */
  function applyAutopilot(next: AutoPilotSession | null) {
    // 别的项目的会话（换项目后旧流上迟到的事件）一律不收
    if (next && next.project_id !== autopilotProject) return
    const prev = autopilot.value
    // 迟到的旧状态（轮询响应慢于 SSE）不能把会话倒回去
    if (next && prev && next.session_id === prev.session_id && next.updated_at < prev.updated_at) return
    autopilot.value = next
    syncAutopilotPolling()
    if (!next) {
      followTarget = ''
      return
    }
    const advanced =
      prev && prev.session_id === next.session_id
        ? prev.current_scene_id !== next.current_scene_id || next.steps.length > prev.steps.length
        : next.status === 'running'
    if (advanced) followTarget = next.current_scene_id
    if (followTarget) void followAutopilot()
  }

  /** 把舞台切到会话要求的场景。后端已经替用户开演，这里绝不调 /start。 */
  async function followAutopilot() {
    const id = followTarget
    // 同一场的跟随已在途：等它的结果，不重复取
    if (!id || followInFlight === id) return
    // 自动 continue：后端刻意没发终态帧，还开着的流会直接收到新一轮
    if (currentScene.value?.scene_id === id && es && es.readyState !== EventSource.CLOSED) {
      evaluation.value = null
      appliedDecision.value = null
      followTarget = ''
      return
    }
    const seq = ++followSeq
    const epoch = autopilotEpoch
    followInFlight = id
    try {
      const scene = await api.getSceneById(id)
      // 校验必须在改舞台、换流**之前**：期间会话又前进了（更新的跟随已发出）或已离页，
      // 这份结果就过期了 —— 提交之后再查，舞台和流已经被切回旧场景
      if (seq !== followSeq || epoch !== autopilotEpoch) return
      showAttachedScene(scene)
      // 刚建好的下一场可能还没来得及开跑，但后端马上就会开演
      if (autopilot.value?.status === 'running' && scene.status === 'pending') {
        openStream(id, { keepLog: true })
      }
      if (followTarget === id) followTarget = ''
      autopilotFollows.value++
    } catch {
      // followTarget 保留：下一次轮询会再试
    } finally {
      if (seq === followSeq) followInFlight = ''
    }
  }

  function syncAutopilotPolling() {
    const projectId = autopilot.value?.status === 'running' ? autopilot.value.project_id : ''
    if (projectId && !autopilotTimer) {
      autopilotTimer = setInterval(() => void refreshAutopilot(projectId), AUTOPILOT_POLL_MS)
    } else if (!projectId && autopilotTimer) {
      clearInterval(autopilotTimer)
      autopilotTimer = null
    }
  }

  async function refreshAutopilot(projectId: string) {
    const epoch = autopilotEpoch
    try {
      const session = await api.getAutopilot(projectId)
      // 离页（或换项目）之前发出的请求：结果作废，否则会重新建立轮询、把舞台切回旧项目
      if (epoch !== autopilotEpoch || projectId !== autopilotProject) return
      // 后端重启后会话就没了（null）：同样收下，停掉轮询
      applyAutopilot(session)
    } catch {
      // 轮询失败不打扰用户，下一轮再说
    }
  }

  async function startAutopilot(
    projectId: string,
    sceneId: string,
    maxSteps: number,
    maxRollbacks: number,
  ) {
    const key = `${projectId}|${sceneId}|${maxSteps}|${maxRollbacks}`
    if (pendingStart?.key !== key) pendingStart = { key, requestId: newRequestId() }
    const epoch = autopilotEpoch
    const session = await api.startAutopilot(projectId, {
      scene_id: sceneId,
      request_id: pendingStart.requestId,
      max_steps: maxSteps,
      max_consecutive_rollbacks: maxRollbacks,
    })
    pendingStart = null
    if (epoch === autopilotEpoch) applyAutopilot(session)
  }

  async function stopAutopilot(projectId: string) {
    const epoch = autopilotEpoch
    const session = await api.stopAutopilot(projectId)
    if (session && epoch === autopilotEpoch) applyAutopilot(session)
  }

  /** 导演页挂载：从此只收这个项目的会话，并取回进行中的会话（刷新恢复）。 */
  function bindAutopilot(projectId: string) {
    resetAutopilot()
    autopilotProject = projectId
    return refreshAutopilot(projectId)
  }

  /**
   * 离开导演页/换项目：停掉轮询、丢掉会话（后端的会话不受影响）。
   * 生命周期版本前进，让此前发出的轮询、开启/停止与跟随请求全部作废。
   */
  function resetAutopilot() {
    autopilotEpoch++
    autopilotProject = ''
    followTarget = ''
    followInFlight = ''
    followSeq++
    autopilot.value = null
    syncAutopilotPolling()
  }

  return {
    currentScene,
    turns,
    evaluation,
    appliedDecision,
    running,
    statusMsg,
    lastError,
    decisionPending,
    autopilot,
    autopilotFollows,
    plan,
    createScene,
    startNewScene,
    startSimulation,
    joinScene,
    attachScene,
    resumeScene,
    pause,
    stopStream,
    clearScene,
    submitDecision,
    applyAutopilot,
    bindAutopilot,
    refreshAutopilot,
    startAutopilot,
    stopAutopilot,
    resetAutopilot,
  }
})
