<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useCharacterStore } from '@/stores/characters'
import { useProjectStore } from '@/stores/project'
import { useSceneStore } from '@/stores/scenes'
import { useDirectorStore } from '@/stores/director'
import { api } from '@/api/client'
import type { Scene, SceneConfig } from '@/types'
import DirectorPanel from '@/components/DirectorPanel.vue'
import CharacterInspector from '@/components/CharacterInspector.vue'
import StoryboardPanel from '@/components/StoryboardPanel.vue'
import BranchRail from '@/components/director/BranchRail.vue'
import StageView from '@/components/director/StageView.vue'
import SceneComposer from '@/components/director/SceneComposer.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import Icon from '@/components/ui/Icon.vue'
import { branchColors, flattenBranches } from '@/utils/branches'

const props = defineProps<{ projectId: string }>()

const route = useRoute()
const router = useRouter()
const charStore = useCharacterStore()
const projectStore = useProjectStore()
const sceneStore = useSceneStore()
const directorStore = useDirectorStore()

const intent = ref('')
const planning = ref(false)
// 规划/创建失败的原因，显示在规划界面顶部；重新点规划或开演时清掉
const composeError = ref('')
const creating = ref(false)
const draft = ref<SceneConfig | null>(null)
const branchId = ref('')
const inspectingId = ref('')
const scenes = ref<Scene[]>([])
const forkingId = ref('')
const forkName = ref('')
const forkConditions = ref('')
// 给导演的分叉说明：进入新分支分镜稿的"分叉说明"，首场起就在导演的规划/评估 prompt 里
const forkNotes = ref('')
// 舞台处于"开演前"：写本场意图 → 导演规划 → 就地改草稿 → 开演
const composing = ref(false)
// 右侧检查器：当前标签页；窄屏下它是抽屉，inspOpen 控制显隐
const inspTab = ref<'eval' | 'decide' | 'board' | 'snaps'>('eval')
const inspOpen = ref(false)
// 首次加载期间不让 branchId 的 watcher 推翻刚从 URL 恢复出来的场景
let bootstrapped = false

const STATUS_LABEL: Record<string, string> = {
  pending: '未开始',
  running: '模拟中',
  paused: '已中断',
  completed: '已完成',
}

const INSP_TABS = [
  { key: 'eval', label: '评估' },
  { key: 'decide', label: '决策' },
  { key: 'board', label: '分镜稿' },
  { key: 'snaps', label: '快照' },
] as const

const allBranches = computed(() => flattenBranches(directorStore.branchTree))
const colors = computed(() => branchColors(directorStore.branchTree))
const stageTitle = computed(() =>
  composing.value || draft.value ? '规划下一场' : sceneStore.currentScene?.name || '导演台',
)

/** 当前分支的快照；分支为空时退回全部，避免刚建项目时面板空白。 */
const branchSnapshots = computed(() =>
  branchId.value
    ? directorStore.snapshots.filter((s) => s.branch_id === branchId.value)
    : directorStore.snapshots,
)

const resumable = computed(
  () =>
    !!sceneStore.currentScene &&
    !sceneStore.running &&
    ['pending', 'paused'].includes(sceneStore.currentScene.status),
)

/** 主线目标是只读锚点，导演页只展示、不提供编辑（编辑入口在工作台）。 */
const narrativeGoal = computed(() => projectStore.current?.narrative_goal ?? '')

onMounted(async () => {
  if (projectStore.current?.project_id !== props.projectId) {
    await projectStore.selectProject(props.projectId)
  }
  await charStore.load(props.projectId)
  await directorStore.loadBranches(props.projectId)

  // 刷新恢复：URL 上的 scene 参数优先，其次落到该分支最近一场。
  const wanted = (route.query.scene as string) || ''
  const restored = wanted ? await attach(wanted).catch(() => null) : null
  branchId.value =
    restored?.branch_id || directorStore.branchTree.roots[0]?.branch.branch_id || ''
  await refreshBranchData()
  if (!restored) {
    const last = scenes.value[scenes.value.length - 1]
    if (last) await attach(last.scene_id)
  }
  bootstrapped = true
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  sceneStore.stopStream()
  window.removeEventListener('keydown', onKeydown)
})

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') inspOpen.value = false
}

