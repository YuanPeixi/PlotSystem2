import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { Branch, BranchTree, Snapshot } from '@/types'

export const useDirectorStore = defineStore('director', () => {
  const branchTree = ref<BranchTree>({ project_id: '', roots: [] })
  const snapshots = ref<Snapshot[]>([])
  const branchesLoading = ref(false)
  const snapshotsLoading = ref(false)
  const branchesError = ref('')
  const snapshotsError = ref('')

  async function loadBranches(projectId: string) {
    branchesLoading.value = true
    branchesError.value = ''
    try {
      branchTree.value = await api.getBranches(projectId)
    } catch (error) {
      branchesError.value = error instanceof Error ? error.message : '分支加载失败'
      throw error
    } finally {
      branchesLoading.value = false
    }
  }

  async function loadSnapshots(projectId: string) {
    snapshotsLoading.value = true
    snapshotsError.value = ''
    try {
      snapshots.value = await api.listSnapshots(projectId)
    } catch (error) {
      snapshotsError.value = error instanceof Error ? error.message : '快照加载失败'
      throw error
    } finally {
      snapshotsLoading.value = false
    }
  }

  async function fork(
    projectId: string,
    snapshotId: string,
    name: string,
    conditions: Record<string, unknown>,
    notes: string,
  ): Promise<Branch> {
    const branch = await api.forkBranch(projectId, snapshotId, {
      branch_name: name,
      new_conditions: conditions,
      director_notes: notes,
    })
    await Promise.all([loadBranches(projectId), loadSnapshots(projectId)])
    return branch
  }

  return {
    branchTree,
    snapshots,
    branchesLoading,
    snapshotsLoading,
    branchesError,
    snapshotsError,
    loadBranches,
    loadSnapshots,
    fork,
  }
})
