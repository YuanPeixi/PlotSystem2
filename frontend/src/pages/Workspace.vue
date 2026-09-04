<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore, getLastProjectId } from '@/stores/project'
import { useCharacterStore } from '@/stores/characters'
import GraphViewer from '@/components/GraphViewer.vue'
import GraphViewer2 from '@/components/GraphViewer2.vue'
import CharacterCardView from '@/components/CharacterCard.vue'
import CharacterInspector from '@/components/CharacterInspector.vue'

const router = useRouter()
const store = useProjectStore()
const charStore = useCharacterStore()

const inspectingId = ref('')

const newName = ref('')
const newDesc = ref('')
const newGoal = ref('')
const editingGoal = ref(false)
// 草稿归属的项目：编辑 A 时直接点开 B 再保存，会把 A 的文字静默写进 B 的锚点
const editingProjectId = ref('')
const goalDraft = ref('')
const criteriaDraft = ref('')
const savingGoal = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const building = ref(false)
const graphViewerVersion = ref<'legacy' | 'focused'>('legacy')
let pollTimer: number | undefined
let lastCharDone = 0

onMounted(async () => {
  await store.loadProjects()
  // 刷新页面后自动恢复上次打开的项目，避免看起来像"进度丢失"
  const lastId = getLastProjectId()
  if (lastId && store.projects.some((p) => p.project_id === lastId)) {
    await open(lastId)
  }
})

onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})

async function create() {
  if (!newName.value.trim()) return
  const p = await store.createProject({
    name: newName.value,
    description: newDesc.value,
    narrative_goal: newGoal.value,
  })
  newName.value = ''
  newDesc.value = ''
  newGoal.value = ''
  await open(p.project_id)
}

function startEditGoal() {
  goalDraft.value = store.current?.narrative_goal ?? ''
  criteriaDraft.value = store.current?.ending_criteria ?? ''
  editingProjectId.value = store.current?.project_id ?? ''
  editingGoal.value = true
}

function cancelEditGoal() {
  editingGoal.value = false
  editingProjectId.value = ''
  goalDraft.value = ''
  criteriaDraft.value = ''
}

async function saveGoal() {
  if (!store.current || store.current.project_id !== editingProjectId.value) {
    cancelEditGoal()
    return
  }
  savingGoal.value = true
  try {
    await store.updateProject(editingProjectId.value, {
      narrative_goal: goalDraft.value,
      ending_criteria: criteriaDraft.value,
    })
    cancelEditGoal()
  } finally {
    savingGoal.value = false
  }
}

async function open(id: string) {
  if (pollTimer) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
  cancelEditGoal()
  await store.selectProject(id)
  await store.loadGraph(id)
  await charStore.load(id)
  // 恢复该项目的构建进度；若仍在进行中（未完成也未失败），自动继续轮询
  const s = await store.refreshBuildStatus(id)
  lastCharDone = s.character_done ?? 0
  const inProgress = s.progress > 0 && s.progress < 1 && !s.stage?.startsWith('失败')
  if (inProgress) {
    building.value = true
    startPolling()
  } else {
    building.value = false
  }
}

function startPolling() {
  if (!store.current) return
  pollTimer = window.setInterval(async () => {
    const s = await store.refreshBuildStatus(store.current!.project_id)
    // 角色卡逐个生成时实时刷新角色列表，便于预览
    if (s.character_done && s.character_done > lastCharDone) {
      lastCharDone = s.character_done
      await charStore.load(store.current!.project_id)
    }
    if (s.progress >= 1 || s.stage.startsWith('失败')) {
      building.value = false
      window.clearInterval(pollTimer)
      await open(store.current!.project_id)
    }
  }, 1500)
}

async function onUpload(e: Event) {
  const input = e.target as HTMLInputElement
  if (!input.files?.length || !store.current) return
  for (const f of Array.from(input.files)) {
    await store.uploadSeed(store.current.project_id, f)
  }
  await store.selectProject(store.current.project_id)
}

async function build() {
  if (!store.current) return
  building.value = true
  lastCharDone = 0
  await store.build(store.current.project_id)
  startPolling()
}
</script>

