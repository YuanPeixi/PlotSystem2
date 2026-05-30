import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { CharacterCard } from '@/types'

export const useCharacterStore = defineStore('characters', () => {
  const characters = ref<CharacterCard[]>([])

  async function load(projectId: string) {
    characters.value = await api.listCharacters(projectId)
  }

  async function update(projectId: string, cid: string, patch: Partial<CharacterCard>) {
    const updated = await api.updateCharacter(projectId, cid, patch)
    const idx = characters.value.findIndex((c) => c.character_id === cid)
    if (idx >= 0) characters.value[idx] = updated
    return updated
  }

  function nameOf(cid: string): string {
    return characters.value.find((c) => c.character_id === cid)?.name ?? cid
  }

  return { characters, load, update, nameOf }
})
