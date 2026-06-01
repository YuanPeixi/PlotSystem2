<script setup lang="ts">
import { computed, ref } from 'vue'
import type { SceneEvaluation } from '@/types'

const props = defineProps<{ evaluation: SceneEvaluation | null; sceneId: string }>()
const emit = defineEmits<{
  (e: 'decision', payload: Record<string, unknown>): void
}>()

const rollbackConditions = ref('')
const showRollback = ref(false)
const nextSceneGoal = ref('')
const showNextScene = ref(false)

const scores = computed(() => {
  const e = props.evaluation
  if (!e) return []
  return [
    { label: '目标达成', value: e.narrative_goal_score, danger: e.narrative_goal_score < 4 },
    { label: '戏剧张力', value: e.dramatic_tension_score, danger: e.dramatic_tension_score < 3 },
    { label: '主线偏离', value: e.plot_deviation_score, danger: e.plot_deviation_score > 7 },
    { label: '角色一致', value: e.character_consistency_score, danger: e.character_consistency_score < 5 },
  ]
})

function decide(type: string) {
  if (type === 'rollback') {
    showRollback.value = false
    showNextScene.value = false
    showRollback.value = true
    return
  }
  if (type === 'next_scene') {
    showRollback.value = false
    showNextScene.value = true
    return
  }
  emit('decision', { decision_type: type, extra_turns: type === 'continue' ? 6 : null })
}

function confirmNextScene() {
  emit('decision', {
    decision_type: 'next_scene',
    next_scene_description: nextSceneGoal.value.trim() || null,
  })
  showNextScene.value = false
  nextSceneGoal.value = ''
}

function confirmRollback() {
  let conditions: Record<string, unknown> = {}
  try {
    conditions = rollbackConditions.value ? JSON.parse(rollbackConditions.value) : {}
  } catch {
    conditions = { note: rollbackConditions.value }
  }
  emit('decision', { decision_type: 'rollback', new_initial_conditions: conditions })
  showRollback.value = false
}
</script>

<template>
  <div class="director-panel card">
    <h3>导演决策面板</h3>
    <div v-if="!evaluation" class="dim" style="margin: 16px 0">场景完成后将自动生成评估。</div>
    <template v-else>
      <p class="synopsis">{{ evaluation.synopsis }}</p>
      <div class="scores">
        <div v-for="s in scores" :key="s.label" class="score-bar">
          <div class="score-label">
            <span>{{ s.label }}</span>
            <span :class="{ danger: s.danger }">{{ s.value.toFixed(1) }}</span>
          </div>
          <div class="bar">
            <div class="fill" :class="{ danger: s.danger }" :style="{ width: s.value * 10 + '%' }"></div>
          </div>
        </div>
      </div>
      <div class="recommend dim">AI 建议：{{ evaluation.recommended_decision }}</div>
    </template>

    <div class="actions">
      <button @click="decide('continue')">▶ 继续</button>
      <button @click="decide('next_scene')">⏭ 下一场</button>
      <button class="warn" @click="decide('rollback')">↩ 回滚</button>
    </div>

    <div v-if="showNextScene" class="rollback-box">
      <label>下一场叙事目标（可不填，导演自动接续）</label>
      <textarea v-model="nextSceneGoal" placeholder="例：两人在業余中和解，或新冲突将起"></textarea>
      <div class="row" style="margin-top: 8px">
        <button @click="confirmNextScene">确认下一场</button>
        <button class="ghost" @click="showNextScene = false">取消</button>
      </div>
    </div>

    <div v-if="showRollback" class="rollback-box">
      <label>新初始条件（JSON 或文本）</label>
      <textarea v-model="rollbackConditions" placeholder='{"tension": "高", "note": "让对话更激烈"}'></textarea>
      <div class="row" style="margin-top: 8px">
        <button class="warn" @click="confirmRollback">确认回滚</button>
        <button class="ghost" @click="showRollback = false">取消</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.director-panel {
  display: flex;
  flex-direction: column;
}
.synopsis {
  font-size: 13px;
  margin: 10px 0 16px;
}
.scores {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.score-label {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  margin-bottom: 4px;
}
.bar {
  height: 6px;
  background: var(--bg);
  border-radius: 3px;
  overflow: hidden;
}
.fill {
  height: 100%;
  background: #3ec46d;
}
.fill.danger,
.danger {
  color: var(--highlight);
}
.fill.danger {
  background: var(--highlight);
}
.recommend {
  margin: 14px 0;
  font-size: 13px;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.rollback-box {
  margin-top: 14px;
}
</style>
