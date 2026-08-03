<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useCharacterStore } from '@/stores/characters'
import { useSceneStore } from '@/stores/scenes'
import { useDirectorStore } from '@/stores/director'
import { resolveBranchId, resolveScene } from '@/utils/directorNavigation'
import type { BranchTreeNode, Scene, SceneConfig, Snapshot } from '@/types'
import SceneTree from '@/components/SceneTree.vue'
import DialogLog from '@/components/DialogLog.vue'
import DirectorPanel from '@/components/DirectorPanel.vue'

const props = defineProps<{ projectId: string }>()
const route = useRoute()
const router = useRouter()

const charStore = useCharacterStore()
const sceneStore = useSceneStore()
const directorStore = useDirectorStore()

const goal = ref('')
const planning = ref(false)
const draft = ref<SceneConfig | null>(null)
const branchId = ref('')
const pageLoading = ref(true)
const pageError = ref('')
const forkSnapshot = ref<Snapshot | null>(null)
const forkName = ref('')
const forkConditions = ref('')
const forkNotes = ref('')
const forking = ref(false)
let navigationRun = 0

const allBranchIds = computed(() => {
  const ids: string[] = []
  const visit = (nodes: BranchTreeNode[]) => {
    nodes.forEach((node) => {
      ids.push(node.branch.branch_id)
      visit(node.children)
    })
  }
  visit(directorStore.branchTree.roots)
  return ids
})

const selectedSceneId = computed(() => sceneStore.currentScene?.scene_id || '')
const branchSnapshots = computed(() =>
  directorStore.snapshots.filter((snapshot) => snapshot.branch_id === branchId.value),
)

function queryText(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function snapshotPosition(snapshot: Snapshot): string {
  if (snapshot.label.startsWith('before:')) return '场景前'
  if (snapshot.label.startsWith('after:')) return '场景后'
  return snapshot.label || '快照'
}

function formatTime(value: string): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '时间未知'
}

onMounted(async () => {
  pageLoading.value = true
  pageError.value = ''
  try {
    await Promise.all([
      charStore.load(props.projectId),
      directorStore.loadBranches(props.projectId),
      directorStore.loadSnapshots(props.projectId),
    ])
    await restoreFromUrl()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '导演工作台加载失败'
  } finally {
    pageLoading.value = false
  }
})

onBeforeUnmount(() => sceneStore.stopStream())

watch(
  () => [route.query.branch_id, route.query.scene_id],
  async ([nextBranch, nextScene]) => {
    if (pageLoading.value) return
    if (
      queryText(nextBranch) === branchId.value &&
      queryText(nextScene) === selectedSceneId.value
    ) return
    await restoreFromUrl()
  },
)

async function writeNavigation(nextBranchId: string, nextSceneId = '') {
  await router.replace({
    query: {
      ...route.query,
      branch_id: nextBranchId || undefined,
      scene_id: nextSceneId || undefined,
    },
  })
}

async function restoreFromUrl() {
  const requestedBranch = queryText(route.query.branch_id)
  const validBranch = resolveBranchId(requestedBranch, allBranchIds.value)
  await selectBranch(validBranch, queryText(route.query.scene_id))
}

async function selectBranch(nextBranchId: string, preferredSceneId = '') {
  const run = ++navigationRun
  pageError.value = ''
  branchId.value = nextBranchId
  draft.value = null
  sceneStore.clearSelection()
  if (!nextBranchId) {
    await writeNavigation('')
    return
  }

  try {
    const scenes = await sceneStore.loadByBranch(props.projectId, nextBranchId)
    if (run !== navigationRun) return
    const target = resolveScene(preferredSceneId, scenes)
    if (!target) {
      await writeNavigation(nextBranchId)
      return
    }
    await sceneStore.loadScene(target.scene_id)
    if (run !== navigationRun) return
    await writeNavigation(nextBranchId, target.scene_id)
  } catch (error) {
    if (run !== navigationRun) return
    pageError.value = error instanceof Error ? error.message : '导航加载失败'
  }
}

