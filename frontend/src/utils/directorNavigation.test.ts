import { describe, expect, it } from 'vitest'
import type { Scene } from '@/types'
import { resolveBranchId, resolveScene } from './directorNavigation'

function scene(sceneId: string): Scene {
  return {
    scene_id: sceneId,
    project_id: 'project-1',
    branch_id: 'branch-1',
    name: sceneId,
    description: '',
    participating_characters: [],
    location: '',
    initial_conditions: {},
    max_turns: 6,
    status: 'completed',
    snapshot_id_before: '',
    snapshot_id_after: null,
    turns_completed: 0,
    speaker_mode: 'round_robin',
    dialogue_log: [],
    created_at: '2026-08-03T00:00:00Z',
  }
}

describe('director URL navigation', () => {
  it('restores valid identities and falls back from invalid query values', () => {
    const scenes = [scene('scene-1'), scene('scene-2')]

    expect(resolveBranchId('branch-2', ['branch-1', 'branch-2'])).toBe('branch-2')
    expect(resolveBranchId('deleted', ['branch-1', 'branch-2'])).toBe('branch-1')
    expect(resolveScene('scene-1', scenes)?.scene_id).toBe('scene-1')
    expect(resolveScene('deleted', scenes)?.scene_id).toBe('scene-2')
    expect(resolveScene('', [])).toBeUndefined()
  })
})