watch(branchId, async () => {
  if (!bootstrapped) return
  // 草稿属于规划它的那条分支，切走就作废（连同在途的规划/创建响应）
  planSeq++
  composing.value = false
  draft.value = null
  composeError.value = ''
  await refreshBranchData()
  // 切分支必须连当前场景一起切，否则中间的日志与右侧的决策面板还停在上一条线上
  const last = scenes.value[scenes.value.length - 1]
  if (last) {
    await attach(last.scene_id)
  } else {
    sceneStore.clearScene()
    const q = { ...route.query }
    delete q.scene
    void router.replace({ query: q })
  }
})

async function refreshBranchData() {
  if (!branchId.value) {
    scenes.value = []
    return
  }
  const [list] = await Promise.all([
    api.listScenes(props.projectId, branchId.value),
    directorStore.loadSnapshots(props.projectId),
  ])
  scenes.value = list
}

/** 打开历史/运行中的场景：只重连，不重跑（见 store.attachScene）。 */
async function attach(sceneId: string) {
  const scene = await sceneStore.attachScene(sceneId)
  if (route.query.scene !== sceneId) {
    void router.replace({ query: { ...route.query, scene: sceneId } })
  }
  return scene
}

async function resume() {
  const scene = sceneStore.currentScene
  if (!scene) return
  // /start 失败由 store 复位并写进 lastError；这里只兜住取场景本身失败的情况
  try {
    await sceneStore.resumeScene(scene.scene_id)
  } catch (err) {
    sceneStore.lastError = err instanceof Error ? err.message : '启动失败，请重试'
  }
  await refreshBranchData()
}

const NO_GOAL_WARNING =
  '项目还没有设定主线目标，导演将自由发挥：本场意图只管这一场，' +
  '之后的评估里「目标达成」「主线偏离」没有参照，主线推进度也不度量。\n\n' +
  '可以先去工作台填写主线目标。仍要继续规划吗？'
// 同一项目确认过一次就不再问：用户已经知道没有锚点，每次规划都弹就成了噪声
let goalConfirmedFor = ''

// 规划请求的序号：重新规划/取消/开演都会作废在途的旧响应，
// 否则迟到的响应会把草稿填回已取消或已开演的舞台（甚至填进切走之后的另一条分支）
let planSeq = 0

async function plan() {
  // 只提示不拦：没有目标也可以先看看角色自己会演出什么，但不能让用户以为评分有参照
  if (!narrativeGoal.value.trim() && goalConfirmedFor !== props.projectId) {
    if (!confirm(NO_GOAL_WARNING)) return
    goalConfirmedFor = props.projectId
  }
  const seq = ++planSeq
  const forBranch = branchId.value
  planning.value = true
  composeError.value = ''
  try {
    // 不再要求必填：主线目标已由后端从项目读，这里只是可选的本场意图
    const config = await sceneStore.plan(props.projectId, forBranch, intent.value)
    // 已重新规划/已取消或开演（composer 已收起）/已切到别的分支：这份草稿过期，丢弃
    if (seq !== planSeq || forBranch !== branchId.value || (!composing.value && !draft.value)) return
    draft.value = config
  } catch (err) {
    if (seq === planSeq) composeError.value = err instanceof Error ? err.message : '规划失败，请重试'
  } finally {
    // 只有最后一次请求才能收 planning 的状态，否则旧请求会关掉新请求的"规划中"
    if (seq === planSeq) planning.value = false
  }
}

