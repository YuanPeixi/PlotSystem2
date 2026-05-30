<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { DialogueTurn } from '@/types'

const props = defineProps<{ turns: DialogueTurn[] }>()

const filter = ref<string>('')
const scroller = ref<HTMLDivElement | null>(null)

const characters = computed(() => {
  const set = new Map<string, string>()
  props.turns.forEach((t) => set.set(t.character_id, t.character_name))
  return Array.from(set, ([id, name]) => ({ id, name }))
})

const filtered = computed(() =>
  filter.value ? props.turns.filter((t) => t.character_id === filter.value) : props.turns,
)

watch(
  () => props.turns.length,
  async () => {
    await nextTick()
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
  },
)
</script>

<template>
  <div class="dialog-log">
    <div class="log-toolbar">
      <span class="dim">对话日志（{{ turns.length }}）</span>
      <select v-model="filter" style="width: auto">
        <option value="">全部角色</option>
        <option v-for="c in characters" :key="c.id" :value="c.id">{{ c.name }}</option>
      </select>
    </div>
    <div ref="scroller" class="log-body">
      <div v-if="!filtered.length" class="dim empty">等待对话生成...</div>
      <div v-for="t in filtered" :key="t.turn_id" class="turn">
        <div class="turn-head">
          <span class="speaker">{{ t.character_name }}</span>
          <span class="dim">#{{ t.turn_number }}</span>
        </div>
        <div v-if="t.action" class="action">*{{ t.action }}*</div>
        <div v-if="t.dialogue" class="dialogue">{{ t.dialogue }}</div>
        <div v-if="t.inner_thought" class="thought">[{{ t.inner_thought }}]</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dialog-log {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.log-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.log-body {
  flex: 1;
  overflow-y: auto;
  padding-right: 6px;
}
.empty {
  text-align: center;
  padding: 40px 0;
}
.turn {
  border-left: 2px solid var(--accent);
  padding: 8px 0 8px 12px;
  margin-bottom: 12px;
}
.turn-head {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-bottom: 4px;
}
.speaker {
  font-weight: 600;
  color: var(--highlight);
}
.action {
  color: #f0a020;
  font-style: italic;
}
.dialogue {
  color: var(--text);
}
.thought {
  color: var(--text-dim);
  font-style: italic;
}
</style>
