<script setup lang="ts">
import { computed, ref } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import type { AutoPilotSession } from '@/types'

const props = defineProps<{
  session: AutoPilotSession | null
  /** 这一场能否作为起点：已决策过的场景不行（后端 422） */
  canStart: boolean
  /** 开启/停止请求在途 */
  busy: boolean
}>()
const emit = defineEmits<{
  (e: 'start', steps: number, rollbacks: number): void
  (e: 'stop'): void
}>()

// 与 .env.example 的默认值一致；上下限以后端为准（越界返回 422）
const steps = ref(5)
const rollbacks = ref(2)
const configuring = ref(false)

const active = computed(() => props.session?.status === 'running')
const phaseText = computed(() =>
  props.session?.phase === 'deciding' ? '导演决策中' : '推演中',
)

function start() {
  configuring.value = false
  emit('start', steps.value, rollbacks.value)
}
</script>

<template>
  <span class="autopilot">
    <template v-if="active && session">
      <span class="live" :title="`最多 ${session.max_steps} 步，连续回滚上限 ${session.max_consecutive_rollbacks} 次`">
        <Icon :name="session.phase === 'deciding' ? 'spinner' : 'autopilot'" :size="15" />
        自动推演 · 第 {{ session.steps_taken }} / {{ session.max_steps }} 步 · {{ phaseText }}
      </span>
      <button :disabled="busy" title="不再自动往下接；正在演的这一场照常演完" @click="emit('stop')">
        <Icon name="pause" :size="15" />停止
      </button>
    </template>
    <template v-else-if="canStart">
      <button
        :disabled="busy"
        title="按导演的建议自动决策、连续推进，遇到结局/评估失败/回滚过多等情况会停下交还给你"
        @click="configuring = !configuring"
      >
        <Icon name="autopilot" :size="15" />自动推演…
      </button>
      <form v-if="configuring" class="config" @submit.prevent="start">
        <label>
          最多
          <input v-model.number="steps" type="number" min="1" max="20" required />
          步
        </label>
        <label>
          连续回滚上限
          <input v-model.number="rollbacks" type="number" min="0" max="20" required />
          次
        </label>
        <p class="dim hint">继续、下一场、回滚各算一步，每步都是一整场推演。每次回滚都会新建一条分支。</p>
        <div class="actions">
          <button type="button" class="ghost" @click="configuring = false">取消</button>
          <button type="submit" class="primary" :disabled="busy"><Icon name="autopilot" :size="15" />开始</button>
        </div>
      </form>
    </template>
  </span>
</template>

<style scoped>
.autopilot {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.live {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--spot);
  white-space: nowrap;
}
.config {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  z-index: 20;
  width: 260px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--panel);
  box-shadow: var(--shadow-float);
  display: flex;
  flex-direction: column;
  gap: 8px;
  color: var(--ink);
}
.config label {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-weight: 400;
}
.config input {
  width: 64px;
  height: 28px;
}
.hint {
  font-size: 12px;
  line-height: 1.6;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
