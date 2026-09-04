<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useCharacterStore } from '@/stores/characters'
import { useProjectStore } from '@/stores/project'
import { useSceneStore } from '@/stores/scenes'
import { useDirectorStore } from '@/stores/director'
import { api } from '@/api/client'
import type { Scene, SceneConfig } from '@/types'
import SceneTree from '@/components/SceneTree.vue'
import DialogLog from '@/components/DialogLog.vue'
import DirectorPanel from '@/components/DirectorPanel.vue'
import CharacterInspector from '@/components/CharacterInspector.vue'

const props = defineProps<{ projectId: string }>()

const route = useRoute()
const router = useRouter()
const charStore = useCharacterStore()
const projectStore = useProjectStore()
const sceneStore = useSceneStore()
const directorStore = useDirectorStore()

const intent = ref('')
const planning = ref(false)
const draft = ref<SceneConfig | null>(null)
const branchId = ref('')
const inspectingId = ref('')
const scenes = ref<Scene[]>([])
const forkingId = ref('')
const forkName = ref('')
const forkConditions = ref('')
// 首次加载期间不让 branchId 的 watcher 推翻刚从 URL 恢复出来的场景
let bootstrapped = false

const STATUS_LABEL: Record<string, string> = {
  pending: '未开始',
  running: '模拟中',
  paused: '已中断',
  completed: '已完成',
}

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
})

onBeforeUnmount(() => sceneStore.stopStream())