async function startScene() {
  if (!draft.value || creating.value) return
  // branch_id 为空的场景会从所有按分支过滤的列表里消失，建之前先拦下
  if (!branchId.value) {
    alert('当前没有可用分支，请先完成项目构建或选中一条分支')
    return
  }
  const seq = planSeq
  const forBranch = branchId.value
  creating.value = true
  composeError.value = ''
  let scene: Scene
  try {
    scene = await sceneStore.createScene(props.projectId, {
      branch_id: forBranch,
      name: draft.value.name,
      description: draft.value.description,
      participating_characters: draft.value.participating_characters,
      location: draft.value.location,
      initial_conditions: draft.value.initial_conditions,
      max_turns: draft.value.max_turns,
      opening_narration: draft.value.opening_narration,
      speaker_mode: draft.value.speaker_mode || 'round_robin',
    })
  } catch (err) {
    // 留在规划界面、草稿不丢，改一改或直接重试
    composeError.value = err instanceof Error ? err.message : '创建场景失败，请重试'
    return
  } finally {
    creating.value = false
  }
  // 创建期间已取消/点开别的场景/切到别的分支：场景已按原分支落库为未开演，
  // 不再把它拉上舞台，更不能替用户开演
  if (seq !== planSeq || forBranch !== branchId.value) {
    await refreshBranchData()
    return
  }
  // 草稿已落库成场景，作废在途的重新规划响应，舞台切回剧本视图
  planSeq++
  draft.value = null
  composing.value = false
  void router.replace({ query: { ...route.query, scene: scene.scene_id } })
  // 启动失败不抛出：store 会复位状态并把原因显示在舞台上，场景留在未开演、可重试
  await sceneStore.startNewScene(scene)
  await refreshBranchData()
}

function startCompose() {
  composing.value = true
  composeError.value = ''
}

function cancelCompose() {
  planSeq++ // 在途的规划响应回来后直接丢弃
  composing.value = false
  draft.value = null
  composeError.value = ''
}

async function selectScene(sceneId: string) {
  if (!sceneId) return
  cancelCompose()
  await attach(sceneId)
}

/** 在检查器里打开角色内部视图；窄屏时顺带拉出抽屉。 */
function openCharacter(cid: string) {
  inspectingId.value = cid
  inspOpen.value = true
}

/** 把每行 `key=value` 解析成初始条件字典（第一个 = 之后全部算值）。 */
function parseConditions(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const idx = line.indexOf('=')
    if (idx <= 0) continue
    const key = line.slice(0, idx).trim()
    if (key) out[key] = line.slice(idx + 1).trim()
  }
  return out
}

async function confirmFork() {
  if (!forkingId.value || !forkName.value.trim()) return
  try {
    const { branch, scene } = await directorStore.fork(
      props.projectId,
      forkingId.value,
      forkName.value.trim(),
      parseConditions(forkConditions.value),
      forkNotes.value.trim(),
    )
    forkingId.value = ''
    forkName.value = ''
    forkConditions.value = ''
    forkNotes.value = ''
    // 切到新分支并打开首场。只 attach 不 start：分叉是探索性操作，不该隐式烧掉一整场 LLM。
    branchId.value = branch.branch_id
    await refreshBranchData()
    await attach(scene.scene_id)
  } catch (err) {
    alert(err instanceof Error ? err.message : '分叉失败')
  }
}

async function removeSnapshot(snapshotId: string) {
  if (!confirm('删除该快照后将无法再从此处回滚或分叉，确定？')) return
  try {
    await directorStore.removeSnapshot(props.projectId, snapshotId)
  } catch (err) {
    alert(err instanceof Error ? err.message : '删除失败')
  }
}

/** 舞台底栏的快捷决策：继续/下一场不带人工覆盖，等价于决策面板里的默认提交。 */
function quickDecision(type: 'continue' | 'next_scene') {
  void onDecision({ decision_type: type, extra_turns: type === 'continue' ? 6 : null })
}

/** 回滚要填 IF 条件/选快照，引导到决策面板而不是在舞台栏里塞表单。 */
function openDecide() {
  inspTab.value = 'decide'
  inspOpen.value = true
}