async function selectScene(scene: Scene) {
  pageError.value = ''
  try {
    await sceneStore.loadScene(scene.scene_id)
    await writeNavigation(branchId.value, scene.scene_id)
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '场景加载失败'
    await selectBranch(branchId.value)
  }
}

async function plan() {
  if (!goal.value.trim()) return
  planning.value = true
  try {
    draft.value = await sceneStore.plan(props.projectId, branchId.value, goal.value)
  } finally {
    planning.value = false
  }
}

async function startScene() {
  if (!draft.value) return
  const scene = await sceneStore.createScene(props.projectId, {
    branch_id: branchId.value,
    name: draft.value.name,
    description: draft.value.description,
    participating_characters: draft.value.participating_characters,
    location: draft.value.location,
    initial_conditions: draft.value.initial_conditions,
    max_turns: draft.value.max_turns,
    opening_narration: draft.value.opening_narration,
    speaker_mode: draft.value.speaker_mode || 'round_robin',
  })
  await sceneStore.loadByBranch(props.projectId, branchId.value)
  await writeNavigation(branchId.value, scene.scene_id)
  await sceneStore.startSimulation(scene.scene_id)
  await directorStore.loadSnapshots(props.projectId)
}

async function startPendingScene() {
  const scene = sceneStore.currentScene
  if (!scene || scene.status !== 'pending') return
  try {
    await sceneStore.startSimulation(scene.scene_id)
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '场景启动失败'
  }
}

async function onDecision(payload: Record<string, unknown>, done?: (ok: boolean) => void) {
  if (!sceneStore.currentScene) return
  try {
    const decision = await sceneStore.submitDecision(sceneStore.currentScene.scene_id, payload)
    await Promise.all([
      directorStore.loadBranches(props.projectId),
      directorStore.loadSnapshots(props.projectId),
      sceneStore.loadByBranch(props.projectId, branchId.value),
    ])
    if (decision?.decision_type === 'continue') {
      await sceneStore.loadScene(sceneStore.currentScene.scene_id)
      sceneStore.subscribeToScene(sceneStore.currentScene.scene_id)
    }
    done?.(true)
  } catch (err) {
    // 失败时通知面板保留表单，再提示用户（例如 409：决策已生效/正在处理）
    done?.(false)
    alert(err instanceof Error ? err.message : '决策提交失败')
  }
}

function openFork(snapshot: Snapshot) {
  forkSnapshot.value = snapshot
  forkName.value = `分支 · ${snapshotPosition(snapshot)}`
  forkConditions.value = ''
  forkNotes.value = ''
}

async function confirmFork() {
  if (!forkSnapshot.value || !forkName.value.trim()) return
  let conditions: Record<string, unknown> = {}
  if (forkConditions.value.trim()) {
    try {
      conditions = JSON.parse(forkConditions.value)
    } catch {
      conditions = { note: forkConditions.value.trim() }
    }
  }
  forking.value = true
  try {
    const branch = await directorStore.fork(
      props.projectId,
      forkSnapshot.value.snapshot_id,
      forkName.value.trim(),
      conditions,
      forkNotes.value.trim(),
    )
    forkSnapshot.value = null
    await writeNavigation(branch.branch_id)
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '创建分支失败'
  } finally {
    forking.value = false
  }
}
</script>

