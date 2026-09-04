<script setup lang="ts">
import { computed, ref } from 'vue'
import type { CharacterCard, SceneEvaluation, SnapshotMeta } from '@/types'

const props = defineProps<{
  evaluation: SceneEvaluation | null
  sceneId: string
  characters?: CharacterCard[]
  snapshots?: SnapshotMeta[]
  appliedDecision?: Record<string, unknown> | null
  pending?: boolean
}>()
const emit = defineEmits<{
  // done 回调由父组件在请求结束后调用：ok=true 时面板才关闭/清空表单，
  // 失败（409/网络错误/500）时保留用户已填写的内容供修正重试。
  (e: 'decision', payload: Record<string, unknown>, done?: (ok: boolean) => void): void
  (e: 'generate-output'): void
}>()

const rollbackConditions = ref('')
const rollbackSnapshotId = ref('')
const showRollback = ref(false)
const nextSceneGoal = ref('')
const showNextScene = ref(false)
// “下一场”人工可编辑覆盖项（均留空/不选时保持 AI 自动规划的结果，工单13）
const nextChars = ref<string[]>([])
const nextLocation = ref('')
const nextConditions = ref('')

const DECISION_LABEL: Record<string, string> = {
  continue: '继续本场',
  next_scene: '下一场',
  rollback: '回滚',
}

// 已生效的决策不可再提交（后端会 409）；刷新后从 GET /decision 恢复出来。
const decided = computed(() => (props.appliedDecision?.decision_type as string) || '')
const locked = computed(() => !!props.pending || !!decided.value)

// 后端在评估 JSON 解析失败时把四项分数置为 -1（工单04），不能当成正常低分展示
const evalFailed = computed(() => (props.evaluation?.narrative_goal_score ?? 0) < 0)

const scores = computed(() => {
  const e = props.evaluation
  if (!e || evalFailed.value) return []
  return [
    { label: '目标达成', value: e.narrative_goal_score, danger: e.narrative_goal_score < 4 },
    { label: '戏剧张力', value: e.dramatic_tension_score, danger: e.dramatic_tension_score < 3 },
    { label: '主线偏离', value: e.plot_deviation_score, danger: e.plot_deviation_score > 7 },
    { label: '角色一致', value: e.character_consistency_score, danger: e.character_consistency_score < 5 },
  ]
})

// 负值 = 本场未度量到推进度，不能当成“进度 0”画成空进度条
const progress = computed(() => {
  const v = props.evaluation?.story_progress ?? -1
  return v >= 0 ? v : null
})
const ending = computed(() => props.evaluation?.is_ending_reached === true)

function decide(type: string) {
  if (locked.value) return
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
  emit(
    'decision',
    {
      decision_type: 'rollback',
      // 留空时后端回退到本场的模拟前快照（scene.snapshot_id_before）
      rollback_snapshot_id: rollbackSnapshotId.value || null,
      new_initial_conditions: conditions,
    },
    (ok) => {
      if (!ok) return // 提交失败：保留表单内容
      showRollback.value = false
      rollbackConditions.value = ''
      rollbackSnapshotId.value = ''
    },
  )
}
</script>

<template>
  <div class="director-panel card">
    <h3>导演决策面板</h3>
    <div v-if="!evaluation" class="dim" style="margin: 16px 0">场景完成后将自动生成评估。</div>
    <template v-else>
      <p v-if="evalFailed" class="eval-failed">⚠ 本场评估未能生成（模型返回内容无法解析），评分不可用，请自行判断。</p>
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

      <div class="story-progress">
        <div class="score-label">
          <span>主线推进度</span>
          <span v-if="progress !== null">{{ Math.round(progress * 100) }}%</span>
          <span v-else class="dim">未评估</span>
        </div>
        <div v-if="progress !== null" class="bar">
          <div class="fill" :style="{ width: progress * 100 + '%' }"></div>
        </div>
        <p v-if="evaluation.progress_stalled" class="dim stalled">
          本场未推进主线（导演自评 {{ Math.round(evaluation.story_progress_raw * 100) }}%，不低于历史值才算推进）
        </p>
      </div>

      <div v-if="evaluation.unresolved_threads.length" class="threads">
        <div class="dim">未收束线索</div>
        <ul>
          <li v-for="t in evaluation.unresolved_threads" :key="t">{{ t }}</li>
        </ul>
      </div>

      <div v-if="ending" class="ending-box">
        <div class="ending-title">🏁 导演判定故事已抵达结局</div>
        <p v-if="evaluation.ending_reason" class="dim">{{ evaluation.ending_reason }}</p>
        <button @click="emit('generate-output')">✒ 生成结局输出</button>
        <p class="dim" style="font-size: 12px">不认同这个判定？下方三个决策依然可用。</p>
      </div>
    </template>

    <div class="actions">
      <button :disabled="locked" @click="decide('continue')">▶ 继续</button>
      <button :disabled="locked" @click="decide('next_scene')">⏭ 下一场</button>
      <button class="danger" :disabled="locked" @click="decide('rollback')">↩ 回滚</button>
    </div>
    <div v-if="decided" class="dim" style="margin-top: 8px; font-size: 12px">
      本场已做出决策：{{ DECISION_LABEL[decided] || decided }}。请在左侧场景列表选择后续场次。
    </div>
    <div v-if="pending" class="dim" style="margin-top: 8px; font-size: 12px">
      决策正在处理中，请勿重复提交...
    </div>

    <div v-if="showNextScene" class="rollback-box">
      <label>下一场意图（可不填，导演自动接续）</label>
      <textarea v-model="nextSceneGoal" placeholder="例：两人在业余中和解，或新冲突将起"></textarea>
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
      <label>回滚到哪份快照（留空 = 本场开始前）</label>
      <select v-model="rollbackSnapshotId">
        <option value="">本场开始前的快照</option>
        <option v-for="s in props.snapshots || []" :key="s.snapshot_id" :value="s.snapshot_id">
          {{ s.label || s.snapshot_id.slice(0, 8) }}
        </option>
      </select>
      <label style="margin-top: 8px">新初始条件（JSON 或文本）</label>
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
.eval-failed {
  font-size: 12px;
  color: #e94560;
  margin: 10px 0 0;
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
.story-progress {
  margin: 4px 0 12px;
}
.stalled {
  font-size: 12px;
  margin-top: 6px;
}
.threads {
  font-size: 12px;
  margin-bottom: 12px;
}
.threads ul {
  list-style: none;
  margin-top: 4px;
}
.threads li::before {
  content: '· ';
}
.ending-box {
  border: 1px solid var(--highlight);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.ending-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.ending-box button {
  margin: 8px 0;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
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
