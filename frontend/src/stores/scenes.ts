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

  function startSimulation(sceneId: string) {
    turns.value = []
    evaluation.value = null
    running.value = true
    statusMsg.value = '准备中...'

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
      }
    })
    es.addEventListener('evaluation', (e) => {
      evaluation.value = JSON.parse((e as MessageEvent).data)
    })
    es.addEventListener('error', () => {
      statusMsg.value = '连接中断'
      running.value = false
    })

    return api.startScene(sceneId)
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
    // continue / next_scene 决策返回 next_scene_id 时，自动建立对应场景的流
    const nextId = (decision as Record<string, unknown>)?.next_scene_id as string | undefined
    if (nextId) {
      await joinScene(nextId)
    }
    return decision
  }

  /** 加入一个已存在的场景（获取场景元信息 + 启动模拟流） */
  async function joinScene(sceneId: string) {
    currentScene.value = await api.getSceneById(sceneId)
    turns.value = []
    evaluation.value = null
    await startSimulation(sceneId)
  }

  return {
    currentScene,
    turns,
    evaluation,
    running,
    statusMsg,
    plan,
    createScene,
    startSimulation,
    joinScene,
    pause,
    stopStream,
    submitDecision,
  }
})
