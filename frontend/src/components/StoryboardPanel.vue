<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, ApiError } from '@/api/client'
import type { BeatStatus, Storyboard, StoryboardUpdate } from '@/types'

/**
 * 导演分镜稿面板（工单18）：分叉说明置顶、路线图按状态区分、可编辑、changelog 可展开。
 *
 * 四条约束，每条都对应一类已经踩过的坑：
 * - **草稿有归属**：切分支时清空（28 教训：草稿没有归属项目时，切过去再保存会写错地方）；
 *   保存的响应只认发出它的那份草稿 —— A→B→A 往返后分支号相同，草稿却已是新的一份；
 * - **409 不自动覆盖**：导演评估与用户编辑会并发，冲突时保留草稿、提示重新加载，
 *   绝不拿草稿去重试覆盖对方刚写的内容；
 * - **已有节拍原样回传 beat_id**：身份由它承载，新节拍不带 ID、由后端分配。PUT 是整份替换，
 *   漏掉一个已有节拍就等于删掉它，所以只有没填标题的**新**空行可以丢；
 * - **幂等键随内容走**：响应丢失时拿不到新节拍的 ID，只能原样重发，后端靠 request_id 认出重放。
 */
const props = defineProps<{
  projectId: string
  branchId: string
  // 变化时（如本场评估到达）刷新；正在编辑时不刷新，免得冲掉草稿
  refreshKey?: unknown
}>()

interface DraftBeat {
  beat_id: string
  title: string
  description: string
  status: BeatStatus
}

interface Draft {
  branchId: string
  revision: number
  outline: DraftBeat[]
  memo: string
  confirmGoal: boolean
  // 开始编辑时看到的主线目标：确认框旁显示它，确认时带回它的版本
  goalText: string
  goalRevisionSeen: string
  // 上一次发出的请求内容与它的幂等键：内容没变就沿用同一个键
  requestKey: string
  requestId: string
}

const STATUS_LABEL: Record<BeatStatus, string> = {
  planned: '计划',
  done: '已完成',
  dropped: '已放弃',
}
const SOURCE_LABEL: Record<string, string> = { director: '导演', user: '用户', fork: '分叉' }

const board = ref<Storyboard | null>(null)
const loading = ref(false)
const loadError = ref('')
const draft = ref<Draft | null>(null)
const saving = ref(false)
const conflict = ref('')
const saveError = ref('')
const showLog = ref(false)
// 只认最后一次请求：快速切分支时，慢回来的旧分支响应不能覆盖新分支的面板
let loadSeq = 0
// 保存中的状态属于发起它的那次保存：切走后新分支上的保存按钮不该被旧请求锁住
let saveSeq = 0

const forkConditions = computed(() =>
  Object.entries(board.value?.fork_origin?.conditions || {}).map(([k, v]) => `${k}=${v}`),
)
const changelog = computed(() => [...(board.value?.changelog || [])].reverse())
const isEmpty = computed(
  () => !!board.value && !board.value.outline.length && !board.value.memo && !board.value.fork_origin,
)

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}

async function load() {
  const branch = props.branchId
  const seq = ++loadSeq
  if (!branch) {
    board.value = null
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    const data = await api.getStoryboard(props.projectId, branch)
    if (seq !== loadSeq || branch !== props.branchId) return
    board.value = data
  } catch (err) {
    if (seq === loadSeq) loadError.value = errorText(err, '分镜稿加载失败')
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

watch(
  () => props.branchId,
  () => {
    // 草稿属于上一条分支：带着它切过去再点保存，会把 A 线的路线图写进 B 线
    draft.value = null
    conflict.value = ''
    saveError.value = ''
    board.value = null
    saveSeq++
    saving.value = false
    void load()
  },
  { immediate: true },
)

watch(
  () => props.refreshKey,
  () => {
    if (!draft.value) void load()
  },
)

function startEdit() {
  if (!board.value) return
  draft.value = {
    branchId: props.branchId,
    revision: board.value.revision,
    memo: board.value.memo,
    confirmGoal: false,
    goalText: board.value.narrative_goal,
    goalRevisionSeen: board.value.current_goal_revision,
    requestKey: '',
    requestId: '',
    outline: board.value.outline.map((b) => ({
      beat_id: b.beat_id,
      title: b.title,
      description: b.description,
      status: b.status,
    })),
  }
  conflict.value = ''
  saveError.value = ''
}

function cancelEdit() {
  draft.value = null
  conflict.value = ''
  saveError.value = ''
}

// 保存期间草稿只读（模板里用 fieldset 禁用），这几个入口同样拦住
function addBeat() {
  if (saving.value) return
  draft.value?.outline.push({ beat_id: '', title: '', description: '', status: 'planned' })
}

function removeBeat(index: number) {
  if (saving.value) return
  draft.value?.outline.splice(index, 1)
}

function moveBeat(index: number, delta: number) {
  const outline = draft.value?.outline
  const target = index + delta
  if (saving.value || !outline || target < 0 || target >= outline.length) return
  const [beat] = outline.splice(index, 1)
  outline.splice(target, 0, beat)
}

function newRequestId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
  )
}

