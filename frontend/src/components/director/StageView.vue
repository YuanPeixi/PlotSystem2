<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import DialogLog from '@/components/DialogLog.vue'
import type { DialogueTurn, Scene } from '@/types'

const props = defineProps<{
  scene: Scene
  turns: DialogueTurn[]
  running: boolean
  statusText: string
  lastError: string
  /** pending / paused 且未在跑：可以开演或续跑 */
  resumable: boolean
  /** 已完成、未在跑、尚未决策：舞台底栏直接给出决策入口 */
  decidable: boolean
  /** 决策请求在途 */
  deciding: boolean
  nameOf: (cid: string) => string
}>()
const emit = defineEmits<{
  (e: 'inspect', cid: string): void
  (e: 'resume'): void
  (e: 'decide', type: 'continue' | 'next_scene'): void
  (e: 'open-decide'): void
}>()

const only = ref('')
const scroller = ref<HTMLElement | null>(null)
// 用户往上翻看时不把他拽回底部；停在底部附近才跟随新台词
const stick = ref(true)

const SPEAKER_MODE: Record<string, string> = { round_robin: '轮流发言', selector: '评分选人' }

const narration = computed(() => {
  const n = props.scene.initial_conditions?.opening_narration
  return (typeof n === 'string' && n.trim()) || props.scene.description || ''
})
// 初始条件对在场角色公开；开场白单独排成旁白，不在这里重复
const conditions = computed(() =>
  Object.entries(props.scene.initial_conditions || {})
    .filter(([k, v]) => k !== 'opening_narration' && v !== null && v !== '')
    .map(([k, v]) => [k, typeof v === 'boolean' ? (v ? '是' : '否') : String(v)] as const),
)
const speakers = computed(() => {
  const seen = new Map<string, string>()
  props.turns.forEach((t) => seen.set(t.character_id, t.character_name))
  return [...seen]
})
const progress = computed(() =>
  props.scene.max_turns ? Math.min(1, props.turns.length / props.scene.max_turns) : 0,
)

function onScroll() {
  const el = scroller.value
  if (el) stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}
async function toBottom(force = false) {
  await nextTick()
  const el = scroller.value
  if (el && (force || stick.value)) el.scrollTop = el.scrollHeight
}
watch(() => props.turns.length, () => toBottom())
watch(
  () => props.scene.scene_id,
  () => {
    only.value = ''
    stick.value = true
    void toBottom(true)
  },
  { immediate: true },
)
</script>

<template>
  <div class="stage">
    <div ref="scroller" class="stage-scroll" @scroll.passive="onScroll">
      <article class="script">
        <h2 class="slug">{{ scene.name || '未命名场景' }}</h2>
        <p v-if="scene.location" class="slug-sub">{{ scene.location }}</p>
        <div v-if="scene.participating_characters.length" class="cast">
          <button
            v-for="cid in scene.participating_characters"
            :key="cid"
            class="chip"
            :title="`查看${nameOf(cid)}的内部状态`"
            @click="emit('inspect', cid)"
          >
            {{ nameOf(cid) }}
          </button>
        </div>
        <dl v-if="conditions.length" class="conditions">
          <template v-for="[k, v] in conditions" :key="k">
            <dt>{{ k }}</dt>
            <dd>{{ v }}</dd>
          </template>
        </dl>
        <p v-if="narration" class="narration">{{ narration }}</p>
        <p v-if="lastError" class="notice danger error">
          <Icon name="alert" :size="15" />{{ lastError }}
        </p>
        <DialogLog :turns="turns" :running="running" :only="only" />
      </article>
    </div>

    <footer class="stage-bar">
      <button v-if="resumable" class="primary" @click="emit('resume')">
        <Icon name="play" :size="15" />{{ scene.status === 'paused' ? '继续这一场' : '开演' }}
      </button>
      <span class="status"><span class="status-dot" :class="running ? 'running' : scene.status"></span>{{ statusText }}</span>
      <span class="num turns">第 {{ turns.length }} / {{ scene.max_turns }} 轮</span>
      <span class="meter bar"><i :style="{ width: progress * 100 + '%' }"></i></span>
      <span class="spacer"></span>
      <span class="mode">{{ SPEAKER_MODE[scene.speaker_mode] || scene.speaker_mode }}</span>
      <select v-if="speakers.length > 1" v-model="only" class="filter" aria-label="只看某个角色">
        <option value="">全部角色</option>
        <option v-for="[id, name] in speakers" :key="id" :value="id">只看{{ name }}</option>
      </select>
      <!-- 快捷决策：与决策面板的默认提交等价；回滚要选快照、填条件，引导到决策页 -->
      <span v-if="decidable" class="decide">
        <button :disabled="deciding" title="同一场再演 6 轮" @click="emit('decide', 'continue')">
          <Icon name="continue" :size="15" />继续
        </button>
        <button :disabled="deciding" title="让导演规划并开演下一场" @click="emit('decide', 'next_scene')">
          <Icon name="next" :size="15" />下一场
        </button>
        <button :disabled="deciding" title="在决策面板里选择快照与新条件" @click="emit('open-decide')">
          <Icon name="rollback" :size="15" />回滚…
        </button>
      </span>
    </footer>
  </div>
</template>

<style scoped>
.stage {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}
.stage-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
.script {
  max-width: 720px;
  margin: 0 auto;
  padding: 36px 40px 24px;
}
.slug {
  font-family: var(--font-script);
  font-weight: 600;
  font-size: 24px;
  letter-spacing: 0.04em;
  line-height: 1.4;
}
.slug-sub {
  color: var(--ink-2);
  margin-top: 4px;
}
.cast {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}
.chip {
  height: 24px;
  padding: 0 8px;
  border: 0;
  border-radius: var(--r-xs);
  background: var(--hover);
  color: var(--ink-2);
  font-size: 12.5px;
}
.chip:hover {
  background: var(--hover);
  color: var(--ink);
  box-shadow: 0 0 0 1px var(--line-strong);
}
.conditions {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px 12px;
  margin-top: 12px;
  font-size: 13px;
}
.conditions dt {
  color: var(--ink-3);
}
.narration {
  font-family: var(--font-script);
  color: var(--ink-2);
  font-size: 15.5px;
  line-height: 1.9;
  margin-top: 20px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--line);
  white-space: pre-wrap;
}
.error {
  margin-top: 16px;
}
.stage-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 48px;
  padding: 8px 16px;
  border-top: 1px solid var(--line);
  background: var(--panel);
  color: var(--ink-2);
  font-size: 13px;
  flex-wrap: wrap;
}
.status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.bar {
  width: 120px;
}
.filter {
  width: auto;
  height: 28px;
  padding: 0 8px;
  font-size: 13px;
}
.decide {
  display: inline-flex;
  gap: 6px;
}
@container director (max-width: 860px) {
  .script {
    padding: 24px 20px;
  }
  .bar,
  .mode {
    display: none;
  }
}
</style>