watch(branchId, async () => {
  if (!bootstrapped) return
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
  await sceneStore.resumeScene(scene.scene_id)
  await refreshBranchData()
}

async function plan() {
  planning.value = true
  try {
    // 不再要求必填：主线目标已由后端从项目读，这里只是可选的本场意图
    draft.value = await sceneStore.plan(props.projectId, branchId.value, intent.value)
  } finally {
    planning.value = false
  }
}

async function startScene() {
  if (!draft.value) return
  // branch_id 为空的场景会从所有按分支过滤的列表里消失，建之前先拦下
  if (!branchId.value) {
    alert('当前没有可用分支，请先完成项目构建或选中一条分支')
    return
  }
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
  void router.replace({ query: { ...route.query, scene: scene.scene_id } })
  await sceneStore.startSimulation(scene.scene_id)
  await refreshBranchData()
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
      '',
    )
    forkingId.value = ''
    forkName.value = ''
    forkConditions.value = ''
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
  <div class="director">
    <h1>导演视角</h1>
    <div class="layout-grid">
      <!-- 左侧：分支树 + 规划 -->
      <section class="left">
        <div class="card">
          <h3>分支树</h3>
          <SceneTree
            :tree="directorStore.branchTree"
            :selected-branch-id="branchId"
            @select="branchId = $event"
          />
        </div>
        <div class="card">
          <h3>本分支场景</h3>
          <ul class="scene-list">
            <li
              v-for="s in scenes"
              :key="s.scene_id"
              :class="{ active: s.scene_id === sceneStore.currentScene?.scene_id }"
              @click="attach(s.scene_id)"
            >
              <span class="scene-name">{{ s.name || '未命名场景' }}</span>
              <span class="tag" :class="s.status">
                {{ STATUS_LABEL[s.status] || s.status }} · {{ s.turns_completed }}轮
              </span>
            </li>
            <li v-if="!scenes.length" class="dim">该分支还没有场景</li>
          </ul>
        </div>
        <div class="card">
          <h3>角色内部状态</h3>
          <ul class="char-list">
            <li v-for="c in charStore.characters" :key="c.character_id">
              <span>{{ c.name }}<span class="dim"> · {{ c.current_emotion }}</span></span>
              <button class="ghost" @click="inspectingId = c.character_id">🔍</button>
            </li>
            <li v-if="!charStore.characters.length" class="dim">尚未生成角色</li>
          </ul>
        </div>
        <div class="card">
          <h3>规划场景</h3>
          <p class="dim goal-anchor">
            主线目标：{{ narrativeGoal || '尚未设定（可在工作台填写）' }}
          </p>
          <div class="field" style="margin-top: 10px">
            <label>本场意图（可留空）</label>
            <textarea v-model="intent" placeholder="例如：让两位主角在雨夜的酒馆中第一次正面冲突"></textarea>
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
            <button :disabled="sceneStore.running" @click="startScene">
              {{ sceneStore.running ? '模拟中...' : '▶ 开始模拟' }}
            </button>
          </div>
        </div>
      </section>

      <!-- 中：对话日志 -->
      <section class="card center">
        <div class="row" style="justify-content: space-between">
          <h3>{{ sceneStore.currentScene?.name || '对话日志' }}</h3>
          <div class="row" style="gap: 8px">
            <button v-if="resumable" class="ghost" @click="resume">▶ 继续这一场</button>
            <span class="tag">{{ sceneStore.statusMsg || '空闲' }}</span>
          </div>
        </div>
        <p v-if="sceneStore.lastError" class="scene-error">⚠ {{ sceneStore.lastError }}</p>
        <DialogLog :turns="sceneStore.turns" />
      </section>

      <!-- 右：导演面板 + 快照 -->
      <section class="right">
        <DirectorPanel
          :evaluation="sceneStore.evaluation"
          :scene-id="sceneStore.currentScene?.scene_id || ''"
          :characters="charStore.characters"
          :snapshots="branchSnapshots"
          :applied-decision="sceneStore.appliedDecision"
          :pending="sceneStore.decisionPending"
          @decision="onDecision"
          @generate-output="router.push(`/output/${props.projectId}`)"
        />
        <div class="card">
          <h3>快照</h3>
          <ul class="snap-list">
            <li v-for="s in branchSnapshots" :key="s.snapshot_id">
              <div class="snap-main">
                <span class="snap-label">{{ s.label || s.snapshot_id.slice(0, 8) }}</span>
                <span class="dim">{{ (s.created_at || '').replace('T', ' ').slice(0, 19) }}</span>
              </div>
              <div class="row" style="gap: 6px">
                <button class="ghost" @click="forkingId = s.snapshot_id">🌱 分叉</button>
                <button class="ghost danger" @click="removeSnapshot(s.snapshot_id)">🗑</button>
              </div>
              <div v-if="forkingId === s.snapshot_id" class="fork-form">
                <input v-model="forkName" placeholder="新分支名称，如：IF线·公主提前知情" />
                <textarea
                  v-model="forkConditions"
                  rows="3"
                  placeholder="IF 条件，每行一条 key=value，如：公主知情=是"
                ></textarea>
                <span class="dim" style="font-size: 12px">
                  分叉不会改动当前分支的任何数据；新分支会承接该快照的角色状态与长期记忆，
                  并生成一个未开跑的首场。
                </span>
                <div class="row" style="gap: 6px">
                  <button @click="confirmFork">创建分支</button>
                  <button class="ghost" @click="forkingId = ''">取消</button>
                </div>
              </div>
            </li>
            <li v-if="!branchSnapshots.length" class="dim">还没有快照。每场推演会自动生成前后两份。</li>
          </ul>
        </div>
      </section>
    </div>

    <CharacterInspector
      v-if="inspectingId"
      :project-id="props.projectId"
      :character-id="inspectingId"
      :scene-id="sceneStore.currentScene?.scene_id || ''"
      @close="inspectingId = ''"
    />
  </div>
</template>

<style scoped>
.director h1 {
  margin-bottom: 16px;
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
.scene-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--highlight);
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
.char-list {
  list-style: none;
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.char-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
}
.scene-list,
.snap-list {
  list-style: none;
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.scene-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  cursor: pointer;
}
.scene-list li.active {
  border-color: var(--highlight);
}
.scene-list li.dim {
  border: none;
  cursor: default;
}
.scene-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.goal-anchor {
  font-size: 12px;
  line-height: 1.5;
  margin-top: 8px;
}
.tag.running {
  color: var(--highlight);
}
.snap-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.snap-list li.dim {
  border: none;
}
.snap-main {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.snap-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fork-form {
  flex: 1 0 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
