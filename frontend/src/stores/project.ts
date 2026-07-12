import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { BuildStatus, GraphData, Project } from '@/types'

const LAST_PROJECT_KEY = 'plotsystem.lastProjectId'

export function getLastProjectId(): string | null {
  return localStorage.getItem(LAST_PROJECT_KEY)
}

function setLastProjectId(id: string | null) {
  if (id) localStorage.setItem(LAST_PROJECT_KEY, id)
  else localStorage.removeItem(LAST_PROJECT_KEY)
}

export const useProjectStore = defineStore('project', () => {
  const projects = ref<Project[]>([])
  const current = ref<Project | null>(null)
  const graph = ref<GraphData>({ nodes: [], edges: [] })
  const buildStatus = ref<BuildStatus>({ stage: '未开始', progress: 0 })

  async function loadProjects() {
    projects.value = await api.listProjects()
  }

  async function createProject(name: string, description = '') {
    const p = await api.createProject(name, description)
    await loadProjects()
    return p
  }

  async function selectProject(id: string) {
    current.value = await api.getProject(id)
    setLastProjectId(id)
  }

  async function deleteProject(id: string) {
    await api.deleteProject(id)
    if (current.value?.project_id === id) {
      current.value = null
      setLastProjectId(null)
    }
    await loadProjects()
  }

  async function uploadSeed(id: string, file: File) {
    return api.uploadSeed(id, file)
  }

  async function build(id: string) {
    await api.build(id)
  }

  async function refreshBuildStatus(id: string) {
    buildStatus.value = await api.buildStatus(id)
    return buildStatus.value
  }

  async function loadGraph(id: string) {
    graph.value = await api.getGraph(id)
  }

  return {
    projects,
    current,
    graph,
    buildStatus,
    loadProjects,
    createProject,
    selectProject,
    deleteProject,
    uploadSeed,
    build,
    refreshBuildStatus,
    loadGraph,
  }
})