async function onDecision(payload: Record<string, unknown>, done?: (ok: boolean) => void) {
  if (!sceneStore.currentScene) return
  try {
    await sceneStore.submitDecision(sceneStore.currentScene.scene_id, payload)
    await directorStore.loadBranches(props.projectId)
    // rollback 会把新场景建到一条新分支上，分支选择不跟着切，后续"让导演规划"
    // 和场景列表还会落回旧分支（watcher 会改写当前场景，故先抑制再手工刷新）
    const nextBranch = sceneStore.currentScene?.branch_id
    if (nextBranch && nextBranch !== branchId.value) {
      bootstrapped = false
      branchId.value = nextBranch
      await nextTick()
      bootstrapped = true
    }
    await refreshBranchData()
    const nowId = sceneStore.currentScene?.scene_id
    if (nowId && route.query.scene !== nowId) {
      void router.replace({ query: { ...route.query, scene: nowId } })
    }
    done?.(true)
  } catch (err) {
    // 失败时通知面板保留表单，再提示用户（例如 409：决策已生效/正在处理）
    done?.(false)
    alert(err instanceof Error ? err.message : '决策提交失败')
  }
}
</script>

<template>
  <div class="director-page">
    <PageHeader :context="projectStore.current?.name" :title="stageTitle">
      <template #meta>
        <span v-if="sceneStore.currentScene && !composing && !draft" class="meta">
          <span class="status-dot" :class="sceneStore.running ? 'running' : sceneStore.currentScene.status"></span>
          {{ sceneStore.statusMsg || STATUS_LABEL[sceneStore.currentScene.status] }}
        </span>
      </template>
      <button class="icon insp-toggle" title="打开检查器" @click="inspOpen = true"><Icon name="inspector" /></button>
    </PageHeader>

    <!-- 窄屏：左栏收成顶部的两个下拉框 -->
    <div class="mini-nav">
      <select v-model="branchId" aria-label="分支">
        <option v-for="b in allBranches" :key="b.branch_id" :value="b.branch_id">{{ b.name || '未命名分支' }}</option>
      </select>
      <select
        :value="composing || draft ? '' : sceneStore.currentScene?.scene_id || ''"
        aria-label="场景"
        @change="selectScene(($event.target as HTMLSelectElement).value)"
      >
        <option v-if="composing || draft" value="">规划中的新场景</option>
        <option v-for="(s, i) in scenes" :key="s.scene_id" :value="s.scene_id">第 {{ i + 1 }} 场　{{ s.name || '未命名场景' }}</option>
      </select>
      <button class="icon" title="让导演规划下一场" :disabled="!branchId" @click="startCompose"><Icon name="plus" /></button>
    </div>

    <div class="director-grid">
      <BranchRail
        class="col col-rail"
        :project-id="props.projectId"
        :tree="directorStore.branchTree"
        :branch-id="branchId"
        :scenes="scenes"
        :current-scene-id="sceneStore.currentScene?.scene_id || ''"
        :snapshots="directorStore.snapshots"
        :composing="composing || !!draft"
        @select-branch="branchId = $event"
        @select-scene="selectScene"
        @compose="startCompose"
      />

      <section class="col col-stage" aria-label="舞台">
        <SceneComposer
          v-if="composing || draft"
          v-model:intent="intent"
          :draft="draft"
          :planning="planning"
          :creating="creating"
          :error="composeError"
          :busy="sceneStore.running"
          :goal="narrativeGoal"
          :characters="charStore.characters"
          @plan="plan"
          @start="startScene"
          @cancel="cancelCompose"
        />
        <StageView
          v-else-if="sceneStore.currentScene"
          :scene="sceneStore.currentScene"
          :turns="sceneStore.turns"
          :running="sceneStore.running"
          :status-text="sceneStore.statusMsg || STATUS_LABEL[sceneStore.currentScene.status] || ''"
          :last-error="sceneStore.lastError"
          :resumable="resumable"
          :decidable="sceneStore.currentScene.status === 'completed' && !sceneStore.running && !sceneStore.appliedDecision"
          :deciding="sceneStore.decisionPending"
          :name-of="charStore.nameOf"
          @inspect="openCharacter"
          @resume="resume"
          @decide="quickDecision"
          @open-decide="openDecide"
        />
        <div v-else class="stage-empty">
          <Icon name="director" :size="28" />
          <p>{{ branchId ? '这条分支还没有打开的场景。' : '还没有分支。先在工作台完成项目构建。' }}</p>
          <button v-if="branchId" class="primary" @click="startCompose"><Icon name="plus" :size="15" />规划第一场</button>
        </div>
      </section>

      <aside class="col col-insp" :class="{ open: inspOpen }" aria-label="检查器">
        <CharacterInspector
          v-if="inspectingId"
          embedded
          :project-id="props.projectId"
          :character-id="inspectingId"
          :scene-id="sceneStore.currentScene?.scene_id || ''"
          @close="inspectingId = ''"
        />
        <!-- 用 v-show 保留各标签页的状态：分镜稿草稿、决策表单切走再回来不能丢 -->
        <div v-show="!inspectingId" class="insp-tabs">
          <div class="tabs" role="tablist">
            <button
              v-for="t in INSP_TABS"
              :key="t.key"
              role="tab"
              :aria-selected="inspTab === t.key"
              @click="inspTab = t.key"
            >
              {{ t.label }}
            </button>
            <button class="icon drawer-close" title="关闭检查器" @click="inspOpen = false"><Icon name="close" /></button>
          </div>
          <div class="pane">
            <div v-show="inspTab === 'eval'">
              <DirectorPanel
                mode="eval"
                :evaluation="sceneStore.evaluation"
                :scene-id="sceneStore.currentScene?.scene_id || ''"
              />
            </div>
            <div v-show="inspTab === 'decide'">
              <DirectorPanel
                mode="decide"
                :evaluation="sceneStore.evaluation"
                :scene-id="sceneStore.currentScene?.scene_id || ''"
                :characters="charStore.characters"
                :snapshots="branchSnapshots"
                :applied-decision="sceneStore.appliedDecision"
                :pending="sceneStore.decisionPending"
                @decision="onDecision"
                @generate-output="router.push(`/output/${props.projectId}?branch=${sceneStore.currentScene?.branch_id || branchId}`)"
              />
            </div>
            <div v-show="inspTab === 'board'">
              <!-- 本场评估到达时刷新：导演的路线图调整随评估一起落盘 -->
              <StoryboardPanel :project-id="props.projectId" :branch-id="branchId" :refresh-key="sceneStore.evaluation" />
            </div>
            <div v-show="inspTab === 'snaps'">
              <p v-if="!branchSnapshots.length" class="dim small">还没有快照。每场推演会自动生成开场前与结束后两份。</p>
              <ul class="snaps">
                <li v-for="s in branchSnapshots" :key="s.snapshot_id">
                  <div class="snap-row">
                    <span class="snap-bar" :style="{ background: colors.get(s.branch_id) || 'var(--line-strong)' }"></span>
                    <div class="snap-main">
                      <div class="snap-label">{{ s.label || s.snapshot_id.slice(0, 8) }}</div>
                      <div class="dim num snap-time">{{ (s.created_at || '').replace('T', ' ').slice(0, 16) }}</div>
                    </div>
                    <button class="icon" title="从这里分叉" @click="forkingId = forkingId === s.snapshot_id ? '' : s.snapshot_id">
                      <Icon name="fork" />
                    </button>
                    <button class="icon danger" title="删除快照" @click="removeSnapshot(s.snapshot_id)"><Icon name="trash" /></button>
                  </div>
                  <form v-if="forkingId === s.snapshot_id" class="fork-form" @submit.prevent="confirmFork">
                    <div class="field">
                      <label>新分支名称</label>
                      <input v-model="forkName" placeholder="例如：公主提前知情" />
                    </div>
                    <div class="field">
                      <label>IF 条件（每行一条 名称=内容）</label>
                      <textarea v-model="forkConditions" rows="3" placeholder="公主知情=是"></textarea>
                    </div>
                    <div class="field">
                      <label>给导演的说明（可留空）</label>
                      <textarea v-model="forkNotes" rows="2" placeholder="这条线想试试公主提前摊牌的走向"></textarea>
                    </div>
                    <p class="dim small">分叉不会改动当前分支的任何数据。新分支承接这份快照的角色状态与长期记忆，并生成一个未开演的首场。</p>
                    <div class="fork-actions">
                      <button type="button" class="ghost" @click="forkingId = ''">取消</button>
                      <button type="submit" class="primary" :disabled="!forkName.trim()"><Icon name="fork" :size="15" />分叉</button>
                    </div>
                  </form>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </aside>
      <div class="scrim" :class="{ open: inspOpen }" @click="inspOpen = false"></div>
    </div>
  </div>
