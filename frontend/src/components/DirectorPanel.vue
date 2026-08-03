<script setup lang="ts">
import { computed, ref } from 'vue'
import type { CharacterCard, DirectorDecision, SceneEvaluation } from '@/types'

const props = defineProps<{
  evaluation: SceneEvaluation | null
  sceneId: string
  characters?: CharacterCard[]
  pending?: boolean
  decision?: DirectorDecision | null
  readonly?: boolean
}>()
const emit = defineEmits<{
  // done 回调由父组件在请求结束后调用：ok=true 时面板才关闭/清空表单，
  // 失败（409/网络错误/500）时保留用户已填写的内容供修正重试。
  (e: 'decision', payload: Record<string, unknown>, done?: (ok: boolean) => void): void
  (e: 'openScene', sceneId: string): void
}>()

const rollbackConditions = ref('')
const showRollback = ref(false)
const nextSceneGoal = ref('')
const showNextScene = ref(false)
// “下一场”人工可编辑覆盖项（均留空/不选时保持 AI 自动规划的结果，工单13）
const nextChars = ref<string[]>([])
const nextLocation = ref('')
const nextConditions = ref('')

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
  if (props.pending) return
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

function applyRecommendation() {
  const type = props.evaluation?.recommended_decision
  if (!type || props.pending || props.readonly) return
  if (type === 'continue') {
    emit('decision', { decision_type: type, extra_turns: 6 })
  } else if (type === 'next_scene') {
    emit('decision', { decision_type: type })
  } else if (type === 'rollback') {
    emit('decision', {
      decision_type: type,
      new_initial_conditions: props.evaluation?.rollback_suggestion || {},
    })
  }
}

function confirmNextScene() {
  let conditions: Record<string, unknown> | null = null
  if (nextConditions.value.trim()) {
    try {
      conditions = JSON.parse(nextConditions.value)
    } catch {
      conditions = { note: nextConditions.value }
    }
  }
  emit(
    'decision',
    {
      decision_type: 'next_scene',
      next_scene_description: nextSceneGoal.value.trim() || null,
      next_participating_characters: nextChars.value.length ? nextChars.value : null,
      next_location: nextLocation.value.trim() || null,
      next_initial_conditions: conditions,
    },
    (ok) => {
      if (!ok) return // 提交失败：保留表单内容，用户可修正后重试
      showNextScene.value = false
      nextSceneGoal.value = ''
      nextChars.value = []
      nextLocation.value = ''
      nextConditions.value = ''
    },
  )
}

function confirmRollback() {
  let conditions: Record<string, unknown> = {}
  try {
    conditions = rollbackConditions.value ? JSON.parse(rollbackConditions.value) : {}
  } catch {
    conditions = { note: rollbackConditions.value }
  }
  emit('decision', { decision_type: 'rollback', new_initial_conditions: conditions }, (ok) => {
    if (!ok) return // 提交失败：保留表单内容
    showRollback.value = false
    rollbackConditions.value = ''
  })
}
</script>

<template>
  <div class="director-panel card">
    <h3>导演决策面板</h3>
    <div v-if="decision" class="decision-result">
      <strong>决策已生效</strong>
      <span>{{ decision.decision_type }}</span>
      <button
        v-if="decision.next_scene_id"
        class="decision-target"
        title="打开目标场景"
        @click="$emit('openScene', decision.next_scene_id)"
      >
        打开目标场景
      </button>
    </div>
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

    <div v-if="evaluation && !readonly" class="actions">
      <button :disabled="pending" @click="decide('continue')">▶ 继续</button>
      <button :disabled="pending" @click="decide('next_scene')">⏭ 下一场</button>
      <button class="danger" :disabled="pending" @click="decide('rollback')">↩ 回滚</button>
    </div>
    <button
      v-if="evaluation && !readonly"
      class="auto-decision"
      :disabled="pending"
      title="仅执行当前评估的建议一次，不会连续运行后续场景"
      @click="applyRecommendation"
    >
      执行 AI 建议
    </button>
    <div v-if="pending" class="dim" style="margin-top: 8px; font-size: 12px">
      决策正在处理中，请勿重复提交...
    </div>

    <div v-if="showNextScene" class="rollback-box">
      <label>下一场叙事目标（可不填，导演自动接续）</label>
      <textarea v-model="nextSceneGoal" placeholder="例：两人在業余中和解，或新冲突将起"></textarea>
      <label style="margin-top: 8px">参与角色（不选则由导演自动决定）</label>
      <div class="char-pills">
        <label v-for="c in characters || []" :key="c.character_id" class="check-pill">
          <input type="checkbox" :value="c.character_id" v-model="nextChars" />
          {{ c.name }}
        </label>
      </div>
      <label style="margin-top: 8px">地点（留空则由导演自动决定）</label>
      <input v-model="nextLocation" placeholder="例：雨夜的酒馆" />
      <label style="margin-top: 8px">初始条件/环境变量（JSON，留空则由导演自动决定）</label>
      <textarea v-model="nextConditions" placeholder='{"weather": "storm"}'></textarea>
      <div class="row" style="margin-top: 8px">
        <button :disabled="pending" @click="confirmNextScene">确认下一场</button>
        <button class="ghost" @click="showNextScene = false">取消</button>
      </div>
    </div>

    <div v-if="showRollback" class="rollback-box">
      <label>新初始条件（JSON 或文本）</label>
      <textarea v-model="rollbackConditions" placeholder='{"tension": "高", "note": "让对话更激烈"}'></textarea>
      <div class="row" style="margin-top: 8px">
        <button class="danger" :disabled="pending" @click="confirmRollback">确认回滚</button>
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
  color: #ffffff;
}
.fill.danger {
  background: var(--highlight);
}
.recommend {
  margin: 14px 0;
  font-size: 13px;
}
.decision-result {
  display: grid;
  gap: 4px;
  margin: 12px 0;
  padding: 10px;
  border-left: 3px solid #3ec46d;
  background: var(--bg);
  color: var(--text-dim);
  font-size: 12px;
}
.decision-result strong {
  color: var(--text);
}
.decision-target {
  justify-self: start;
  padding: 4px 8px;
  font-size: 12px;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.auto-decision {
  margin-top: 8px;
}
.rollback-box {
  margin-top: 14px;
}
.rollback-box label {
  display: block;
  font-size: 12px;
  margin-bottom: 4px;
}
.char-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.check-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  background: var(--bg);
  border-radius: 12px;
  padding: 2px 8px;
}
</style>
