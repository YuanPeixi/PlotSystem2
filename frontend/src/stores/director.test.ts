import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  getBranches: vi.fn(),
  listSnapshots: vi.fn(),
  forkBranch: vi.fn(),
}))

vi.mock('@/api/client', () => ({ api: apiMock }))

import { useDirectorStore } from './director'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  apiMock.getBranches.mockResolvedValue({ project_id: 'project-1', roots: [] })
  apiMock.listSnapshots.mockResolvedValue([
    {
      snapshot_id: 'snapshot-1',
      scene_id: 'scene-1',
      branch_id: 'branch-1',
      label: 'before:测试场景',
      created_at: '2026-08-03T00:00:00Z',
    },
  ])
})

describe('director snapshot navigation', () => {
  it('loads snapshots and refreshes navigation after fork', async () => {
    apiMock.forkBranch.mockResolvedValue({ branch_id: 'branch-new' })
    const store = useDirectorStore()

    await store.loadSnapshots('project-1')
    const branch = await store.fork('project-1', 'snapshot-1', '新分支', {}, '')

    expect(store.snapshots[0].label).toBe('before:测试场景')
    expect(apiMock.forkBranch).toHaveBeenCalledWith('project-1', 'snapshot-1', {
      branch_name: '新分支',
      new_conditions: {},
      director_notes: '',
    })
    expect(apiMock.getBranches).toHaveBeenCalledWith('project-1')
    expect(apiMock.listSnapshots).toHaveBeenCalledTimes(2)
    expect(branch.branch_id).toBe('branch-new')
  })
})