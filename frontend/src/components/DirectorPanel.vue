<script setup lang="ts">
import { computed, ref } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import type { CharacterCard, SceneEvaluation, SnapshotMeta, WorldObject } from '@/types'

const props = defineProps<{
  evaluation: SceneEvaluation | null
  sceneId: string
  characters?: CharacterCard[]
  objects?: WorldObject[]
  snapshots?: SnapshotMeta[]
  appliedDecision?: Record<string, unknown> | null
  pending?: boolean
  /** 检查器里分成两个标签页：eval 只看评估，decide 只做决策；不传则两者都显示 */
  mode?: 'eval' | 'decide'
  /** 自动推演进行中：决策由后端按导演规则自动执行，这里不再接受人工提交 */
  piloting?: boolean
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
const nextObjects = ref<string[]>([])
const nextConditions = ref('')

const DECISION_LABEL: Record<string, string> = {
  continue: '继续本场',
  next_scene: '下一场',
  rollback: '回滚',
}

// 已生效的决策不可再提交（后端会 409）；刷新后从 GET /decision 恢复出来。
const decided = computed(() => (props.appliedDecision?.decision_type as string) || '')
// 已决策的来源：自动推演执行的决策要标出来，否则看不出是谁拍的板
const decidedByAuto = computed(() => props.appliedDecision?.source === 'auto')
const locked = computed(() => !!props.pending || !!decided.value || !!props.piloting)

// 后端在评估 JSON 解析失败时把四项分数置为 -1（工单04），不能当成正常低分展示
const evalFailed = computed(() => (props.evaluation?.narrative_goal_score ?? 0) < 0)
// 评估时没有主线目标：对照目标的两项分数只是噪声，不判红、不画推进度
const goalMissing = computed(() => props.evaluation?.goal_missing === true)

const scores = computed(() => {
  const e = props.evaluation
  if (!e || evalFailed.value) return []
  const free = goalMissing.value
  return [
    { label: '目标达成', value: e.narrative_goal_score, unanchored: free, danger: !free && e.narrative_goal_score < 4 },
    { label: '戏剧张力', value: e.dramatic_tension_score, unanchored: false, danger: e.dramatic_tension_score < 3 },
    { label: '主线偏离', value: e.plot_deviation_score, unanchored: free, danger: !free && e.plot_deviation_score > 7 },
    { label: '角色一致', value: e.character_consistency_score, unanchored: false, danger: e.character_consistency_score < 5 },
  ]
})

// 负值 = 本场未度量到推进度，不能当成“进度 0”画成空进度条
const progress = computed(() => {
  const v = props.evaluation?.story_progress ?? -1
  return v >= 0 && !goalMissing.value ? v : null
})
const ending = computed(() => props.evaluation?.is_ending_reached === true)
const showEval = computed(() => props.mode !== 'decide')
const showDecide = computed(() => props.mode !== 'eval')
// 世界变量增量：值为 null 表示该变量已被收束删除
const worldDelta = computed(() => Object.entries(props.evaluation?.world_state_delta || {}))

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
      next_objects_present: nextObjects.value.length ? nextObjects.value : null,
      next_initial_conditions: conditions,
    },
    (ok) => {
      if (!ok) return // 提交失败：保留表单内容，用户可修正后重试
      showNextScene.value = false
      nextSceneGoal.value = ''
      nextChars.value = []
      nextLocation.value = ''
      nextObjects.value = []
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
  <div class="director-panel">
    <!-- ============ 评估 ============ -->
    <template v-if="showEval">
      <p v-if="!evaluation" class="dim empty">本场结束后会自动生成评估。</p>
      <template v-else>
        <p v-if="evalFailed" class="notice danger">
          <Icon name="alert" :size="15" />本场评估没能生成（模型返回的内容无法解析），评分不可用，请自行判断。
        </p>
        <p v-else-if="goalMissing" class="notice">
          <Icon name="info" :size="15" />
          评估时项目还没有主线目标：「目标达成」「主线偏离」没有参照，仅供参考，也不度量主线推进度。可以在工作台补填主线目标。
        </p>
        <p v-if="ending" class="ending-flag"><Icon name="flag" :size="15" />导演判定故事已抵达结局</p>
        <p v-if="evaluation.synopsis" class="synopsis">{{ evaluation.synopsis }}</p>

        <div class="section-title">主线推进度</div>
        <template v-if="progress !== null">
          <div class="progress">
            <b class="num">{{ Math.round(progress * 100) }}%</b>
            <span v-if="evaluation.progress_stalled" class="dim">
              本场未推进（导演自评 {{ Math.round(evaluation.story_progress_raw * 100) }}%，不高于历史值）
            </span>
          </div>
          <span class="meter progress-bar"><i :style="{ width: progress * 100 + '%', background: 'var(--spot)' }"></i></span>
        </template>
        <p v-else class="dim small">{{ goalMissing ? '未设定主线目标，不度量。' : '本场没有度量到推进度。' }}</p>

        <template v-if="scores.length">
          <div class="section-title">四维评分</div>
          <div class="scores">
            <div v-for="s in scores" :key="s.label" class="score" :class="{ unanchored: s.unanchored }">
              <span class="score-name">
                {{ s.label }}
                <small v-if="s.label === '主线偏离'">越低越好</small>
                <small v-if="s.unanchored">无锚点</small>
              </span>
              <span class="score-val num" :class="{ danger: s.danger }">{{ s.value.toFixed(1) }}</span>
              <span class="meter"><i :class="{ danger: s.danger }" :style="{ width: s.value * 10 + '%' }"></i></span>
            </div>
          </div>
        </template>

        <template v-if="evaluation.unresolved_threads.length">
          <div class="section-title">未收束线索</div>
          <ul class="bullets">
            <li v-for="t in evaluation.unresolved_threads" :key="t">{{ t }}</li>
          </ul>
        </template>

        <template v-if="worldDelta.length">
          <div class="section-title">世界变量变化</div>
          <dl class="kv">
            <template v-for="[k, v] in worldDelta" :key="k">
              <dt>{{ k }}</dt>
              <dd :class="v === null ? 'removed' : 'changed'">{{ v === null ? '已收束' : v }}</dd>
            </template>
          </dl>
        </template>
      </template>
    </template>

    <!-- ============ 决策 ============ -->
    <template v-if="showDecide">
      <div v-if="ending && evaluation" class="ending-box">
        <div class="ending-title"><Icon name="flag" :size="15" />导演判定故事已抵达结局</div>
        <p v-if="evaluation.ending_reason" class="dim small">{{ evaluation.ending_reason }}</p>
        <button class="primary" @click="emit('generate-output')"><Icon name="output" :size="15" />生成结局输出</button>
        <p class="dim small">不认同这个判定？下面三个决策依然可用。</p>
      </div>

      <p v-if="!evaluation && !decided" class="dim small">本场结束、评估生成后即可决策。</p>
      <p v-else-if="evaluation && !evalFailed" class="recommend">
        导演建议：<b>{{ DECISION_LABEL[evaluation.recommended_decision] || evaluation.recommended_decision }}</b>
      </p>

      <div class="decide">
        <button :disabled="locked" @click="decide('continue')">
          <Icon name="continue" :size="15" />继续本场<small>加演 6 轮</small>
        </button>
        <button :disabled="locked" @click="decide('next_scene')">
          <Icon name="next" :size="15" />进入下一场<small>导演规划</small>
        </button>
        <button class="danger" :disabled="locked" @click="decide('rollback')">
          <Icon name="rollback" :size="15" />回滚重演<small>新建分支</small>
        </button>
      </div>
      <p v-if="decided" class="notice">
        <Icon name="check" :size="15" />本场已{{ decidedByAuto ? '由自动推演' : '' }}决策：{{ DECISION_LABEL[decided] || decided }}。后续场次在左侧场景列表里。
      </p>
      <p v-else-if="piloting" class="notice">
        <Icon name="autopilot" :size="15" />自动推演中：本场结束后由导演按规则自动决策。要接手，先在舞台底栏停止自动推演。
      </p>
      <p v-if="pending" class="notice"><Icon name="spinner" :size="15" />决策处理中，请勿重复提交。</p>

      <form v-if="showNextScene" class="decision-form" @submit.prevent="confirmNextScene">
        <div class="section-title">下一场</div>
        <div class="field">
          <label>本场意图（可留空，导演自动接续）</label>
          <textarea v-model="nextSceneGoal" rows="2" placeholder="例如：两人和解，或新的冲突将起"></textarea>
        </div>
        <div class="field">
          <label>在场角色（不选则由导演决定）</label>
          <div class="pills">
            <label v-for="c in characters || []" :key="c.character_id" class="pill">
              <input v-model="nextChars" type="checkbox" :value="c.character_id" />{{ c.name }}
            </label>
          </div>
        </div>
        <div class="field">
          <label>地点（留空则由导演决定）</label>
          <input v-model="nextLocation" placeholder="例如：雨夜的酒馆" />
        </div>
        <div v-if="objects?.length" class="field">
          <label>在场物件（不选则由导演决定）</label>
          <div class="pills">
            <label v-for="o in objects" :key="o.object_id" class="pill">
              <input v-model="nextObjects" type="checkbox" :value="o.object_id" />{{ o.name }}
            </label>
          </div>
        </div>
        <div class="field">
          <label>初始条件（JSON，留空则由导演决定）</label>
          <textarea v-model="nextConditions" rows="2" placeholder='{"天气": "暴雨"}'></textarea>
        </div>
        <div class="row form-actions">
          <button type="button" class="ghost" @click="showNextScene = false">取消</button>
          <button type="submit" class="primary" :disabled="pending">进入下一场</button>
        </div>
      </form>

      <form v-if="showRollback" class="decision-form" @submit.prevent="confirmRollback">
        <div class="section-title">回滚重演</div>
        <p class="dim small">会从所选快照新建一条分支重演，不改动当前分支的任何数据。</p>
        <div class="field">
          <label>从哪份快照重演</label>
          <select v-model="rollbackSnapshotId">
            <option value="">本场开场前</option>
            <option v-for="s in props.snapshots || []" :key="s.snapshot_id" :value="s.snapshot_id">
              {{ s.label || s.snapshot_id.slice(0, 8) }}
            </option>
          </select>
        </div>
        <div class="field">
          <label>新的初始条件（JSON 或文本）</label>
          <textarea v-model="rollbackConditions" rows="2" placeholder='{"氛围": "更紧张"}'></textarea>
        </div>
        <div class="row form-actions">
          <button type="button" class="ghost" @click="showRollback = false">取消</button>
          <button type="submit" class="danger" :disabled="pending">回滚重演</button>
        </div>
      </form>
    </template>
  </div>
</template>

<style scoped>
.empty {
  padding: 24px 0;
  text-align: center;
}
.small {
  font-size: 12.5px;
}
.notice {
  margin-bottom: 12px;
}
.ending-flag {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--ok);
  font-size: 13px;
  margin-bottom: 10px;
}
.synopsis {
  font-size: 13.5px;
  line-height: 1.7;
  margin-bottom: 4px;
}
.section-title {
  margin-top: 18px;
}
.progress {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12.5px;
}
.progress b {
  font-size: 28px;
  font-weight: 600;
  line-height: 1.2;
}
.progress-bar {
  margin-top: 8px;
}
.scores {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.score {
  display: grid;
  grid-template-columns: 1fr auto;
  row-gap: 4px;
  align-items: end;
}
.score .meter {
  grid-column: 1 / -1;
  height: 3px;
}
.score .meter i.danger {
  background: var(--danger);
}
.score-name {
  font-size: 13px;
}
.score-name small {
  margin-left: 6px;
  font-size: 11.5px;
  color: var(--ink-3);
}
.score-val {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.2;
}
.score-val.danger {
  color: var(--danger);
}
.score.unanchored .score-val,
.score.unanchored .meter {
  opacity: 0.5;
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
.kv {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 12px;
  font-size: 13px;
}
.kv dt {
  color: var(--ink-2);
}
.kv dd.changed {
  color: var(--ok);
}
.kv dd.removed {
  color: var(--ink-3);
  text-decoration: line-through;
}
.ending-box {
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  padding: 12px;
  background: var(--panel-2);
  margin-bottom: 14px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.ending-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--ok);
}
.recommend {
  font-size: 13px;
  color: var(--ink-2);
  margin-bottom: 10px;
}
.recommend b {
  color: var(--ink);
}
.decide {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.decide button {
  justify-content: flex-start;
  height: 36px;
}
.decide small {
  margin-left: auto;
  color: var(--ink-3);
  font-size: 12px;
}
.decision-form {
  border-top: 1px solid var(--line);
  margin-top: 6px;
}
.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  height: 26px;
  padding: 0 8px;
  border-radius: var(--r-xs);
  background: var(--hover);
  color: var(--ink);
  font-weight: 400;
  font-size: 13px;
  cursor: pointer;
}
.form-actions {
  justify-content: flex-end;
  gap: 8px;
}
</style>
