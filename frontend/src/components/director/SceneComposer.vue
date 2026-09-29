<script setup lang="ts">
/**
 * 舞台的"开演前"状态：先写本场意图请导演规划，拿到草稿后就地修改再开演。
 * 草稿尚未落库（SceneConfig），改动只作用于这一场；主线目标是只读锚点，只展示不编辑。
 */
import { ref, watch } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import type { CharacterCard, SceneConfig } from '@/types'

const props = defineProps<{
  draft: SceneConfig | null
  intent: string
  planning: boolean
  /** 另一场正在推演时不能开演 */
  busy: boolean
  goal: string
  characters: CharacterCard[]
}>()
const emit = defineEmits<{
  (e: 'update:intent', v: string): void
  (e: 'plan'): void
  (e: 'start'): void
  (e: 'cancel'): void
}>()

interface Row {
  k: string
  v: unknown
}
const rows = ref<Row[]>([])
const goalOpen = ref(false)
watch(
  () => props.draft,
  (d) => {
    rows.value = Object.entries(d?.initial_conditions || {}).map(([k, v]) => ({ k, v }))
  },
  { immediate: true },
)
// 未改动的值原样写回（可能不是字符串），改过的才是输入框里的文本
function writeBack() {
  if (!props.draft) return
  props.draft.initial_conditions = Object.fromEntries(
    rows.value.filter((r) => r.k.trim()).map((r) => [r.k.trim(), r.v]),
  )
}
function setRow(i: number, field: 'k' | 'v', value: string) {
  rows.value[i][field] = value
  writeBack()
}
function addRow() {
  rows.value.push({ k: '', v: '' })
}
function removeRow(i: number) {
  rows.value.splice(i, 1)
  writeBack()
}

function toggleCast(cid: string) {
  if (!props.draft) return
  const list = props.draft.participating_characters
  const i = list.indexOf(cid)
  if (i >= 0) list.splice(i, 1)
  else list.push(cid)
}
</script>

<template>
  <div class="composer">
    <div class="script">
      <!-- 第一步：本场意图 -->
      <template v-if="!draft">
        <h2 class="slug">规划下一场</h2>
        <div class="goal">
          <span class="dim">主线目标</span>
          <div>
            <p class="goal-text" :class="{ clamped: !goalOpen }">
              {{ goal || '尚未设定。可以在工作台填写；不填也能规划，但导演会自由发挥。' }}
            </p>
            <button v-if="goal.length > 120" class="ghost toggle" @click="goalOpen = !goalOpen">
              {{ goalOpen ? '收起' : '展开全文' }}
            </button>
          </div>
        </div>
        <div class="field">
          <label for="scene-intent">本场意图（可留空）</label>
          <textarea
            id="scene-intent"
            :value="intent"
            rows="3"
            placeholder="例如：让两位主角在雨夜的酒馆里第一次正面冲突"
            @input="emit('update:intent', ($event.target as HTMLTextAreaElement).value)"
          ></textarea>
        </div>
      </template>

      <!-- 第二步：就地修改导演的草稿 -->
      <form v-else @submit.prevent>
        <p class="note dim"><Icon name="director" :size="15" />导演已规划本场。开演前可以修改，改动只作用于这一场。</p>
        <input v-model="draft.name" class="slug-input" aria-label="场景名" placeholder="场景名" />
        <input v-model="draft.location" class="sub-input" aria-label="地点" placeholder="地点与时间" />

        <div class="field">
          <label>在场角色</label>
          <div class="cast">
            <button
              v-for="c in characters"
              :key="c.character_id"
              type="button"
              class="chip"
              :aria-pressed="draft.participating_characters.includes(c.character_id)"
              @click="toggleCast(c.character_id)"
            >
              {{ c.name }}
            </button>
          </div>
        </div>
        <div class="field">
          <label for="draft-desc">本场描述</label>
          <textarea id="draft-desc" v-model="draft.description" rows="2"></textarea>
        </div>
        <div class="field">
          <label for="draft-opening">开场白</label>
          <textarea id="draft-opening" v-model="draft.opening_narration" rows="3" class="script-font opening"></textarea>
        </div>

        <details class="more">
          <summary>
            <Icon name="chevron-right" :size="15" class="caret" />更多设定
            <span class="dim summary-hint">
              初始条件 {{ rows.filter((r) => r.k.trim()).length }} 条，{{
                draft.speaker_mode === 'selector' ? '评分选人' : '轮流发言'
              }}，最多 {{ draft.max_turns }} 轮
            </span>
          </summary>
          <div class="field">
            <label>初始条件（只作用于本场，会公开给在场角色）</label>
            <div class="kv">
              <template v-for="(r, i) in rows" :key="i">
                <input :value="r.k" placeholder="名称" @input="setRow(i, 'k', ($event.target as HTMLInputElement).value)" />
                <input :value="String(r.v ?? '')" placeholder="内容" @input="setRow(i, 'v', ($event.target as HTMLInputElement).value)" />
                <button type="button" class="icon" title="删除这条" @click="removeRow(i)"><Icon name="close" :size="15" /></button>
              </template>
            </div>
            <button type="button" class="ghost add" @click="addRow"><Icon name="plus" :size="15" />添加条件</button>
          </div>
          <div class="field inline">
            <div>
              <label>发言方式</label>
              <div class="seg">
                <button type="button" :aria-pressed="draft.speaker_mode !== 'selector'" @click="draft.speaker_mode = 'round_robin'">轮流发言</button>
                <button type="button" :aria-pressed="draft.speaker_mode === 'selector'" @click="draft.speaker_mode = 'selector'">评分选人</button>
              </div>
            </div>
            <div>
              <label for="draft-turns">最多轮数</label>
              <input id="draft-turns" v-model.number="draft.max_turns" type="number" min="1" class="num turns" />
            </div>
          </div>
        </details>
      </form>
    </div>

    <footer class="stage-bar">
      <button class="ghost" @click="emit('cancel')">取消</button>
      <span class="spacer"></span>
      <button v-if="!draft" class="primary" :disabled="planning" @click="emit('plan')">
        <Icon name="director" :size="15" />{{ planning ? '导演规划中' : '让导演规划' }}
      </button>
      <template v-else>
        <button class="ghost" :disabled="planning" @click="emit('plan')">
          <Icon name="replan" :size="15" />{{ planning ? '规划中' : '让导演重新规划' }}
        </button>
        <button
          class="primary"
          :disabled="busy || !draft.participating_characters.length"
          :title="busy ? '另一场正在推演' : ''"
          @click="emit('start')"
        >
          <Icon name="play" :size="15" />开演
        </button>
      </template>
    </footer>
  </div>
