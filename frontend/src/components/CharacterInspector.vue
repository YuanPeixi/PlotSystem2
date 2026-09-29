<script setup lang="ts">
/**
 * 角色内部视图（导演视角，工单17）。
 * 数据来自 Inspection 层：状态与运行时记忆取自快照时点，不是角色卡的实时值。
 * embedded：嵌在导演台检查器里（左上角是返回）；否则是工作台的侧拉浮层。
 */
import { computed, ref, watch } from 'vue'
import { api } from '@/api/client'
import { useCharacterStore } from '@/stores/characters'
import Icon from '@/components/ui/Icon.vue'
import type { CharacterInspection } from '@/types'

const props = defineProps<{
  projectId: string
  characterId: string
  /** 给出时按该场景的时点解析状态（契约4 四级继承） */
  sceneId?: string
  embedded?: boolean
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const charStore = useCharacterStore()
const data = ref<CharacterInspection | null>(null)
const loading = ref(false)
const error = ref('')
const memoryQuery = ref('')
const searching = ref(false)
const memTab = ref<'short' | 'episodic' | 'long'>('episodic')

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

// 事件摘要按"一行一条"序列化，[重要] 前缀只是存储标记
const episodes = computed(() =>
  (data.value?.episodic_summary || '')
    .split('\n')
    .map((l) => l.replace(/^\[重要\]\s*/, '').trim())
    .filter(Boolean),
)
</script>

<template>
  <div :class="embedded ? 'embedded' : 'sheet-mask'" @click.self="!embedded && emit('close')">
    <aside class="inspector" :class="{ sheet: !embedded }" aria-label="角色内部状态">
      <header class="head">
        <button v-if="embedded" class="icon" title="返回" @click="emit('close')"><Icon name="back" /></button>
        <div class="title">
          <b>{{ data?.name || charStore.nameOf(characterId) }}</b>
          <span class="dim">角色内部状态</span>
        </div>
        <span class="spacer"></span>
        <button v-if="!embedded" class="icon" title="关闭" @click="emit('close')"><Icon name="close" /></button>
      </header>

      <div class="body">
        <p v-if="loading" class="dim">加载中</p>
        <p v-else-if="error" class="notice danger"><Icon name="alert" :size="15" />{{ error }}</p>

        <template v-else-if="data">
          <p class="source dim">
            <Icon name="snapshot" :size="14" />
            <template v-if="data.state_source === 'snapshot'">状态取自快照 {{ data.source_snapshot_id.slice(0, 8) }}</template>
            <template v-else>还没有快照，显示角色卡上的当前值</template>
          </p>

          <div class="section-title">当前状态</div>
          <dl class="kv">
            <dt>情绪</dt><dd>{{ data.current_emotion || '未知' }}</dd>
            <dt>目标</dt><dd>{{ data.current_goal || '无明确目标' }}</dd>
            <dt>位置</dt><dd>{{ data.current_location || '未知' }}</dd>
          </dl>

          <template v-if="data.known_facts.length">
            <div class="section-title">已知事实</div>
            <ul class="bullets">
              <li v-for="(f, i) in data.known_facts" :key="i">{{ f }}</li>
            </ul>
          </template>

          <template v-if="data.unknown_facts.length">
            <div class="section-title">未知事实</div>
            <p class="private-note"><Icon name="private" :size="14" />仅导演可见，不会进入{{ data.name }}的视野</p>
            <ul class="bullets private">
              <li v-for="(f, i) in data.unknown_facts" :key="i">{{ f }}</li>
            </ul>
          </template>

          <template v-if="relationEntries(data).length">
            <div class="section-title">关系</div>
            <ul class="rels">
              <li v-for="[cid, r] in relationEntries(data)" :key="cid" :title="r.notes">
                <span class="rel-name">{{ charStore.nameOf(r.target_character_id || cid) }}</span>
                <span class="dim rel-type">{{ r.relation_type }}</span>
                <span class="meter rel-bar"><i :style="{ width: Math.max(0, Math.min(1, r.strength)) * 100 + '%' }"></i></span>
              </li>
            </ul>
          </template>

          <div class="section-title">
            记忆
            <div class="seg" role="group" aria-label="记忆层">
              <button :aria-pressed="memTab === 'short'" @click="memTab = 'short'">短期</button>
              <button :aria-pressed="memTab === 'episodic'" @click="memTab = 'episodic'">事件摘要</button>
              <button :aria-pressed="memTab === 'long'" @click="memTab = 'long'">长期检索</button>
            </div>
          </div>
          <template v-if="memTab === 'short'">
            <ul v-if="data.short_term_buffer.length" class="memory">
              <li v-for="(m, i) in data.short_term_buffer" :key="i">{{ m }}</li>
            </ul>
            <p v-else class="dim small">缓冲为空：已固化进长期记忆，或这个角色还没参演。</p>
          </template>
          <template v-else-if="memTab === 'episodic'">
            <ul v-if="episodes.length" class="memory">
              <li v-for="(e, i) in episodes" :key="i">{{ e }}</li>
            </ul>
            <p v-else class="dim small">暂无事件摘要。</p>
          </template>
          <template v-else>
            <form class="search" @submit.prevent="load(memoryQuery)">
              <input v-model="memoryQuery" placeholder="输入检索词，如：与王子的冲突" />
              <button type="submit" :disabled="searching || !memoryQuery.trim()">
                <Icon name="search" :size="15" />{{ searching ? '检索中' : '检索' }}
              </button>
            </form>
            <p class="dim small">只在点检索时查询（会调用一次 embedding）。</p>
            <ul v-if="data.long_term_hits.length" class="memory">
              <li v-for="(h, i) in data.long_term_hits" :key="i">
                <span class="num dim">{{ h.score.toFixed(2) }}</span> {{ h.text }}
              </li>
            </ul>
          </template>

          <details v-if="data.persona || data.appearance || data.speech_style" class="more">
            <summary><Icon name="chevron-right" :size="15" class="caret" />人设与说话风格</summary>
            <p v-if="data.persona">{{ data.persona }}</p>
            <p v-if="data.appearance" class="dim">外貌：{{ data.appearance }}</p>
            <p v-if="data.speech_style" class="dim">说话风格：{{ data.speech_style }}</p>
          </details>
          <details v-if="data.world_lore_entries.length" class="more">
            <summary><Icon name="chevron-right" :size="15" class="caret" />可感知的世界观条目（{{ data.world_lore_entries.length }}）</summary>
            <ul class="bullets">
              <li v-for="l in data.world_lore_entries" :key="l.lore_id">{{ l.content }}</li>
            </ul>
          </details>
        </template>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.embedded {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}
.sheet-mask {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: rgba(0, 0, 0, 0.18);
}
.inspector {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}
.inspector.sheet {
  position: absolute;
  top: 8px;
  right: 8px;
  bottom: 8px;
  height: auto;
  width: min(400px, calc(100vw - 16px));
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--material);
  backdrop-filter: saturate(180%) blur(24px);
  -webkit-backdrop-filter: saturate(180%) blur(24px);
  box-shadow: var(--shadow-float);
  overflow: hidden;
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 52px;
  padding: 8px 10px 8px 8px;
  border-bottom: 1px solid var(--line);
}
.sheet .head {
  padding-left: 16px;
}
.title {
  display: flex;
  flex-direction: column;
  line-height: 1.35;
  min-width: 0;
}
.title b {
  font-size: 15px;
}
.title .dim {
  font-size: 12px;
}
.body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 16px;
}
.source {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  margin-bottom: 16px;
}
.small {
  font-size: 12.5px;
}
.kv {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 12px;
  font-size: 13px;
}
.kv dt {
  color: var(--ink-2);
}
.bullets {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.bullets li {
  display: flex;
  gap: 8px;
}
.bullets li::before {
  content: '';
  flex: none;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--ink-3);
  margin-top: 8px;
}
.bullets.private {
  margin-top: 8px;
}
.bullets.private li {
  color: var(--private);
}
.bullets.private li::before {
  background: var(--private);
}
.rels {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rels li {
  display: grid;
  grid-template-columns: 4em minmax(0, 1fr) 64px;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.rel-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rel-type {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rel-bar {
  width: 64px;
}
.section-title .seg button {
  height: 22px;
  font-size: 12px;
  font-weight: 400;
  padding: 0 8px;
}
.memory {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
  line-height: 1.65;
}
.memory li {
  padding-left: 10px;
  border-left: 2px solid var(--line);
}
.search {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}
.more {
  margin-top: 16px;
  border-top: 1px solid var(--line);
  padding-top: 12px;
  font-size: 13px;
  line-height: 1.7;
}
.more summary {
  list-style: none;
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-2);
  margin-bottom: 8px;
}
.more summary::-webkit-details-marker {
  display: none;
}
.caret {
  transition: transform 0.15s;
}
.more[open] .caret {
  transform: rotate(90deg);
}
.more p + p {
  margin-top: 6px;
}
</style>