</template>

<style scoped>
/* 导演台是一个尺寸容器：布局按自身宽度而不是视口宽度退让（侧栏收起后可用宽度会变） */
.director-page {
  container: director / inline-size;
  display: flex;
  flex-direction: column;
  height: 100%;
}
.meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--ink-2);
  font-size: 13px;
  white-space: nowrap;
}
.director-grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 248px minmax(0, 1fr) 340px;
  grid-template-rows: minmax(0, 1fr);
}
.col {
  min-height: 0;
  overflow: auto;
}
.col-rail {
  border-right: 1px solid var(--line);
}
.col-stage {
  overflow: hidden;
}
.col-insp {
  border-left: 1px solid var(--line);
  background: var(--panel);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.insp-tabs {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}
.tabs {
  display: flex;
  gap: 2px;
  padding: 8px 10px 0;
  border-bottom: 1px solid var(--line);
}
.tabs [role='tab'] {
  position: relative;
  height: 36px;
  padding: 0 10px;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--ink-2);
}
.tabs [role='tab']:hover {
  color: var(--ink);
}
.tabs [role='tab'][aria-selected='true'] {
  color: var(--ink);
  font-weight: 600;
}
.tabs [role='tab'][aria-selected='true']::after {
  content: '';
  position: absolute;
  left: 10px;
  right: 10px;
  bottom: -1px;
  height: 2px;
  border-radius: 1px;
  background: var(--ink);
}
.drawer-close {
  display: none;
  margin-left: auto;
  align-self: center;
}
.pane {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 16px;
}
.small {
  font-size: 12.5px;
}
.stage-empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--ink-2);
  padding: 24px;
  text-align: center;
}
.snaps {
  list-style: none;
}
.snaps > li {
  border-top: 1px solid var(--line);
  padding: 8px 0;
}
.snaps > li:first-child {
  border-top: 0;
}
.snap-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.snap-bar {
  width: 3px;
  align-self: stretch;
  border-radius: 2px;
  flex: none;
}
.snap-main {
  flex: 1;
  min-width: 0;
}
.snap-label {
  font-size: 13.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.snap-time {
  font-size: 12px;
}
.fork-form {
  margin: 10px 0 4px 11px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--panel-2);
}
.fork-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
}
.mini-nav {
  display: none;
}
.insp-toggle {
  display: none;
}
.scrim {
  display: none;
}