function toPayload(d: Draft): Omit<StoryboardUpdate, 'request_id'> {
  return {
    revision: d.revision,
    memo: d.memo,
    confirm_goal: d.confirmGoal,
    goal_revision_seen: d.goalRevisionSeen,
    outline: d.outline
      // 只丢没填标题的新空行；已有节拍一旦漏发，整份替换就把它删了
      .filter((b) => b.beat_id || b.title.trim())
      .map((b) => {
        const beat = { title: b.title.trim(), description: b.description.trim(), status: b.status }
        return b.beat_id ? { beat_id: b.beat_id, ...beat } : beat
      }),
  }
}

async function save() {
  const d = draft.value
  if (!d || saving.value) return
  if (d.branchId !== props.branchId) {
    // 理论上切分支时已清空；这里是最后一道：宁可丢草稿也不写错分支
    draft.value = null
    return
  }
  const untitled = d.outline.find((b) => b.beat_id && !b.title.trim())
  if (untitled) {
    saveError.value = `节拍 ${untitled.beat_id} 的标题不能为空。清空标题不会删除节拍，要删除请点 ✕`
    return
  }
  const body = toPayload(d)
  const key = JSON.stringify(body)
  if (key !== d.requestKey) {
    d.requestKey = key
    d.requestId = newRequestId()
  }
  const token = ++saveSeq
  saving.value = true
  conflict.value = ''
  saveError.value = ''
  try {
    const data = await api.updateStoryboard(props.projectId, d.branchId, {
      ...body,
      request_id: d.requestId,
    })
    if (draft.value !== d) return
    // 保存结果比此前发出、尚未返回的刷新更新：让它们作废，否则旧稿会把面板盖回去
    loadSeq++
    loading.value = false
    // 被作废的刷新若已先失败，它留下的加载错误说的是一份已经过时的状态
    loadError.value = ''
    board.value = data
    if (JSON.stringify(toPayload(d)) !== key) {
      // 保存期间草稿又被改过：发出去的那份已落盘，之后的修改还没有。不清草稿，也不把它的
      // 基准改成新修订号 —— 幂等重放时返回的稿子可能已含他人后来的改动，改了基准下一次
      // 保存就会悄悄覆盖它们；保留旧基准，冲突就走 409 提示
      saveError.value = '已保存发出时的内容，之后的修改尚未保存。草稿已保留供对照，请重新加载后再编辑。'
      return
    }
    draft.value = null
  } catch (err) {
    if (draft.value !== d) return
    if (err instanceof ApiError && err.status === 409) {
      conflict.value = '分镜稿已被导演或他人修改。草稿已保留供对照，请重新加载后再编辑。'
    } else {
      saveError.value = errorText(err, '保存失败')
    }
  } finally {
    if (token === saveSeq) saving.value = false
  }
}

async function reloadAfterConflict() {
  draft.value = null
  conflict.value = ''
  await load()
}
</script>