</template>

<style scoped>
.composer {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.script {
  flex: 1;
  min-height: 0;
  overflow: auto;
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
  padding: 36px 40px 24px;
}
.slug {
  font-family: var(--font-script);
  font-weight: 600;
  font-size: 24px;
  letter-spacing: 0.04em;
  margin-bottom: 14px;
}
.goal {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 12px;
  font-size: 13.5px;
  padding: 10px 12px;
  border-radius: var(--r-sm);
  background: var(--hover);
  margin-bottom: 18px;
}
.goal-text {
  white-space: pre-wrap;
  line-height: 1.7;
}
.goal-text.clamped {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.toggle {
  height: 24px;
  padding: 0 6px;
  margin: 4px 0 0 -6px;
  font-size: 12.5px;
  color: var(--ink-2);
}
.note {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  margin-bottom: 16px;
}
.slug-input,
.sub-input {
  display: block;
  border: 0;
  border-bottom: 1px solid transparent;
  border-radius: 0;
  background: transparent;
  padding: 2px 0;
}
.slug-input {
  font-family: var(--font-script);
  font-weight: 600;
  font-size: 24px;
  letter-spacing: 0.04em;
}
.sub-input {
  color: var(--ink-2);
  margin: 2px 0 22px;
}
.slug-input:hover,
.sub-input:hover {
  border-bottom-color: var(--line-strong);
}
.slug-input:focus,
.sub-input:focus {
  box-shadow: none;
  border-bottom-color: var(--ink-2);
}
.cast {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  height: 26px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--r-xs);
  background: var(--hover);
  color: var(--ink-2);
  font-size: 13px;
}
.chip:hover {
  background: var(--hover);
  color: var(--ink);
}
.chip[aria-pressed='true'] {
  background: var(--ink);
  color: var(--panel);
}
.opening {
  font-size: 15.5px;
}
.more {
  border-top: 1px solid var(--line);
  padding-top: 12px;
}
.more summary {
  list-style: none;
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--ink-2);
  margin-bottom: 12px;
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
.summary-hint {
  font-weight: 400;
  margin-left: 6px;
}
.kv {
  display: grid;
  grid-template-columns: 140px 1fr 30px;
  gap: 6px;
  align-items: center;
}
.add {
  margin-top: 6px;
}
.inline {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
}
.turns {
  width: 96px;
}
.stage-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 48px;
  padding: 8px 16px;
  border-top: 1px solid var(--line);
  background: var(--panel);
}
@container director (max-width: 860px) {
  .script {
    padding: 24px 20px;
  }
  .kv {
    grid-template-columns: 1fr 1fr 30px;
  }
}
</style>
