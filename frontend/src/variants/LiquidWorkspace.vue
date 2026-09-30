<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore, getLastProjectId } from '@/stores/project'
import { useCharacterStore } from '@/stores/characters'
import GraphViewer from '@/components/GraphViewer.vue'
import GraphViewer2 from '@/components/GraphViewer2.vue'
import CharacterCardView from '@/components/CharacterCard.vue'
import CharacterInspector from '@/components/CharacterInspector.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import Icon from '@/components/ui/Icon.vue'

const router = useRouter()
const store = useProjectStore()
const charStore = useCharacterStore()

const inspectingId = ref('')
const showCreate = ref(false)
const loadingProjects = ref(true)
const loadError = ref('')
const creating = ref(false)
const createError = ref('')

async function loadWorkspace() {
  loadingProjects.value = true
  loadError.value = ''
  try {
    await store.loadProjects()
    const lastId = getLastProjectId()
    if (lastId && store.projects.some((p) => p.project_id === lastId)) await open(lastId)
  } catch {
    loadError.value = '暂时无法加载项目，请确认服务已启动后重试。'
  } finally {
    loadingProjects.value = false
  }
}

async function beginCreate() {
  showCreate.value = true
  await nextTick()
  document.getElementById('np-name')?.focus()
}

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

onMounted(loadWorkspace)

onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})

