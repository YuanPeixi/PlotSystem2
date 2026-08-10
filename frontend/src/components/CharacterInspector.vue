<script setup lang="ts">
/**
 * 角色内部视图（导演视角，工单17）。
 * 数据来自 Inspection 层：状态与运行时记忆取自快照时点，不是角色卡的实时值。
 */
import { ref, watch } from 'vue'
import { api } from '@/api/client'
import { useCharacterStore } from '@/stores/characters'
import type { CharacterInspection } from '@/types'

const props = defineProps<{
  projectId: string
  characterId: string
  /** 给出时按该场景的时点解析状态（契约4 四级继承） */
  sceneId?: string
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const charStore = useCharacterStore()
const data = ref<CharacterInspection | null>(null)
const loading = ref(false)
const error = ref('')
const memoryQuery = ref('')
const searching = ref(false)

async function load(query = '') {
  loading.value = !query
  searching.value = !!query
  error.value = ''
  try {
    data.value = await api.inspectCharacter(props.projectId, props.characterId, {
      scene_id: props.sceneId || '',
      query,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
    searching.value = false
  }
}

watch(
  () => [props.characterId, props.sceneId],
  () => load(),
  { immediate: true },
)

function relationEntries(d: CharacterInspection) {
  return Object.entries(d.relationships || {})
}
</script>

<template>
  <div class="inspector-mask" @click.self="emit('close')">
    <aside class="inspector">
      <header class="row" style="justify-content: space-between">
        <h3>{{ data?.name || '角色详情' }}</h3>
        <button class="ghost" @click="emit('close')">✕</button>
      </header>

      <p v-if="loading" class="dim">加载中…</p>
      <p v-else-if="error" class="dim err">{{ error }}</p>

      <div v-else-if="data" class="body">
        <p class="dim source">
          状态时点：
          <template v-if="data.state_source === 'snapshot'">
            快照 {{ data.source_snapshot_id.slice(0, 8) }}
          </template>
          <template v-else>尚无快照，显示角色卡当前值</template>
        </p>

        <section>
          <div class="stat-row">
            <span class="tag">情绪：{{ data.current_emotion }}</span>
            <span class="tag">位置：{{ data.current_location || '未知' }}</span>
          </div>
          <p class="dim">目标：{{ data.current_goal || '无明确目标' }}</p>
        </section>

        <section v-if="data.persona || data.appearance || data.speech_style">
          <h4>人设</h4>
          <p>{{ data.persona }}</p>
          <p class="dim" v-if="data.appearance">外貌：{{ data.appearance }}</p>
          <p class="dim" v-if="data.speech_style">说话风格：{{ data.speech_style }}</p>
        </section>

        <section v-if="data.known_facts.length">
          <h4>已知信息</h4>
          <ul class="facts">
            <li v-for="(f, i) in data.known_facts" :key="i">{{ f }}</li>
          </ul>
        </section>

        <section v-if="data.unknown_facts.length">
          <h4 class="private">未知信息 · 仅导演可见</h4>
          <ul class="facts private-list">
            <li v-for="(f, i) in data.unknown_facts" :key="i">{{ f }}</li>
          </ul>
        </section>

        <section v-if="relationEntries(data).length">
          <h4>关系</h4>
          <ul class="facts">
            <li v-for="([cid, r]) in relationEntries(data)" :key="cid">
              {{ charStore.nameOf(r.target_character_id || cid) }} ·
              {{ r.relation_type }}（{{ r.strength.toFixed(2) }}）
              <span class="dim" v-if="r.notes">— {{ r.notes }}</span>
            </li>
          </ul>
        </section>

        <section>
          <h4>短期记忆（{{ data.short_term_buffer.length }}）</h4>
          <ul class="facts scroll" v-if="data.short_term_buffer.length">
            <li v-for="(m, i) in data.short_term_buffer" :key="i">{{ m }}</li>
          </ul>
          <p class="dim" v-else>缓冲为空（已固化进长期记忆，或该角色尚未参演）。</p>
        </section>

        <section>
          <h4>事件摘要</h4>
          <p class="dim pre">{{ data.episodic_summary || '（暂无）' }}</p>
        </section>

        <section>
          <h4>长期记忆检索</h4>
          <div class="row">
            <input v-model="memoryQuery" placeholder="输入检索词，如：与王子的冲突" @keyup.enter="load(memoryQuery)" />
            <button class="ghost" :disabled="searching || !memoryQuery.trim()" @click="load(memoryQuery)">
              {{ searching ? '检索中…' : '检索' }}
            </button>
          </div>
          <ul class="facts scroll" v-if="data.long_term_hits.length">
            <li v-for="(h, i) in data.long_term_hits" :key="i">
              <span class="dim">{{ h.score.toFixed(2) }}</span> {{ h.text }}
            </li>
          </ul>
        </section>

        <section v-if="data.world_lore_entries.length">
          <h4>可感知世界观条目</h4>
          <ul class="facts scroll">
            <li v-for="l in data.world_lore_entries" :key="l.lore_id">{{ l.content }}</li>
          </ul>
        </section>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.inspector-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  z-index: 40;
}
.inspector {
  position: absolute;
  top: 0;
  right: 0;
  height: 100%;
  width: 420px;
  max-width: 92vw;
  background: var(--card);
  border-left: 1px solid var(--border);
  padding: 18px;
  overflow-y: auto;
}
.body {
  display: flex;
  flex-direction: column;
  gap: 16px;
  margin-top: 12px;
}
.source {
  font-size: 12px;
}
h4 {
  font-size: 13px;
  margin-bottom: 6px;
}
h4.private {
  color: var(--highlight);
}
.stat-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.facts {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.facts li {
  background: var(--accent);
  border-radius: 6px;
  padding: 6px 8px;
}
.private-list li {
  background: transparent;
  border: 1px dashed var(--highlight);
}
.scroll {
  max-height: 220px;
  overflow-y: auto;
}
.pre {
  white-space: pre-wrap;
  font-size: 13px;
}
.err {
  color: var(--highlight);
}
</style>