/* 容器变窄：检查器改成浮在右侧的毛玻璃抽屉 */
@container director (max-width: 1180px) {
  .director-grid {
    grid-template-columns: 248px minmax(0, 1fr);
  }
  .insp-toggle,
  .drawer-close {
    display: inline-flex;
  }
  .col-insp {
    position: fixed;
    top: 60px;
    right: 8px;
    bottom: 8px;
    width: min(380px, calc(100vw - 16px));
    z-index: 30;
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--material);
    backdrop-filter: saturate(180%) blur(24px);
    -webkit-backdrop-filter: saturate(180%) blur(24px);
    box-shadow: var(--shadow-float);
    transform: translateX(calc(100% + 16px));
    visibility: hidden;
    transition: transform 0.22s ease, visibility 0s linear 0.22s;
  }
  .col-insp.open {
    transform: none;
    visibility: visible;
    transition: transform 0.22s ease;
  }
  .scrim.open {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 25;
  }
}
/* 再窄：左栏收成舞台上方的下拉框，舞台永远不让出空间 */
@container director (max-width: 860px) {
  .director-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .col-rail {
    display: none;
  }
  .mini-nav {
    display: flex;
    gap: 8px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--line);
  }
  .mini-nav select {
    width: auto;
    max-width: 45%;
    height: 30px;
    padding: 0 8px;
  }
}
@supports (corner-shape: squircle) {
  @container director (max-width: 1180px) {
    .col-insp {
      corner-shape: squircle;
      border-radius: calc(var(--r-lg) * 1.6);
    }
  }
}
</style>
