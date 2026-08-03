import type { Scene } from '@/types'

export function resolveBranchId(requestedId: string, availableIds: string[]): string {
  return availableIds.includes(requestedId) ? requestedId : availableIds[0] || ''
}

export function resolveScene(requestedId: string, scenes: Scene[]): Scene | undefined {
  return scenes.find((scene) => scene.scene_id === requestedId) ?? scenes.at(-1)
}