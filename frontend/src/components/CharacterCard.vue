<script setup lang="ts">
import type { CharacterCard } from '@/types'

defineProps<{ character: CharacterCard; selected?: boolean }>()
defineEmits<{ (e: 'select', id: string): void }>()
</script>

<template>
  <div class="char-card" :class="{ selected }" @click="$emit('select', character.character_id)">
    <div class="char-head">
      <div class="avatar">{{ character.name.slice(0, 1) }}</div>
      <div>
        <div class="char-name">{{ character.name }}</div>
        <div class="char-emotion dim">{{ character.current_emotion }} · {{ character.current_goal || '无明确目标' }}</div>
      </div>
    </div>
    <p class="char-persona">{{ character.persona || '（暂无设定）' }}</p>
    <div class="char-facts" v-if="character.known_facts.length">
      <span class="tag" v-for="(f, i) in character.known_facts.slice(0, 3)" :key="i">{{ f }}</span>
    </div>
  </div>
</template>

<style scoped>
.char-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px;
  cursor: pointer;
  transition: all 0.15s;
}
.char-card:hover {
  border-color: var(--highlight);
}
.char-card.selected {
  border-color: var(--highlight);
  box-shadow: 0 0 0 1px var(--highlight);
}
.char-head {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 10px;
}
.avatar {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  background: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 700;
}
.char-name {
  font-size: 16px;
  font-weight: 600;
}
.char-emotion {
  font-size: 12px;
}
.char-persona {
  font-size: 13px;
  color: var(--text-dim);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.char-facts {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
</style>
