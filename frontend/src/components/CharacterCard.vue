<script setup lang="ts">
import Icon from '@/components/ui/Icon.vue'
import type { CharacterCard } from '@/types'

defineProps<{ character: CharacterCard; selected?: boolean }>()
defineEmits<{ (e: 'select', id: string): void; (e: 'inspect', id: string): void }>()
</script>

<template>
  <article class="char-card" :class="{ selected }">
    <header class="char-head">
      <div class="char-name">{{ character.name }}</div>
      <button class="icon sm" title="查看内部状态（导演视角）" @click="$emit('inspect', character.character_id)">
        <Icon name="search" :size="15" />
      </button>
    </header>
    <p class="char-state dim">
      {{ character.current_emotion || '情绪未知' }}<template v-if="character.current_goal">，{{ character.current_goal }}</template>
    </p>
    <p class="char-persona">{{ character.persona || '暂无人设。' }}</p>
  </article>
</template>

<style scoped>
.char-card {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--panel);
  transition: border-color 0.12s;
}
.char-card:hover,
.char-card.selected {
  border-color: var(--ink-3);
}
.char-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.char-name {
  flex: 1;
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.char-state {
  font-size: 12px;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.char-persona {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ink-2);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
