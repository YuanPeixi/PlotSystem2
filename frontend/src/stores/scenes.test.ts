import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DirectorDecision, Scene } from '@/types'

const apiMock = vi.hoisted(() => ({
  createScene: vi.fn(),
  listScenes: vi.fn(),
  getSceneById: vi.fn(),
  getEvaluation: vi.fn(),
  getDecision: vi.fn(),
  startScene: vi.fn(),
}))
const streamMock = vi.hoisted(() => vi.fn())

vi.mock('@/api/client', () => ({ api: apiMock, openSceneStream: streamMock }))

import { useSceneStore } from './scenes'

function makeScene(status: string): Scene {
  return {
    scene_id: `scene-${status}`,
    project_id: 'project-1',
    branch_id: 'branch-1',
    name: status,
    description: '',
    participating_characters: [],
    location: '',
    initial_conditions: {},
    max_turns: 6,
    status,
    snapshot_id_before: '',
    snapshot_id_after: null,
    turns_completed: 0,
    speaker_mode: 'round_robin',
    dialogue_log: [],
    created_at: '2026-08-03T00:00:00Z',
  }
}

function fakeStream() {
  return { addEventListener: vi.fn(), close: vi.fn() }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  apiMock.getEvaluation.mockResolvedValue(null)
  apiMock.getDecision.mockResolvedValue(null)
  streamMock.mockImplementation(fakeStream)
})

describe('scene store navigation', () => {
  it('loads scenes for the selected branch', async () => {
    apiMock.listScenes.mockResolvedValue([makeScene('completed')])
    const store = useSceneStore()

    await store.loadByBranch('project-1', 'branch-1')

    expect(apiMock.listScenes).toHaveBeenCalledWith('project-1', 'branch-1')
    expect(store.scenesInBranch).toHaveLength(1)
  })

  it('opens completed history read-only without starting or subscribing', async () => {
    apiMock.getSceneById.mockResolvedValue(makeScene('completed'))
    const store = useSceneStore()

    await store.loadScene('scene-completed')

    expect(apiMock.startScene).not.toHaveBeenCalled()
    expect(streamMock).not.toHaveBeenCalled()
    expect(store.statusMsg).toBe('只读 · 已完成')
  })

  it('subscribes to a running scene without calling start', async () => {
    apiMock.getSceneById.mockResolvedValue(makeScene('running'))
    const store = useSceneStore()

    await store.loadScene('scene-running')

    expect(streamMock).toHaveBeenCalledWith('scene-running')
    expect(apiMock.startScene).not.toHaveBeenCalled()
  })

  it('restores an applied decision with its next scene id', async () => {
    const decision = {
      decision_type: 'next_scene',
      next_scene_id: 'scene-next',
    } as DirectorDecision
    apiMock.getSceneById.mockResolvedValue(makeScene('completed'))
    apiMock.getDecision.mockResolvedValue(decision)
    const store = useSceneStore()

    await store.loadScene('scene-completed')

    expect(apiMock.getDecision).toHaveBeenCalledWith('scene-completed')
    expect(store.currentDecision?.next_scene_id).toBe('scene-next')
  })

  it('starts only when a pending scene is explicitly requested', async () => {
    apiMock.getSceneById.mockResolvedValue(makeScene('pending'))
    apiMock.startScene.mockResolvedValue({ status: 'started' })
    const store = useSceneStore()

    await store.loadScene('scene-pending')
    expect(apiMock.startScene).not.toHaveBeenCalled()

    await store.startSimulation('scene-pending')
    expect(apiMock.startScene).toHaveBeenCalledOnce()
    expect(store.currentScene?.status).toBe('running')
  })

  it('deduplicates concurrent scene creation and start requests', async () => {
    let finishCreate: ((scene: Scene) => void) | undefined
    apiMock.createScene.mockReturnValue(new Promise<Scene>((resolve) => { finishCreate = resolve }))
    const store = useSceneStore()

    const firstCreate = store.createScene('project-1', {})
    const secondCreate = store.createScene('project-1', {})
    expect(apiMock.createScene).toHaveBeenCalledOnce()
    finishCreate?.(makeScene('pending'))
    await Promise.all([firstCreate, secondCreate])

    let finishStart: ((result: { status: string }) => void) | undefined
    apiMock.startScene.mockReturnValue(new Promise((resolve) => { finishStart = resolve }))
    const firstStart = store.startSimulation('scene-pending')
    const secondStart = store.startSimulation('scene-pending')
    expect(apiMock.startScene).toHaveBeenCalledOnce()
    finishStart?.({ status: 'started' })
    await Promise.all([firstStart, secondStart])
  })
})