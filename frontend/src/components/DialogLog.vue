<script setup lang="ts">
import { computed } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import type { DialogueTurn } from '@/types'

const props = defineProps<{
  turns: DialogueTurn[]
  /** 推演中：最新一轮打追光，并在末尾显示等待行 */
  running?: boolean
  /** 只看某个角色（character_id），空 = 全部 */
  only?: string
}>()

const shown = computed(() =>
  props.only ? props.turns.filter((t) => t.character_id === props.only) : props.turns,
)
const lastId = computed(() => props.turns[props.turns.length - 1]?.turn_id)

// 名字栏按本场最长的名字定宽：各轮共用一个宽度才能对齐，又不为短名字留白。
// 汉字按 1em、其他字符按 0.6em 估算，超过上限的名字折行。
const whoWidth = computed(() => {
  let widest = 2
  for (const t of props.turns) {
    let w = 0
    for (const ch of t.character_name || '') w += /[⺀-鿿＀-￯]/.test(ch) ? 1 : 0.6
    widest = Math.max(widest, w)
  }
  return `${Math.min(widest, 6) + 0.2}em`
})
</script>

<template>
  <div class="dialog-log" :style="{ '--who': whoWidth }">
    <section
      v-for="t in shown"
      :key="t.turn_id"
      class="turn"
      :class="{ live: running && t.turn_id === lastId }"
    >
      <span class="idx num">{{ t.turn_number }}</span>
      <div class="who">
        {{ t.character_name }}
        <small v-if="t.selector_notice" :title="`选人阶段降级：${t.selector_notice}`">降级选择</small>
      </div>
      <div class="body">
        <span v-if="t.action" class="act">（{{ t.action }}）</span>{{ t.dialogue }}
        <!-- 独白只给导演看：它不进入任何其他角色的上下文（契约1） -->
        <div v-if="t.inner_thought" class="inner">
          <span class="inner-tag"><Icon name="private" :size="13" />独白</span>{{ t.inner_thought }}
        </div>
      </div>
    </section>
    <div v-if="running" class="waiting">
      <span class="dots"><i></i><i></i><i></i></span>推演中
    </div>
    <p v-if="!shown.length && !running" class="empty dim">
      {{ only ? '这个角色在本场还没有发言。' : '本场还没有对白。' }}
    </p>
  </div>
</template>

<style scoped>
.turn {
  display: grid;
  grid-template-columns: 16px var(--who, 3.6em) minmax(0, 1fr);
  column-gap: 10px;
  padding: 14px 0;
  position: relative;
}
.idx {
  color: var(--ink-3);
  font-size: 11.5px;
  padding-top: 6px;
  text-align: right;
}
.who {
  font-weight: 600;
  padding-top: 3px;
  text-align: right;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.who small {
  display: block;
  font-weight: 400;
  font-size: 11px;
  color: var(--ink-3);
  white-space: nowrap;
}
.body {
  font-family: var(--font-script);
  font-size: 17px;
  line-height: 1.85;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
/* 暗底上衬线字发糊，正文略降亮度（scoped 只给最后一段加作用域，祖先选择器照常匹配 html） */
[data-theme='dark'] .body {
  color: #d4d6db;
}
.act {
  color: var(--ink-2);
}
.inner {
  margin-top: 8px;
  padding: 6px 12px;
  border-left: 2px dashed var(--private);
  background: var(--private-soft);
  border-radius: 0 var(--r-xs) var(--r-xs) 0;
  color: var(--private);
  font-size: 15px;
  line-height: 1.75;
}
.inner-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: var(--font-ui);
  font-size: 11.5px;
  margin-right: 6px;
  vertical-align: 1px;
}
/* 追光：推演中的最新一轮 */
.turn.live .who {
  color: var(--spot-ink);
}
.turn.live::before {
  content: '';
  position: absolute;
  inset: 4px -40px;
  background: radial-gradient(55% 85% at 6% 50%, var(--spot-soft), transparent 100%);
  pointer-events: none;
}
.waiting {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 0 10px 26px;
  color: var(--ink-3);
  font-size: 13px;
}
.dots {
  display: inline-flex;
  gap: 4px;
}
.dots i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--spot);
  animation: blink 1.2s infinite;
}
.dots i:nth-child(2) {
  animation-delay: 0.2s;
}
.dots i:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes blink {
  0%,
  80%,
  100% {
    opacity: 0.25;
  }
  40% {
    opacity: 1;
  }
}
.empty {
  padding: 32px 0;
  text-align: center;
}
@container director (max-width: 860px) {
  .turn {
    grid-template-columns: 0 var(--who, 3.6em) minmax(0, 1fr);
  }
  .idx {
    visibility: hidden;
  }
}
@container director (max-width: 520px) {
  .turn {
    grid-template-columns: minmax(0, 1fr);
  }
  .idx {
    display: none;
  }
  .who {
    text-align: left;
  }
  .who small {
    display: inline;
    margin-left: 6px;
  }
}
</style>
