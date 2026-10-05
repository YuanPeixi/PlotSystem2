<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore, getLastProjectId } from '@/stores/project'
import { useCharacterStore } from '@/stores/characters'
import GraphViewer from '@/components/GraphViewer.vue'
import GraphViewer2 from '@/components/GraphViewer2.vue'
import CharacterCardView from '@/components/CharacterCard.vue'
import CharacterInspector from '@/components/CharacterInspector.vue'
import ObjectsPanel from '@/components/ObjectsPanel.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import Icon from '@/components/ui/Icon.vue'

const router = useRouter()
const store = useProjectStore()
const charStore = useCharacterStore()

const inspectingId = ref('')
const showCreate = ref(false)

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
// 构建完成会抽出新物件：递增它让物件面板重新拉列表
const objectsRefresh = ref(0)
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
  showCreate.value = false
  await open(p.project_id)
}

// 删除会连带清空整个项目目录（角色卡、快照、向量库），不可恢复
async function removeProject(id: string, name: string) {
  if (!confirm(`删除项目「${name}」？它的角色、场景、快照和记忆都会被一并删除，无法恢复。`)) return
  await store.deleteProject(id)
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
      objectsRefresh.value++
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
    <PageHeader title="工作台">
      <button
        v-if="store.current"
        class="primary"
        @click="router.push(`/director/${store.current.project_id}`)"
      >
        <Icon name="director" :size="15" />进入导演台
      </button>
    </PageHeader>

    <div class="ws">
      <!-- 左：项目列表 -->
      <aside class="projects">
        <div class="section-title">
          项目
          <button class="icon sm" title="新建项目" @click="showCreate = !showCreate"><Icon name="plus" :size="15" /></button>
        </div>
        <form v-if="showCreate || !store.projects.length" class="create" @submit.prevent="create">
          <div class="field">
            <label for="np-name">名称</label>
            <input id="np-name" v-model="newName" placeholder="例如：雪夜长安" />
          </div>
          <div class="field">
            <label for="np-desc">简述（可选）</label>
            <input id="np-desc" v-model="newDesc" />
          </div>
          <div class="field">
            <label for="np-goal">主线目标（可选，之后也能改）</label>
            <textarea id="np-goal" v-model="newGoal" rows="2" placeholder="故事最终要走到哪里"></textarea>
          </div>
          <div class="create-actions">
            <button v-if="store.projects.length" type="button" class="ghost" @click="showCreate = false">取消</button>
            <button type="submit" class="primary" :disabled="!newName.trim()">新建项目</button>
          </div>
        </form>
        <ul class="project-list">
          <li v-for="p in store.projects" :key="p.project_id">
            <button
              class="project"
              :aria-current="store.current?.project_id === p.project_id"
              @click="open(p.project_id)"
            >
              <span class="p-name">{{ p.name }}</span>
              <span class="p-status dim">{{ p.status }}</span>
            </button>
            <button class="icon sm danger remove" title="删除项目" @click="removeProject(p.project_id, p.name)">
              <Icon name="trash" :size="14" />
            </button>
          </li>
        </ul>
      </aside>

      <!-- 右：当前项目 -->
      <main v-if="store.current" class="detail">
        <header class="detail-head">
          <h2>{{ store.current.name }}</h2>
          <p v-if="store.current.description" class="dim">{{ store.current.description }}</p>
        </header>

        <section class="panel">
          <div class="section-title">
            主线目标
            <button v-if="!editingGoal" class="ghost small-btn" @click="startEditGoal"><Icon name="edit" :size="14" />编辑</button>
          </div>
          <template v-if="editingGoal">
            <div class="field">
              <textarea v-model="goalDraft" rows="3" placeholder="例如：王子查明丞相通敌，并最终把他拉下马"></textarea>
            </div>
            <div class="field">
              <label>结局判定标准（可选）</label>
              <textarea v-model="criteriaDraft" rows="2" placeholder="什么情形算故事讲完了"></textarea>
            </div>
            <div class="row actions">
              <button class="ghost" @click="cancelEditGoal">取消</button>
              <button class="primary" :disabled="savingGoal" @click="saveGoal">{{ savingGoal ? '保存中' : '保存' }}</button>
            </div>
          </template>
          <template v-else>
            <p v-if="store.current.narrative_goal" class="goal-text">{{ store.current.narrative_goal }}</p>
            <p v-else class="dim">还没有设定。没有它，导演只能顺着上一场的惯性往下演，也无法判定结局。</p>
            <p v-if="store.current.ending_criteria" class="dim criteria">结局标准：{{ store.current.ending_criteria }}</p>
            <p class="dim hint">导演只读这个目标，不会改写它。</p>
          </template>
        </section>

        <section class="panel">
          <div class="section-title">
            种子文本
            <button class="ghost small-btn" @click="fileInput?.click()"><Icon name="upload" :size="14" />上传</button>
            <input ref="fileInput" type="file" multiple accept=".txt,.md" hidden @change="onUpload" />
          </div>
          <ul v-if="store.current.seed_texts.length" class="seeds">
            <li v-for="(s, i) in store.current.seed_texts" :key="i">
              <Icon name="file" :size="15" />{{ s.split(/[\\/]/).pop() }}
            </li>
          </ul>
          <p v-else class="dim">上传小说、剧本或世界观设定（.txt / .md），构建时会从中抽取角色与世界规则。</p>
          <div class="build">
            <button :disabled="building || !store.current.seed_texts.length" @click="build">
              <Icon :name="building ? 'spinner' : 'build'" :size="15" />{{ building ? '构建中' : '构建知识图谱与角色' }}
            </button>
            <div v-if="building || store.buildStatus.progress > 0" class="build-progress">
              <span class="meter"><i :style="{ width: store.buildStatus.progress * 100 + '%', background: 'var(--spot)' }"></i></span>
              <span class="dim">
                {{ store.buildStatus.stage }}
                <template v-if="store.buildStatus.character_total">
                  ，角色卡 <span class="num">{{ store.buildStatus.character_done ?? 0 }} / {{ store.buildStatus.character_total }}</span>
                </template>
              </span>
            </div>
          </div>
        </section>

        <section class="panel graph-panel">
          <div class="section-title">
            知识图谱
            <div class="seg" role="group" aria-label="图谱查看器版本">
              <button :aria-pressed="graphViewerVersion === 'legacy'" @click="graphViewerVersion = 'legacy'">概览</button>
              <button :aria-pressed="graphViewerVersion === 'focused'" @click="graphViewerVersion = 'focused'">聚焦</button>
            </div>
          </div>
          <div class="graph-body">
            <GraphViewer v-if="graphViewerVersion === 'legacy'" :data="store.graph" />
            <GraphViewer2 v-else :data="store.graph" />
          </div>
        </section>

        <section class="panel">
          <div class="section-title">
            <span>
              角色 <span class="num">{{ charStore.characters.length }}<template
                v-if="building && store.buildStatus.character_total"
              > / {{ store.buildStatus.character_total }}</template></span>
            </span>
          </div>
          <div class="char-grid">
            <CharacterCardView
              v-for="c in charStore.characters"
              :key="c.character_id"
              :character="c"
              @inspect="inspectingId = $event"
            />
            <div
              v-if="building && store.buildStatus.character_total && charStore.characters.length < store.buildStatus.character_total"
              class="generating dim"
            >
              <Icon name="spinner" :size="15" />正在生成角色卡
            </div>
          </div>
          <p v-if="!charStore.characters.length && !building" class="dim">构建完成后会自动生成角色。</p>
        </section>

        <section class="panel">
          <ObjectsPanel
            :project-id="store.current.project_id"
            :characters="charStore.characters"
            :refresh-key="objectsRefresh"
          />
        </section>
      </main>
      <main v-else class="detail empty">
        <Icon name="workspace" :size="28" />
        <p class="dim">从左侧选择一个项目，或者新建一个。</p>
      </main>
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
.workspace {
  min-height: 100%;
  display: flex;
  flex-direction: column;
}
.ws {
  flex: 1;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  min-height: 0;
}
.projects {
  border-right: 1px solid var(--line);
  padding: 14px 10px 24px;
}
.projects .section-title {
  padding: 0 8px;
}
.create {
  margin: 0 8px 14px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--panel);
}
.create-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.project-list {
  list-style: none;
}
.project-list li {
  position: relative;
}
.project {
  width: 100%;
  height: auto;
  min-height: 44px;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 0;
  padding: 6px 36px 6px 10px;
  border: 0;
  background: transparent;
  text-align: left;
}
.project:hover {
  background: var(--hover);
}
.project[aria-current='true'] {
  background: var(--panel);
  box-shadow: 0 0 0 1px var(--line);
}
.p-name {
  font-weight: 600;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.p-status {
  font-size: 12px;
}
.remove {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  opacity: 0;
}
.project-list li:hover .remove,
.remove:focus-visible {
  opacity: 1;
}
.detail {
  padding: 28px 32px 48px;
  max-width: 1080px;
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.detail.empty {
  align-items: center;
  justify-content: center;
  color: var(--ink-2);
  max-width: none;
}
.detail-head h2 {
  font-size: 22px;
}
.panel {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--panel);
}
.small-btn {
  height: 26px;
  padding: 0 8px;
  font-weight: 400;
}
.goal-text {
  font-size: 14px;
  line-height: 1.75;
  white-space: pre-wrap;
}
.criteria {
  margin-top: 8px;
  font-size: 13px;
}
.hint {
  margin-top: 8px;
  font-size: 12px;
}
.actions {
  justify-content: flex-end;
  gap: 8px;
}
.seeds {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.seeds li {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--ink-2);
}
.build {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
}
.build-progress {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12.5px;
}
.graph-panel {
  display: flex;
  flex-direction: column;
}
.graph-body {
  height: 460px;
  display: flex;
  flex-direction: column;
}
.char-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 10px;
}
.generating {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 18px;
  border: 1px dashed var(--line-strong);
  border-radius: var(--r-md);
}
@media (max-width: 900px) {
  .ws {
    grid-template-columns: minmax(0, 1fr);
  }
  .projects {
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  .detail {
    padding: 20px 16px 40px;
  }
}
</style>