<template>
  <div class="card storyboard">
    <div class="row head">
      <h3>分镜稿</h3>
      <div class="row" style="gap: 6px">
        <button v-if="!draft && board" class="ghost" @click="startEdit">✏ 编辑</button>
        <button v-if="!draft" class="ghost" :disabled="loading" @click="load">↻</button>
      </div>
    </div>
    <p class="dim hint">仅导演可见，不会进入任何角色的上下文。路线图服务于主线目标。</p>

    <p v-if="loadError" class="err">⚠ {{ loadError }}</p>
    <p v-else-if="loading && !board" class="dim">加载中...</p>

    <template v-if="board">
      <div v-if="board.fork_origin" class="fork-origin">
        <div>
          从「{{ board.fork_origin.source_branch_name || '来源分支' }}」的快照「{{
            board.fork_origin.source_snapshot_label || board.fork_origin.source_snapshot_id.slice(0, 8)
          }}」分叉
        </div>
        <div v-if="forkConditions.length" class="dim">条件：{{ forkConditions.join('；') }}</div>
        <div v-if="board.fork_origin.director_notes" class="dim notes">
          备注：{{ board.fork_origin.director_notes }}
        </div>
      </div>

      <p v-if="board.goal_stale" class="stale">
        ⚠ 路线图基于旧版主线目标。导演会在下一次评估时重排；你也可以编辑后勾选“已按当前目标重排”。
      </p>

      <!-- 只读视图 -->
      <template v-if="!draft">
        <p v-if="isEmpty" class="dim">暂无分镜稿。路线图会在第一场评估之后由导演建立。</p>
        <ol v-else-if="board.outline.length" class="beats">
          <li v-for="b in board.outline" :key="b.beat_id" :class="b.status">
            <span class="beat-id">{{ b.beat_id }}</span>
            <span class="tag" :class="b.status">{{ STATUS_LABEL[b.status] }}</span>
            <span class="beat-title">{{ b.title }}</span>
            <span v-if="b.description" class="dim beat-desc">{{ b.description }}</span>
          </li>
        </ol>
        <div v-if="board.memo" class="memo">
          <label class="dim">备忘</label>
          <p>{{ board.memo }}</p>
        </div>
      </template>

      <!-- 编辑视图 -->
      <div v-else class="editor">
        <fieldset :disabled="saving" class="edit-fields">
          <div v-for="(b, i) in draft.outline" :key="b.beat_id || `new-${i}`" class="edit-beat">
            <div class="row" style="gap: 6px">
              <span class="beat-id">{{ b.beat_id || '新' }}</span>
              <select v-model="b.status">
                <option value="planned">计划</option>
                <option value="done">已完成</option>
                <option value="dropped">已放弃</option>
              </select>
              <button class="ghost" :disabled="i === 0" @click="moveBeat(i, -1)">↑</button>
              <button class="ghost" :disabled="i === draft.outline.length - 1" @click="moveBeat(i, 1)">↓</button>
              <button class="ghost danger" @click="removeBeat(i)">✕</button>
            </div>
            <input v-model="b.title" placeholder="节拍标题" />
            <input v-model="b.description" placeholder="打算怎么走（可留空）" />
          </div>
          <button class="ghost" @click="addBeat">＋ 新增节拍</button>
          <div class="field">
            <label>备忘</label>
            <textarea v-model="draft.memo" rows="4" placeholder="人物弧光、已埋伏笔的打算、刻意留白的东西"></textarea>
          </div>
          <template v-if="board.goal_stale">
            <p class="dim goal-text">当前主线目标：{{ draft.goalText || '（未设定）' }}</p>
            <label class="confirm">
              <input v-model="draft.confirmGoal" type="checkbox" /> 已按上面这版主线目标重排
            </label>
          </template>
        </fieldset>
        <p v-if="conflict" class="err">
          ⚠ {{ conflict }}
          <button class="ghost" @click="reloadAfterConflict">重新加载</button>
        </p>
        <p v-if="saveError" class="err">⚠ {{ saveError }}</p>
        <div class="row" style="gap: 6px">
          <button :disabled="saving" @click="save">{{ saving ? '保存中...' : '保存' }}</button>
          <button class="ghost" @click="cancelEdit">取消</button>
        </div>
      </div>

      <div v-if="board.changelog.length" class="log">
        <button class="ghost" @click="showLog = !showLog">
          {{ showLog ? '收起' : '展开' }}改动记录（{{ board.changelog.length }}）
        </button>
        <ul v-if="showLog">
          <li v-for="(c, i) in changelog" :key="i">
            <span class="tag">{{ SOURCE_LABEL[c.source] || c.source }}</span>
            <span class="dim">{{ (c.at || '').replace('T', ' ').slice(0, 19) }}</span>
            <div>{{ c.summary }}</div>
          </li>
        </ul>
      </div>
    </template>
  </div>
</template>

<style scoped>
.head {
  justify-content: space-between;
}
.hint {
  font-size: 12px;
  margin-top: 4px;
}
.err,
.stale {
  font-size: 12px;
  color: var(--highlight);
  margin-top: 8px;
}
.fork-origin {
  margin-top: 10px;
  padding: 8px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
}
.notes {
  white-space: pre-wrap;
}
.beats {
  list-style: none;
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.beats li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}
.beats li.done .beat-title {
  text-decoration: line-through;
  color: var(--text-dim);
}
.beats li.dropped {
  opacity: 0.55;
  font-style: italic;
}
.beat-id {
  font-family: monospace;
  font-size: 11px;
  color: var(--text-dim);
}
.tag.planned {
  color: var(--highlight);
}
.beat-desc {
  flex: 1 0 100%;
  font-size: 12px;
  padding-left: 28px;
}
.memo {
  margin-top: 10px;
  font-size: 13px;
}
.memo p {
  white-space: pre-wrap;
  margin-top: 4px;
}
.editor {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.edit-beat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.confirm {
  font-size: 12px;
}
.edit-fields {
  border: none;
  margin: 0;
  padding: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.goal-text {
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
}
.log {
  margin-top: 10px;
  font-size: 12px;
}
.log ul {
  list-style: none;
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
