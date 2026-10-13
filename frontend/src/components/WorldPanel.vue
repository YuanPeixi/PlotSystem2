<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { CharacterCard, LegacyWorldState, WorldEvent, WorldFact, WorldMigrationPreview, WorldObject, WorldOperation, WorldRevision, WorldValue } from '@/types'

const props = defineProps<{ projectId: string; branchId: string; characters: CharacterCard[]; objects: WorldObject[]; busy?: boolean; refreshKey?: unknown }>()
const emit = defineEmits<{ (e: 'migrated'): void }>()
const world = ref<WorldRevision | null>(null)
const legacy = ref<LegacyWorldState | null>(null)
const history = ref<WorldEvent[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const editing = ref(false)
const kind = ref<WorldFact['subject_kind']>('world')
const subject = ref('world')
const attribute = ref('')
const label = ref('')
const value = ref('')
const valueType = ref<'text' | 'boolean' | 'number' | 'character' | 'object' | 'location'>('text')
const visibility = ref<WorldFact['visibility']>('global')
const observers = ref<string[]>([])
const deleting = ref(false)
const preview = ref<WorldMigrationPreview | null>(null)
const migrationSelection = ref<Record<string, boolean>>({})
let sequence = 0
let generation = 0
let disposed = false
let requestKey = ''
let requestId = ''
const facts = computed(() => Object.values(world.value?.facts || {}))
const visibilityLabels = { global: '广泛公开', observable: '现场可观察', hidden: '隐藏' }
const categories: Record<string, string> = { unclassified: '含义待确认', object_candidate: '物件状态候选', knowledge_candidate: '个人认知', disclosure_candidate: '言论或传播记录' }

function name(fact: WorldFact): string {
  return fact.subject_kind === 'world' ? '世界' : world.value?.definitions[fact.subject_id]?.name || fact.subject_id
}
function showValue(raw: WorldValue | unknown): string {
  if (typeof raw === 'boolean') return raw ? '是' : '否'
  if (raw && typeof raw === 'object' && 'kind' in raw && 'id' in raw) {
    const id = String(raw.id)
    return world.value?.definitions[id]?.name || props.characters.find(c => c.character_id === id)?.name || id
  }
  return raw === null ? '未知' : String(raw)
}
function idempotency(payload: unknown): string {
  const key = JSON.stringify(payload)
  if (key !== requestKey) { requestKey = key; requestId = crypto.randomUUID() }
  return requestId
}
async function load() {
  const seq = ++sequence
  const pid = props.projectId, bid = props.branchId
  if (!bid) { world.value = null; legacy.value = null; loading.value = false; return }
  loading.value = true
  error.value = ''
  try {
    const [state, events] = await Promise.all([api.getWorldState(pid, bid), api.getWorldEvents(pid, bid)])
    if (disposed || seq !== sequence) return
    if ('schema_version' in state) { world.value = state; legacy.value = null }
    else { legacy.value = state; world.value = null }
    history.value = events
  } catch (err) { if (!disposed && seq === sequence) error.value = err instanceof Error ? err.message : '读取世界失败' }
  finally { if (!disposed && seq === sequence) loading.value = false }
}
watch(() => [props.projectId, props.branchId], () => {
  generation++; world.value = null; legacy.value = null; history.value = []; editing.value = false; preview.value = null; migrationSelection.value = {}; requestKey = ''; saving.value = false
  void load()
}, { immediate: true })
watch(() => props.refreshKey, () => { if (!editing.value && !saving.value) void load() })
onBeforeUnmount(() => { disposed = true; sequence++ })

function edit(fact?: WorldFact) {
  if (props.busy || saving.value) return
  editing.value = true; error.value = ''; deleting.value = false
  kind.value = fact?.subject_kind || 'world'; subject.value = fact?.subject_id || 'world'
  attribute.value = fact?.attribute || ''; label.value = fact?.label || ''
  const raw = fact?.value
  valueType.value = typeof raw === 'boolean' ? 'boolean' : typeof raw === 'number' ? 'number' : raw && typeof raw === 'object' ? raw.kind : 'text'
  value.value = raw && typeof raw === 'object' ? raw.id : raw == null ? '' : String(raw)
  visibility.value = fact?.visibility || 'global'; observers.value = []
}
async function save() {
  if (!world.value || saving.value || props.busy) return
  let typed: WorldValue = deleting.value ? null : value.value
  if (!deleting.value) {
  if (valueType.value === 'boolean') {
    if (!['true', 'false'].includes(value.value)) { error.value = '请选择是或否'; return }
    typed = value.value === 'true'
  }
  if (valueType.value === 'number') {
    typed = Number(value.value)
    if (!value.value.trim() || !Number.isFinite(typed)) { error.value = '请输入有效数字'; return }
  }
  if (valueType.value === 'character' || valueType.value === 'object' || valueType.value === 'location') typed = { kind: valueType.value, id: value.value }
  }
  const operation: WorldOperation = { op: deleting.value ? 'unset' : 'set', subject_kind: kind.value,
    subject_id: kind.value === 'world' ? 'world' : subject.value, attribute: attribute.value,
    label: label.value || attribute.value, value: typed, visibility: visibility.value }
  const payload = { revision_id: world.value.revision_id, operations: [operation], observers: observers.value }
  const seq = generation, pid = props.projectId, bid = props.branchId
  saving.value = true; error.value = ''
  try {
    await api.worldCommand(pid, bid, { ...payload, request_id: idempotency(payload) })
    if (disposed || seq !== generation) return
    editing.value = false; requestKey = ''; await load()
  } catch (err) { if (!disposed && seq === generation) error.value = err instanceof Error ? err.message : '保存失败' }
  finally { if (!disposed && seq === generation) saving.value = false }
}
async function adopt() {
  if (!world.value || saving.value || props.busy) return
  const payload = { revision_id: world.value.revision_id }
  const seq = generation, pid = props.projectId, bid = props.branchId
  saving.value = true; error.value = ''
  try {
    await api.adoptWorldDefinitions(pid, bid, { ...payload, request_id: idempotency(payload) })
    if (disposed || seq !== generation) return
    requestKey = ''; await load()
  } catch (err) { if (!disposed && seq === generation) error.value = err instanceof Error ? err.message : '更新规则失败' }
  finally { if (!disposed && seq === generation) saving.value = false }
}
async function previewMigration() {
  const seq = sequence
  try {
    const result = await api.previewWorldMigration(props.projectId)
    if (!disposed && seq === sequence) { preview.value = result; migrationSelection.value = {} }
  } catch (err) { if (!disposed && seq === sequence) error.value = err instanceof Error ? err.message : '预览失败' }
}
async function migrate() {
  if (!preview.value || saving.value || props.busy) return
  const branches: Record<string, WorldOperation[]> = {}
  for (const branch of preview.value.branches) {
    branches[branch.branch_id] = branch.entries.flatMap((entry, i) =>
      migrationSelection.value[`${branch.branch_id}:${i}`] && entry.suggestion ? [entry.suggestion] : [])
  }
  const payload = { preview_revision: preview.value.preview_revision, branches }
  const pid = props.projectId, bid = props.branchId, seq = generation
  saving.value = true; error.value = ''
  try {
    await api.migrateWorld(pid, { ...payload, request_id: idempotency(payload) })
    emit('migrated')
    if (disposed || seq !== generation) return
    preview.value = null; requestKey = ''; await load()
  } catch (err) { if (!disposed && seq === generation) error.value = err instanceof Error ? err.message : '迁移失败' }
  finally { if (!disposed && seq === generation) saving.value = false }
}
</script>

<template>
  <section class="world-panel">
    <div class="toolbar"><strong>分支世界</strong><button :disabled="loading || saving" @click="load">刷新</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="loading && !world && !legacy">读取中…</p>
    <template v-if="world">
      <p class="hint">状态会跨场延续。角色按现场感知与此前获知的信息读取。</p>
      <div class="toolbar">
        <button :disabled="busy || saving" @click="edit()">新增事实</button>
        <button :disabled="busy || saving" @click="adopt">采用当前物件规则</button>
      </div>
      <p class="hint">采用规则只影响此分支之后的推演，旧快照保留当时规则。</p>
      <form v-if="editing" class="editor" @submit.prevent="save">
        <label>对象范围<select :disabled="saving || busy" v-model="kind" @change="subject = kind === 'world' ? 'world' : ''; visibility = kind === 'world' ? 'global' : 'observable'">
          <option value="world">世界</option><option value="object">物件</option><option value="location">地点</option>
        </select></label>
        <label v-if="kind === 'object'">物件<select :disabled="saving || busy" v-model="subject" required>
          <option v-for="obj in world.definitions" :key="obj.object_id" :value="obj.object_id">{{ obj.name }}</option>
        </select></label>
        <label v-if="kind === 'location'">地点标识<input :disabled="saving || busy" v-model="subject" required /></label>
        <label>属性标识<input :disabled="saving || busy" v-model="attribute" maxlength="64" required placeholder="如 open、holder、season" /></label>
        <label>显示名称<input :disabled="saving || busy" v-model="label" maxlength="80" placeholder="如 开启、持有者、季节" /></label>
        <label><input :disabled="saving || busy" v-model="deleting" type="checkbox" /> 清除此属性</label>
        <template v-if="!deleting">
          <label>值的类型<select :disabled="saving || busy" v-model="valueType"><option value="text">文本</option><option value="boolean">是 / 否</option><option value="number">数字</option><option value="character">角色</option><option value="object">物件</option><option value="location">地点引用</option></select></label>
          <label>值<select v-if="valueType === 'boolean'" v-model="value"><option value="true">是</option><option value="false">否</option></select>
            <select v-else-if="valueType === 'character'" v-model="value" required><option v-for="c in characters" :key="c.character_id" :value="c.character_id">{{ c.name }}</option></select>
            <select v-else-if="valueType === 'object'" v-model="value" required><option v-for="o in world.definitions" :key="o.object_id" :value="o.object_id">{{ o.name }}</option></select>
            <input v-else v-model="value" :type="valueType === 'number' ? 'number' : 'text'" step="any" />
          </label>
        </template>
        <label>传播范围<select :disabled="saving || busy" v-model="visibility"><option value="global">广泛公开：后续角色均可获知</option><option value="observable">现场可观察</option><option value="hidden">隐藏：不直接提供给角色</option></select></label>
        <fieldset><legend>明确告知角色</legend><label v-for="c in characters" :key="c.character_id"><input :disabled="saving || busy" v-model="observers" type="checkbox" :value="c.character_id" />{{ c.name }}</label></fieldset>
        <div class="toolbar"><button type="submit" :disabled="busy || saving">{{ saving ? '提交中…' : '保存变化' }}</button><button type="button" :disabled="saving" @click="editing = false">取消</button></div>
      </form>
      <p v-if="!facts.length" class="hint">尚无动态世界事实。</p>
      <article v-for="fact in facts" :key="`${fact.subject_kind}:${fact.subject_id}:${fact.attribute}`" class="fact">
        <div><strong>{{ name(fact) }} · {{ fact.label || fact.attribute }}</strong><span class="scope">{{ visibilityLabels[fact.visibility] }}</span></div>
        <p>{{ showValue(fact.value) }}</p><button :disabled="busy || saving" @click="edit(fact)">修改</button>
      </article>
      <details v-if="Object.keys(world.removed_facts || {}).length"><summary>已清除的属性（{{ Object.keys(world.removed_facts || {}).length }}）</summary><p v-for="fact in world.removed_facts" :key="`${fact.subject_id}:${fact.attribute}`">{{ name(fact) }} · {{ fact.label || fact.attribute }}：已清除</p></details>
      <details v-if="Object.keys(world.legacy_records).length"><summary>保留的旧记录（{{ Object.keys(world.legacy_records).length }}）</summary><p class="hint">未归类的记录不向角色广播，可确认含义后新增对应事实。</p><p v-for="(v, k) in world.legacy_records" :key="k">{{ k }}：{{ showValue(v) }}</p></details>
      <details><summary>变化记录（{{ history.length }}）</summary><article v-for="event in history" :key="event.event_id" class="fact"><strong>{{ event.source === 'environment' ? '环境裁决' : event.source === 'director' ? '导演场间变化' : event.source === 'definitions' ? '规则更新' : event.source === 'observation' ? '角色现场观察' : '用户修改' }}</strong><p>{{ event.narration || event.operations.map(o => `${o.label || o.attribute}：${o.op === 'unset' ? '清除' : showValue(o.value)}`).join('；') }}</p></article></details>
    </template>
    <template v-if="legacy">
      <p class="hint">此项目使用旧世界变量。先预览所有分支，再选择明确的物件属性迁入；其余记录完整保留，切换后可逐项归类。</p>
      <p v-for="(v, k) in legacy.variables" :key="k">{{ k }}：{{ v }}</p>
      <button :disabled="busy || saving" @click="previewMigration">预览项目迁移</button>
      <div v-if="preview">
        <article v-for="branch in preview.branches" :key="branch.branch_id" class="fact">
          <strong>{{ branch.name }}</strong>
          <label v-for="(entry, i) in branch.entries" :key="entry.key" class="migration-entry">
            <input v-if="entry.suggestion" v-model="migrationSelection[`${branch.branch_id}:${i}`]" type="checkbox" />
            <span>{{ entry.key }}：{{ showValue(entry.value) }}<small>{{ categories[entry.category] }}</small></span>
          </label>
        </article>
        <p class="hint">只有勾选的物件属性进入新状态。此操作切换整个项目；旧记录与快照保留。旧场景历史需从快照分叉后继续。</p>
        <button :disabled="busy || saving" @click="migrate">{{ saving ? '迁移中…' : '按上述选择切换项目' }}</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.world-panel { display: grid; gap: .8rem; font-size: .9rem; }
.toolbar { display: flex; justify-content: space-between; gap: .5rem; flex-wrap: wrap; align-items: center; }
button { border: 1px solid var(--line); border-radius: 6px; padding: .35rem .6rem; background: var(--surface); color: inherit; cursor: pointer; }
button:disabled { opacity: .45; cursor: default; }
.hint, .scope, small { color: var(--muted); font-size: .8rem; }
.scope { display: inline-block; margin-left: .6rem; }
.error { color: var(--danger, #b84141); }
.fact { border-bottom: 1px solid var(--line); padding-bottom: .75rem; overflow-wrap: anywhere; }
.fact p { margin: .35rem 0; }
.editor { display: grid; gap: .6rem; padding: .75rem; border: 1px solid var(--line); border-radius: 8px; }
.editor label { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; }
input:not([type=checkbox]), select { min-width: 0; width: 100%; padding: .4rem; border: 1px solid var(--line); border-radius: 4px; color: inherit; background: var(--surface); }
fieldset { display: flex; gap: .5rem; flex-wrap: wrap; border: 1px solid var(--line); }
.migration-entry { display: flex; gap: .4rem; margin-top: .5rem; }
.migration-entry small { display: block; }
details summary { cursor: pointer; }
</style>