<template>
  <div class="director">
    <div class="page-heading">
      <h1>导演视角</h1>
      <span v-if="branchId" class="dim">导航状态已写入 URL</span>
    </div>
    <div v-if="pageLoading" class="card state-block">正在恢复导演工作台...</div>
    <div v-else-if="pageError && !directorStore.branchTree.roots.length" class="card state-block error">
      {{ pageError }}
    </div>
    <div class="layout-grid">
      <!-- 左侧：分支树 + 规划 -->
      <section class="left">
        <div class="card">
          <h3>分支树</h3>
          <div v-if="directorStore.branchesLoading" class="dim state-line">加载分支...</div>
          <div v-else-if="directorStore.branchesError" class="error state-line">
            {{ directorStore.branchesError }}
          </div>
          <SceneTree
            v-else
            :tree="directorStore.branchTree"
            :selected-branch-id="branchId"
            @select="selectBranch($event)"
          />
        </div>
        <div class="card scene-browser">
          <div class="section-heading">
            <h3>场景</h3>
            <span class="dim">{{ sceneStore.scenesInBranch.length }}</span>
          </div>
          <div v-if="sceneStore.listLoading" class="dim state-line">加载场景...</div>
          <div v-else-if="sceneStore.listError" class="error state-line">{{ sceneStore.listError }}</div>
          <div v-else-if="!sceneStore.scenesInBranch.length" class="dim state-line">
            当前分支还没有场景。
          </div>
          <button
            v-for="scene in sceneStore.scenesInBranch"
            v-else
            :key="scene.scene_id"
            class="scene-item"
            :class="{ active: selectedSceneId === scene.scene_id }"
            @click="selectScene(scene)"
          >
            <span>{{ scene.name || '未命名场景' }}</span>
            <small>{{ scene.status }} · {{ scene.turns_completed }} 轮</small>
          </button>
        </div>
        <div class="card">
          <h3>规划场景</h3>
          <div class="field" style="margin-top: 10px">
            <label>叙事目标</label>
            <textarea v-model="goal" placeholder="例如：让两位主角在雨夜的酒馆中第一次正面冲突"></textarea>
          </div>
          <button :disabled="planning" @click="plan">{{ planning ? '规划中...' : '🎬 让导演规划' }}</button>

          <div v-if="draft" class="draft">
            <div class="field">
              <label>场景名</label>
              <input v-model="draft.name" />
            </div>
            <div class="field">
              <label>地点</label>
              <input v-model="draft.location" />
            </div>
            <div class="field">
              <label>描述</label>
              <textarea v-model="draft.description"></textarea>
            </div>
            <div class="field">
              <label>开场白</label>
              <textarea v-model="draft.opening_narration"></textarea>
            </div>
            <div class="field">
              <label>参与角色</label>
              <div class="char-pills">
                <span class="tag" v-for="cid in draft.participating_characters" :key="cid">
                  {{ charStore.nameOf(cid) }}
                </span>
              </div>
            </div>
            <button :disabled="sceneStore.running || !branchId" @click="startScene">
              {{ sceneStore.running ? '模拟中...' : '▶ 开始模拟' }}
            </button>
          </div>
        </div>
      </section>

      <!-- 中：对话日志 -->
      <section class="card center">
        <div class="row" style="justify-content: space-between">
          <h3>{{ sceneStore.currentScene?.name || '对话日志' }}</h3>
          <span class="tag">{{ sceneStore.statusMsg || '空闲' }}</span>
        </div>
        <div v-if="pageError" class="inline-error">{{ pageError }}</div>
        <div v-if="sceneStore.sceneLoading" class="dim state-block">加载场景内容...</div>
        <div v-else-if="sceneStore.sceneError" class="state-block error">{{ sceneStore.sceneError }}</div>
        <div v-else-if="!sceneStore.currentScene" class="dim state-block">
          选择一个场景查看完整记录。
        </div>
        <div v-else-if="sceneStore.currentScene.status === 'pending'" class="pending-banner">
          <span>该场景尚未开始。查看不会启动模拟。</span>
          <button :disabled="sceneStore.running" @click="startPendingScene">开始模拟</button>
        </div>
        <DialogLog :turns="sceneStore.turns" />
      </section>

      <!-- 右：导演面板 -->
      <section class="right">
        <DirectorPanel
          :evaluation="sceneStore.evaluation"
          :scene-id="sceneStore.currentScene?.scene_id || ''"
          :characters="charStore.characters"
          :pending="sceneStore.decisionPending"
          :decision="sceneStore.currentDecision"
          :readonly="sceneStore.currentScene?.status !== 'completed'"
          @decision="onDecision"
        />
        <div class="card snapshot-panel">
          <div class="section-heading">
            <h3>快照时间线</h3>
            <span class="dim">{{ branchSnapshots.length }}</span>
          </div>
          <div v-if="directorStore.snapshotsLoading" class="dim state-line">加载快照...</div>
          <div v-else-if="directorStore.snapshotsError" class="error state-line">
            {{ directorStore.snapshotsError }}
          </div>
          <div v-else-if="!branchSnapshots.length" class="dim state-line">当前分支暂无快照。</div>
          <div v-for="snapshot in branchSnapshots" v-else :key="snapshot.snapshot_id" class="snapshot-item">
            <div>
              <strong>{{ snapshotPosition(snapshot) }}</strong>
              <small>场景 {{ snapshot.scene_id }}</small>
              <small>{{ formatTime(snapshot.created_at) }}</small>
            </div>
            <button class="ghost compact" title="从此快照创建分支" @click="openFork(snapshot)">分叉</button>
          </div>
        </div>
      </section>
    </div>

    <div v-if="forkSnapshot" class="modal-backdrop" @click.self="forkSnapshot = null">
      <div class="card fork-dialog">
        <h3>从快照创建分支</h3>
        <p class="dim">{{ snapshotPosition(forkSnapshot) }} · {{ formatTime(forkSnapshot.created_at) }}</p>
        <div class="field">
          <label>分支名称</label>
          <input v-model="forkName" />
        </div>
        <div class="field">
          <label>新条件（JSON 或文本）</label>
          <textarea v-model="forkConditions"></textarea>
        </div>
        <div class="field">
          <label>导演备注</label>
          <textarea v-model="forkNotes"></textarea>
        </div>
        <p class="dim fork-note">分叉只创建导航分支，不等同于回滚或恢复当前运行状态。</p>
        <div class="row">
          <button :disabled="forking || !forkName.trim()" @click="confirmFork">
            {{ forking ? '创建中...' : '创建分支' }}
          </button>
          <button class="ghost" @click="forkSnapshot = null">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.director h1 {
  margin-bottom: 16px;
}
.page-heading,
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.page-heading span {
  font-size: 12px;
}
.layout-grid {
  display: grid;
  grid-template-columns: 320px 1fr 320px;
  gap: 16px;
  height: calc(100vh - 120px);
}
.left {
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
}
.right {
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
}
.center {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.draft {
  margin-top: 16px;
  border-top: 1px solid var(--border);
  padding-top: 14px;
}
.char-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.scene-browser {
  flex: 0 0 auto;
}
.scene-item {
  display: grid;
  width: 100%;
  gap: 2px;
  margin-top: 8px;
  text-align: left;
  background: var(--bg);
}
.scene-item small,
.snapshot-item small {
  display: block;
  color: var(--text-dim);
  font-size: 11px;
}
.scene-item.active {
  border-color: var(--highlight);
  background: var(--accent);
}
.state-line,
.state-block {
  padding: 14px 4px;
}
.error,
.inline-error {
  color: #ff9cab;
}
.inline-error {
  padding: 8px 0;
  font-size: 12px;
}
.pending-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 10px 0;
  padding: 10px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.snapshot-panel {
  padding: 14px;
}
.snapshot-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}
.snapshot-item:last-child {
  border-bottom: 0;
}
.compact {
  padding: 5px 9px;
  font-size: 12px;
}
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 20;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgb(5 7 18 / 76%);
}
.fork-dialog {
  width: min(480px, 100%);
}
.fork-dialog > p {
  margin: 6px 0 14px;
}
.fork-note {
  font-size: 12px;
}
@media (max-width: 1100px) {
  .layout-grid {
    grid-template-columns: 280px 1fr;
    height: auto;
  }
  .right {
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
  .center {
    min-height: 620px;
  }
}
@media (max-width: 720px) {
  .layout-grid,
  .right {
    display: flex;
    flex-direction: column;
  }
  .center {
    min-height: 520px;
  }
}
</style>
