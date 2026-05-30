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
    return api.submitDecision(sceneId, payload)
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
    pause,
    stopStream,
    submitDecision,
  }
})
