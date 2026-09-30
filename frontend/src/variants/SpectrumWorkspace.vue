<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
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
const projectSearch = ref('')
const filteredProjects = computed(() => store.projects.filter(p => p.name.toLocaleLowerCase().includes(projectSearch.value.trim().toLocaleLowerCase())))

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

    <div v-if="loadError" class="notice danger load-notice" role="alert">{{ loadError }}<button class="ghost" @click="loadWorkspace">重试</button></div>

    <div class="ws">
      <!-- 左：项目列表 -->
      <aside class="projects">
        <div class="section-title">
          <span>项目库 <span class="project-count">{{ store.projects.length }}</span></span>
          <button class="icon sm" title="新建项目" @click="showCreate = !showCreate"><Icon name="plus" :size="15" /></button>
        </div>
        <div class="project-search"><Icon name="search" :size="15" /><input v-model="projectSearch" type="search" aria-label="搜索项目" placeholder="搜索项目" /></div>
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
          <li v-for="p in filteredProjects" :key="p.project_id">
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
        <p v-if="projectSearch && !filteredProjects.length" class="library-message dim" role="status">没有匹配的项目。</p>
        <div class="library-note"><Icon name="private" :size="14" /><span>每个世界，都有独立的角色与记忆。</span></div>
      </aside>

      <!-- 右：当前项目 -->
      <section v-if="store.current" class="detail" aria-label="项目详情">
        <header class="detail-head">
          <p class="eyebrow">项目概览</p>
          <h2>{{ store.current.name }}</h2>
          <p v-if="store.current.description" class="dim">{{ store.current.description }}</p>
          <p v-else class="dim">管理故事素材、世界关系与角色，准备下一场推演。</p>
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
            <span>知识图谱 <span class="panel-subtitle">世界中的人物、地点与联系</span></span>
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
          <span class="welcome-symbol"><Icon name="director" :size="30" /></span>
          <p class="eyebrow">开始创作</p>
          <h3>把故事的可能，变成作品。</h3>
          <p class="welcome-copy">集中管理素材、角色与剧情分支。<br />创建一个项目，或从项目库继续上一次创作。</p>
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
.workspace{min-height:100%;display:flex;flex-direction:column}
.ws{flex:1;display:grid;grid-template-columns:232px minmax(0,1fr);min-height:0}
.projects{padding:20px 14px;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0}
.projects .section-title{margin:0 0 14px}
.project-count{margin-left:6px;font-size:11px;color:var(--ink-2);border:1px solid var(--line);padding:1px 5px;border-radius:3px}
.project-search{position:relative;margin-bottom:18px}
.project-search>svg{position:absolute;top:11px;left:10px;color:var(--ink-3);pointer-events:none}
.project-search input{padding-left:32px;font-size:12px}
.create{padding:14px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel-2);margin-bottom:14px}
.create-actions{display:flex;justify-content:flex-end;gap:8px}
.project-list{list-style:none}
.project-list li{position:relative;margin-bottom:5px}
.project{width:100%;min-height:58px;height:auto;display:flex;flex-direction:column;align-items:flex-start;justify-content:center;gap:3px;padding:10px 34px 10px 12px;border:1px solid transparent;background:transparent;text-align:left}
.project:hover{background:var(--hover)}
.project[aria-current='true']{background:var(--spot-soft);border-color:transparent;color:var(--spot-ink)}
.p-name{font-weight:600;font-size:13px;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.p-status{font-size:11px}
.remove{position:absolute;right:4px;top:50%;transform:translateY(-50%);opacity:.65}
.remove:focus-visible,.project-list li:hover .remove{opacity:1}
.library-note{display:flex;gap:8px;padding-top:18px;margin-top:40px;border-top:1px solid var(--line);color:var(--ink-3);font-size:11px;line-height:1.8}
.library-note svg{flex-shrink:0;margin-top:2px}
.library-message{font-size:12px;padding:12px 4px}
.library-empty{padding:26px 5px;text-align:center;color:var(--ink-2)}
.library-empty p{font-size:12px;margin:12px 0}
.detail{padding:28px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-content:start;gap:18px;min-width:0;max-width:1500px;width:100%;margin-inline:auto}
.detail-head,.project-metrics,.graph-panel,.detail>section:last-child{grid-column:1 / -1}
.eyebrow{font-size:11px;font-weight:600;color:var(--ink-2);letter-spacing:.5px}
.detail-head h2{font-size:27px;line-height:1.4;margin:8px 0;font-weight:650;letter-spacing:-.6px}
.detail-head .dim{font-size:12px}
.project-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.project-metrics>div{padding:18px 22px;display:grid;grid-template-columns:22px auto;column-gap:12px;align-items:center}
.project-metrics>div+div{border-left:1px solid var(--line)}
.project-metrics svg{grid-row:span 2;color:var(--ink-3)}
.project-metrics strong{font-size:24px;line-height:1.2;font-weight:600;font-variant-numeric:tabular-nums}
.project-metrics span{color:var(--ink-2);font-size:11px;margin-top:5px}
.panel{padding:20px;border:1px solid var(--line);border-radius:8px;background:var(--panel);min-width:0}
.panel .section-title{padding-bottom:12px;border-bottom:1px solid var(--line);margin-bottom:16px}
.panel-subtitle{font-weight:400;font-size:11px;color:var(--ink-2);margin-left:12px}
.small-btn{min-height:28px;height:28px;padding:0 8px;font-weight:400;font-size:12px}
.goal-text{font-size:14px;line-height:1.8;white-space:pre-wrap}
.criteria{margin-top:8px;font-size:12px}
.hint{margin-top:12px;font-size:11px}
.actions{justify-content:flex-end;gap:8px}
.seeds{list-style:none;display:flex;flex-direction:column;gap:6px;font-size:12px}
.seeds li{display:flex;align-items:center;gap:8px;padding:8px;border:1px solid var(--line);border-radius:4px;background:var(--panel-2);overflow-wrap:anywhere}
.build{margin-top:16px;display:flex;flex-direction:column;align-items:flex-start;gap:10px}
.build-progress{width:100%;display:flex;flex-direction:column;gap:8px;font-size:11px}
.graph-panel{display:flex;flex-direction:column}
.graph-body{height:460px;display:flex;flex-direction:column;background:var(--panel-2);border-radius:5px;overflow:hidden}
.char-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(240px,100%),1fr));gap:12px}
.generating{display:flex;align-items:center;justify-content:center;gap:8px;padding:18px;border:1px dashed var(--line-strong);border-radius:6px}
.detail.empty{display:flex;flex-direction:column;gap:24px}
.welcome-panel{padding:42px;background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--brand);border-radius:8px}
.welcome-symbol{display:inline-grid;place-items:center;width:58px;height:58px;border-radius:12px;background:var(--brand-soft);color:var(--brand-ink);margin-bottom:28px}
.welcome-panel h3{font-size:clamp(24px,2.4vw,34px);margin-block:12px;font-weight:650;letter-spacing:-.7px}
.welcome-copy{color:var(--ink-2);font-size:13px;line-height:1.9}
.welcome-cta{margin-top:26px;height:38px;padding-inline:18px;gap:12px;border-radius:20px}
.welcome-hint{display:block;font-size:11px;color:var(--ink-3);margin-top:12px}
.journey-heading{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.journey-heading h3{font-size:15px}
.journey-heading span{font-size:11px;color:var(--ink-2)}
.journey{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.journey article{position:relative;padding:22px;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.step-icon{color:var(--ink-2)}
.step-number{position:absolute;right:20px;top:22px;font-size:11px;color:var(--ink-3)}
.journey h4{font-size:13px;margin-top:20px;margin-bottom:10px}
.journey p{font-size:12px;line-height:1.8;color:var(--ink-2)}
.step-caption{display:block;padding-top:18px;margin-top:20px;border-top:1px solid var(--line);color:var(--ink-3);font-size:10px}
.load-notice{border-radius:0;align-items:center;justify-content:space-between}
@media(max-width:1200px){.ws{grid-template-columns:210px minmax(0,1fr)}.detail{padding:22px}.detail:not(.empty){grid-template-columns:minmax(0,1fr)}.journey{grid-template-columns:minmax(0,1fr)}}
@media(max-width:800px){.ws{grid-template-columns:minmax(0,1fr)}.projects{border-right:0;border-bottom:1px solid var(--line);padding:16px}.project-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(180px,100%),1fr));gap:8px}.project-search{margin-bottom:10px}.library-note{display:none}.project{min-height:50px}.detail{padding:18px}.welcome-panel{padding:26px}}
@media(max-width:480px){.detail{padding:14px;gap:14px}.project-metrics>div{padding:14px 8px;column-gap:5px}.project-metrics strong{font-size:20px}.panel{padding:16px}.panel-subtitle{display:none}.journey-heading{flex-wrap:wrap}.graph-body{height:420px}}
</style>