async function create() {
  if (!newName.value.trim() || creating.value) return
  creating.value = true
  createError.value = ''
  try {
    const p = await store.createProject({
      name: newName.value,
      description: newDesc.value,
      narrative_goal: newGoal.value,
    })
    newName.value = ''
    newDesc.value = ''
    newGoal.value = ''
    await open(p.project_id)
    showCreate.value = false
  } catch {
    createError.value = '创建或打开项目失败，请检查连接后重试。'
  } finally {
    creating.value = false
  }
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
    <PageHeader title="工作台" context="创作空间">
      <button class="ghost" @click="beginCreate"><Icon name="plus" :size="16" />新建项目</button>
      <button
        v-if="store.current"
        class="primary"
        @click="router.push(`/director/${store.current.project_id}`)"
      >
        <Icon name="director" :size="15" />进入导演台
      </button>
    </PageHeader>

    <section class="workspace-intro">
      <div><p class="eyebrow">YOUR STORY, UNFOLDED</p><h2>让灵感，流向无限可能。</h2><p class="dim">构建世界，赋予角色生命，探索故事的每一种走向。</p></div>
      <span class="intro-signature"><Icon name="generate" :size="16" /> 剧情推演工作空间</span>
    </section>

    <div v-if="loadError" class="notice danger load-notice" role="alert">{{ loadError }}<button class="ghost" @click="loadWorkspace">重试</button></div>

    <div class="ws">
      <!-- 左：项目列表 -->
      <aside class="projects">
        <div class="section-title">
          <span>项目库 <span class="project-count">{{ store.projects.length }}</span></span>
          <button class="icon sm" title="新建项目" @click="showCreate = !showCreate"><Icon name="plus" :size="15" /></button>
        </div>
        <p v-if="loadingProjects" class="dim library-message" role="status">正在加载项目…</p>
        <form v-if="showCreate" class="create" @submit.prevent="create">
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
            <button type="button" class="ghost" @click="showCreate = false">取消</button>
            <button type="submit" class="primary" :disabled="!newName.trim() || creating">{{ creating ? '创建中…' : '创建' }}</button>
          </div>
          <p v-if="createError" class="notice danger" role="alert">{{ createError }}</p>
        </form>
        <div v-if="!loadingProjects && !store.projects.length && !showCreate" class="library-empty"><Icon name="file" :size="25" /><p>你的下一部作品<br />从这里开始</p><button class="ghost" @click="beginCreate"><Icon name="plus" :size="14" />创建项目</button></div>
        <ul class="project-list">
          <li v-for="p in store.projects" :key="p.project_id">
            <button
              class="project"
              :aria-current="store.current?.project_id === p.project_id"
              @click="open(p.project_id)"
            >
              <span class="p-name">{{ p.name }}</span>
              <span class="p-status dim">{{ ({ ready: '已就绪', building: '构建中', created: '待构建', initialized: '待构建', error: '构建失败' } as Record<string, string>)[p.status] || p.status }}</span>
            </button>
            <button class="icon sm danger remove" title="删除项目" @click="removeProject(p.project_id, p.name)">
              <Icon name="trash" :size="14" />
            </button>
          </li>
        </ul>
        <div class="library-note"><Icon name="private" :size="14" /><span>每个世界，都有独立的角色与记忆。</span></div>
      </aside>

      <!-- 右：当前项目 -->
      <section v-if="store.current" class="detail" aria-label="项目详情">
        <header class="detail-head">
          <h2>{{ store.current.name }}</h2>
          <p v-if="store.current.description" class="dim">{{ store.current.description }}</p>
        </header>

        <div class="project-metrics">
          <div><Icon name="file" :size="18" /><strong>{{ store.current.seed_texts.length }}</strong><span>种子文本</span></div>
          <div><Icon name="world" :size="18" /><strong>{{ store.graph.nodes.length }}</strong><span>世界实体</span></div>
          <div><Icon name="users" :size="18" /><strong>{{ charStore.characters.length }}</strong><span>故事角色</span></div>
        </div>

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
      </section>
      <section v-else class="detail empty" aria-label="开始创作">
        <section class="welcome-panel">
          <div class="glass-art" aria-hidden="true"><div class="orbit orbit-one"></div><div class="orbit orbit-two"></div><div class="art-tile tile-back"><Icon name="branches" :size="42" /></div><div class="art-tile tile-front"><Icon name="world" :size="62" /></div><span class="art-spark spark-one"></span><span class="art-spark spark-two"></span></div>
          <p class="eyebrow">A SPACE FOR POSSIBILITIES</p>
          <h3>一个想法，一整个世界。</h3>
          <p class="welcome-copy">从一段文字开始，让角色相遇、让情节生长。<br />你的故事，还可以有另一种可能。</p>
          <button class="primary welcome-cta" @click="beginCreate"><Icon name="plus" :size="17" />创建你的故事<Icon name="continue" :size="17" /></button>
          <span class="welcome-hint">或从项目库选择一个世界，继续创作</span>
        </section>
        <div class="journey-heading"><h3>从灵感，到故事</h3><span>三个步骤，开启创作</span></div>
        <div class="journey">
          <article><span class="step-icon"><Icon name="file" :size="21" /></span><span class="step-number">01</span><h4>播下故事的种子</h4><p>导入小说、剧本或世界观设定，让灵感有迹可循。</p><span class="step-caption">文本 · 世界观 · 设定</span></article>
          <article><span class="step-icon"><Icon name="world" :size="21" /></span><span class="step-number">02</span><h4>让世界鲜活起来</h4><p>构建知识图谱与角色，让每个人物拥有独立的记忆。</p><span class="step-caption">图谱 · 角色 · 记忆</span></article>
          <article><span class="step-icon"><Icon name="branches" :size="21" /></span><span class="step-number">03</span><h4>探索每一种可能</h4><p>在导演台推进剧情，创建分支，找到属于你的结局。</p><span class="step-caption">推演 · 分支 · 导出</span></article>
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
.workspace {
  min-height: 100%;
  display: flex;
  flex-direction: column;
}
.ws {
  flex: 1;
  display: grid;
  grid-template-columns: 238px minmax(0, 1fr);
  gap: 22px;
  min-height: 0;
}
.projects {
  border: 1px solid var(--glass-edge);
  border-radius: 24px;
  background: var(--glass);
  box-shadow: var(--glass-shadow);
  padding: 22px 12px;
  display: flex;
  flex-direction: column;
  align-self: start;
  min-height: 440px;
}
.projects .section-title {
  padding: 0 8px;
}
.create {
  margin: 8px 0 14px;
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
  min-height: 64px;
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
  background: var(--active-glass);
  box-shadow: inset 0 0 0 1px var(--glass-edge);
  color: var(--spot-ink);
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
  opacity: .6;
}
.project-list li:hover .remove,
.remove:focus-visible {
  opacity: 1;
}
.detail {
  padding: 0 0 24px;
  max-width: none;
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.detail.empty {
  align-items: stretch;
  justify-content: flex-start;
  color: var(--ink);
  max-width: none;
}
.detail-head h2 {
  font-size: 22px;
}
.panel {
  padding: 24px;
  border: 1px solid var(--glass-edge);
  border-radius: 22px;
  background: var(--material);
  box-shadow: var(--glass-shadow);
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
    min-height: auto;
  }
  .detail {
    padding: 0 0 24px;
  }
}
.workspace { padding: 0 4px 12px; }
.workspace-intro { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 30px 4px 26px; }
.eyebrow { font-size: 10px; letter-spacing: 2.6px; color: var(--spot-ink); font-weight: 600; }
.workspace-intro h2 { font-size: clamp(22px, 2.1vw, 30px); letter-spacing: -.8px; margin: 6px 0; }
.workspace-intro .dim { font-size: 13px; }
.intro-signature { display: inline-flex; gap: 8px; align-items: center; font-size: 11px; color: var(--ink-2); white-space: nowrap; padding: 8px 12px; border: 1px solid var(--glass-edge); border-radius: 30px; background: var(--glass); }
.project-count { display: inline-flex; align-items: center; justify-content: center; min-width: 22px; padding: 0 6px; margin-left: 6px; border-radius: 8px; background: var(--spot-soft); color: var(--spot-ink); font-size: 11px; }
.library-empty { text-align: center; padding: 48px 4px 32px; color: var(--ink-2); }
.library-empty > .ui-icon { color: var(--spot-ink); }
.library-empty p { margin: 14px 0; line-height: 1.9; font-size: 13px; }
.library-note { display: flex; align-items: flex-start; gap: 7px; padding: 20px 8px 0; margin-top: auto; color: var(--ink-2); font-size: 11px; border-top: 1px solid var(--line); }
.library-note svg { flex-shrink: 0; margin-top: 2px; }
.library-message { padding: 16px; }
.project-list { margin-bottom: 24px; }
.welcome-panel { position: relative; overflow: hidden; text-align: center; border: 1px solid var(--glass-edge); border-radius: 28px; background: radial-gradient(ellipse at 50% 5%, var(--ambient-a), transparent 65%), var(--glass); box-shadow: var(--glass-shadow); padding: 18px 20px 30px; }
.welcome-panel h3 { font-size: clamp(23px, 2.1vw, 30px); margin: 10px 0; letter-spacing: -.7px; }
.welcome-copy { color: var(--ink-2); font-size: 13px; line-height: 1.9; }
.welcome-cta { height: 44px; padding: 0 24px; gap: 12px; margin: 22px 0 10px; border-radius: 24px; }
.welcome-hint { display: block; color: var(--ink-2); font-size: 11px; }
.glass-art { position: relative; width: 280px; height: 180px; margin: 0 auto 8px; }
.art-tile { position: absolute; display: grid; place-items: center; border: 1px solid var(--glass-edge); background: linear-gradient(135deg, var(--glass), var(--spot-soft)); color: var(--spot-ink); box-shadow: inset 2px 2px 2px var(--glass-edge), inset -2px -2px 8px var(--spot-soft), 0 18px 34px var(--spot-glow); backdrop-filter: blur(6px); }
.tile-front { width: 116px; height: 116px; border-radius: 30px; left: 72px; top: 36px; transform: rotate(-12deg); }
.tile-back { width: 86px; height: 86px; border-radius: 24px; left: 159px; top: 18px; transform: rotate(17deg); color: var(--private); }
.orbit { position: absolute; width: 252px; height: 112px; border: 1px solid var(--glass-edge); border-radius: 50%; left: 14px; top: 36px; transform: rotate(-22deg); }
.orbit-two { transform: rotate(28deg); width: 226px; left: 30px; }
.art-spark { position: absolute; width: 12px; height: 12px; border-radius: 50%; background: var(--glass-edge); box-shadow: 0 0 18px var(--spot-glow); }
.spark-one { top: 40px; left: 40px; }
.spark-two { top: 134px; right: 30px; width: 8px; height: 8px; }
.journey-heading { display: flex; justify-content: space-between; align-items: center; margin: 9px 2px 0; gap: 8px; }
.journey-heading h3 { font-size: 14px; }
.journey-heading span { font-size: 11px; color: var(--ink-2); }
.journey { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.journey article { position: relative; padding: 20px; border: 1px solid var(--glass-edge); border-radius: 20px; background: var(--glass); box-shadow: var(--glass-shadow); }
.step-icon { display: inline-grid; place-items: center; width: 42px; height: 42px; border-radius: 14px; background: var(--active-glass); color: var(--spot-ink); border: 1px solid var(--glass-edge); }
.step-number { position: absolute; top: 24px; right: 20px; color: var(--ink-3); font-size: 11px; letter-spacing: 1px; }
.journey h4 { font-size: 13px; margin: 15px 0 8px; }
.journey p { color: var(--ink-2); font-size: 12px; line-height: 1.8; }
.step-caption { display: block; font-size: 10px; color: var(--spot-ink); margin-top: 18px; }
.project-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.project-metrics > div { display: flex; align-items: center; gap: 12px; padding: 18px; background: var(--glass); border: 1px solid var(--glass-edge); border-radius: 18px; }
.project-metrics svg { color: var(--spot-ink); }
.project-metrics strong { font-size: 22px; font-variant-numeric: tabular-nums; }
.project-metrics span { font-size: 12px; color: var(--ink-2); }
.load-notice { margin-bottom: 16px; align-items: center; justify-content: space-between; }
@media (max-width: 1200px) { .intro-signature { display: none; } .journey article { padding: 16px; } }
@media (max-width: 900px) { .library-empty { padding: 12px; } .library-empty p { display: inline-block; margin: 0 16px; vertical-align: middle; } .library-note { margin-top: 16px; } }
@media (max-width: 600px) {
  .workspace-intro { padding: 24px 4px; }
  .workspace-intro h2 { font-size: 22px; }
  .journey { grid-template-columns: 1fr; }
  .journey-heading { align-items: flex-start; flex-direction: column; }
  .welcome-panel { padding-inline: 12px; }
  .glass-art { width: 240px; transform: scale(.85); transform-origin: center; margin-left: calc(50% - 120px); }
  .project-metrics { gap: 6px; }
  .project-metrics > div { flex-direction: column; gap: 3px; padding: 12px 4px; }
  .panel { padding: 16px; }
}
</style>
