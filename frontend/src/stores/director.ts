import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { BranchTree, Scene } from '@/types'

export const useDirectorStore = defineStore('director', () => {
  const branchTree = ref<BranchTree>({ project_id: '', roots: [] })
  const snapshots = ref<any[]>([])
  const scenes = ref<Scene[]>([])

  async function loadBranches(projectId: string) {
    branchTree.value = await api.getBranches(projectId)
  }

  async function loadSnapshots(projectId: string) {
    snapshots.value = await api.listSnapshots(projectId)
  }

  async function loadScenes(projectId: string) {
    scenes.value = await api.listScenes(projectId)
  }

  /** 一次性刷新分支 + 场景 + 快照（决策结束后调用）。 */
  async function refreshAll(projectId: string) {
    await Promise.all([loadBranches(projectId), loadScenes(projectId), loadSnapshots(projectId)])
  }

  async function fork(projectId: string, snapshotId: string, name: string, conditions: Record<string, unknown>, notes: string) {
    await api.forkBranch(projectId, snapshotId, {
      branch_name: name,
      new_conditions: conditions,
      director_notes: notes,
    })
    await loadBranches(projectId)
  }

  return { branchTree, snapshots, scenes, loadBranches, loadSnapshots, loadScenes, refreshAll, fork }
})
