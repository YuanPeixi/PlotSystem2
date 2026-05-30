import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { BranchTree } from '@/types'

export const useDirectorStore = defineStore('director', () => {
  const branchTree = ref<BranchTree>({ project_id: '', roots: [] })
  const snapshots = ref<any[]>([])

  async function loadBranches(projectId: string) {
    branchTree.value = await api.getBranches(projectId)
  }

  async function loadSnapshots(projectId: string) {
    snapshots.value = await api.listSnapshots(projectId)
  }

  async function fork(projectId: string, snapshotId: string, name: string, conditions: Record<string, unknown>, notes: string) {
    await api.forkBranch(projectId, snapshotId, {
      branch_name: name,
      new_conditions: conditions,
      director_notes: notes,
    })
    await loadBranches(projectId)
  }

  return { branchTree, snapshots, loadBranches, loadSnapshots, fork }
})