<template>
  <div class="workspace">
    <h1>工作台</h1>

    <div class="grid">
      <!-- 左：项目列表 + 创建 -->
      <section class="card">
        <h3>项目</h3>
        <div class="field" style="margin-top: 12px">
          <input v-model="newName" placeholder="新项目名称" />
        </div>
        <div class="field">
          <input v-model="newDesc" placeholder="简述（可选）" />
        </div>
        <div class="field">
          <input v-model="newGoal" placeholder="主线目标（可选，之后也可修改）" />
        </div>
        <button @click="create">＋ 创建项目</button>

        <ul class="project-list">
          <li
            v-for="p in store.projects"
            :key="p.project_id"
            :class="{ active: store.current?.project_id === p.project_id }"
            @click="open(p.project_id)"
          >
            <div>
              <div class="p-name">{{ p.name }}</div>
              <div class="dim">{{ p.status }}</div>
            </div>
            <button class="ghost danger" @click.stop="store.deleteProject(p.project_id)">✕</button>
          </li>
        </ul>
      </section>

      <!-- 右：当前项目操作 -->
      <section class="card" v-if="store.current">
        <div class="row" style="justify-content: space-between">
          <h3>{{ store.current.name }}</h3>
          <div class="row">
            <button class="ghost" @click="router.push(`/director/${store.current.project_id}`)">进入导演视角 →</button>
          </div>
        </div>
        <p class="dim">{{ store.current.description }}</p>

        <div class="goal-box">
          <div class="row" style="justify-content: space-between">
            <span class="dim">主线目标（导演的只读锚点）</span>
            <button v-if="!editingGoal" class="ghost" @click="startEditGoal">编辑</button>
          </div>
          <template v-if="editingGoal">
            <div class="field">
              <textarea v-model="goalDraft" placeholder="例如：王子查明丞相通敌并最终将其拉下马"></textarea>
            </div>
            <div class="field">
              <textarea v-model="criteriaDraft" placeholder="结局判定标准（可选）：什么情形算故事讲完了"></textarea>
            </div>
            <div class="row">
              <button :disabled="savingGoal" @click="saveGoal">{{ savingGoal ? '保存中...' : '保存' }}</button>
              <button class="ghost" @click="cancelEditGoal">取消</button>
            </div>
          </template>
          <template v-else>
            <p v-if="store.current.narrative_goal" class="goal-text">{{ store.current.narrative_goal }}</p>
            <p v-else class="dim">尚未设定。没有它，导演只能顺着上一场的惯性往下演，也无法判定结局。</p>
            <p v-if="store.current.ending_criteria" class="dim">结局标准：{{ store.current.ending_criteria }}</p>
          </template>
        </div>
        <div class="seed-box">
          <div class="row" style="justify-content: space-between">
            <span class="dim">种子文本（{{ store.current.seed_texts.length }}）</span>
            <button class="ghost" @click="fileInput?.click()">上传种子文本</button>
            <input ref="fileInput" type="file" multiple accept=".txt,.md" hidden @change="onUpload" />
          </div>
          <ul class="seed-list">
            <li v-for="(s, i) in store.current.seed_texts" :key="i" class="dim">📄 {{ s.split(/[\\/]/).pop() }}</li>
          </ul>
        </div>

        <div class="build-box">
          <button :disabled="building || !store.current.seed_texts.length" @click="build">
            {{ building ? '构建中...' : '🔨 运行 GraphRAG 构建' }}
          </button>
          <div v-if="building || store.buildStatus.progress > 0" class="progress">
            <div class="progress-bar" :style="{ width: store.buildStatus.progress * 100 + '%' }"></div>
            <span class="dim">{{ store.buildStatus.stage }}</span>
            <span
              v-if="store.buildStatus.character_total"
              class="dim char-progress"
            >
              角色卡：{{ store.buildStatus.character_done ?? 0 }} / {{ store.buildStatus.character_total }}
            </span>
          </div>
        </div>
      </section>
      <section class="card placeholder dim" v-else>← 请选择或创建一个项目</section>
    </div>

    <!-- 图谱 + 角色 -->
    <div class="grid bottom" v-if="store.current">
      <section class="card graph-card">
        <div class="graph-heading">
          <h3>知识图谱</h3>
          <div class="graph-switch" role="group" aria-label="图谱查看器版本">
            <button
              type="button"
              :class="{ active: graphViewerVersion === 'legacy' }"
              @click="graphViewerVersion = 'legacy'"
            >旧版</button>
<button
  type="button"
  :class="{ active: graphViewerVersion === 'focused' }"
  :aria-pressed="graphViewerVersion === 'focused'"
  @click="graphViewerVersion = 'focused'"
>Graph Viewer 2</button>
          </div>
        </div>
        <GraphViewer v-if="graphViewerVersion === 'legacy'" :data="store.graph" />
        <GraphViewer2 v-else :data="store.graph" />
      </section>
      <section class="card">
        <h3>
          角色（{{ charStore.characters.length }}<template
            v-if="building && store.buildStatus.character_total"
            > / {{ store.buildStatus.character_total }}</template
          >）
        </h3>
        <div class="char-grid">
          <CharacterCardView
            v-for="c in charStore.characters"
            :key="c.character_id"
            :character="c"
            @inspect="inspectingId = $event"
          />
          <div
            v-if="building && store.buildStatus.character_total && charStore.characters.length < store.buildStatus.character_total"
            class="dim generating"
          >
            ⏳ 正在生成角色卡…
          </div>
          <div v-else-if="!charStore.characters.length" class="dim">构建后将自动生成角色。</div>
        </div>
      </section>
    </div>

    <CharacterInspector
      v-if="inspectingId && store.current"
      :project-id="store.current.project_id"
      :character-id="inspectingId"
      @close="inspectingId = ''"
    />
  </div>
</template>

<style scoped>
.workspace h1 {
  margin-bottom: 18px;
}
.grid {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 18px;
}
.grid.bottom {
  grid-template-columns: 1.4fr 1fr;
  margin-top: 18px;
}
.project-list {
  list-style: none;
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.project-list li {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
}
.project-list li:hover,
.project-list li.active {
  background: var(--accent);
  border-color: var(--border);
}
.p-name {
  font-weight: 600;
}
.seed-box,
.build-box {
  margin-top: 18px;
}
.goal-box {
  margin-top: 18px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.goal-text {
  margin-top: 8px;
  font-size: 14px;
  line-height: 1.6;
}
.seed-list {
  list-style: none;
  margin-top: 8px;
  font-size: 13px;
}
.progress {
  margin-top: 12px;
}
.progress-bar {
  height: 6px;
  background: var(--highlight);
  border-radius: 3px;
  transition: width 0.4s;
}
.char-progress {
  margin-left: 10px;
}
.generating {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 18px;
  border: 1px dashed var(--border);
  border-radius: 8px;
}
.graph-card {
  height: 480px;
  display: flex;
  flex-direction: column;
}
.graph-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.graph-switch {
  display: inline-flex;
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
}
.graph-switch button {
  border: 0;
  padding: 4px 8px;
  background: transparent;
  color: var(--text-dim);
  font-size: 12px;
}
.graph-switch button.active {
  background: var(--accent);
  color: var(--text);
}
.char-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 12px;
  max-height: 420px;
  overflow-y: auto;
}
.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
