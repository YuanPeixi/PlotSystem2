<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, ApiError } from '@/api/client'
import Icon from '@/components/ui/Icon.vue'
import type { CharacterCard, WorldObject, WorldObjectFields } from '@/types'

/**
 * 物件编辑器（工单24 PR-1c）：列表、单份草稿的新建 / 编辑、删除。
 *
 * 约束沿用分镜稿面板（StoryboardPanel.vue）踩过的坑：
 * - **草稿有归属**：切项目即清空，保存的响应只认发出它的那份草稿；
 * - **409 不自动覆盖**：保留草稿供对照，重新加载时以最新内容重开编辑；
 * - **保存成功让此前发出的列表刷新作废**：较早的 GET 晚回来会把列表盖回旧修订号，再编辑必 409；
 * - **幂等键两种用法**：修改随内容走（内容没变的重试沿用同一个）；新建整份草稿只用一个 ——
 *   创建的响应丢了、用户改了内容再提交时，换新键会建出第二个同名物件，沿用旧键则被后端 422 拦下。
 *
 * 本地只拦"必然 422"的形状问题（空名、单字别名、与角色同名、private 无知情者），
 * token 预算以后端为准，不在前端估算。
 */
const props = defineProps<{
  projectId: string
  characters: CharacterCard[]
  // 变化时刷新列表（构建完成会抽出新物件）；不动草稿
  refreshKey?: unknown
}>()

type Visibility = WorldObject['visibility']

interface Draft {
  projectId: string
  // 空串 = 新建
  objectId: string
  revision: number
  name: string
  aliases: string[]
  aliasInput: string
  public_description: string
  hidden_rules: string[]
  visibility: Visibility
  known_by: string[]
  requestKey: string
  requestId: string
  // 编辑期间物件被删了：草稿只读，只能另存为新物件
  gone: boolean
}

// 与后端 services/objects.py 的上限一致；超出时后端照样 422，这里只是提前禁用按钮
const MAX_OBJECTS = 40
const MAX_ALIASES = 6
const MAX_RULES = 8
const NAME_CHARS = 24
const ALIAS_MIN_CHARS = 2
const ALIAS_CHARS = 16

const VISIBILITY: Record<Visibility, { label: string; hint: string }> = {
  global: { label: '公开', hint: '在场角色都知道它存在、看得到外观' },
  private: { label: '私有', hint: '只有选中的知情者知道它存在' },
  hidden: { label: '隐藏', hint: '没有角色知道它存在' },
}

const objects = ref<WorldObject[]>([])
const loading = ref(false)
const loadError = ref('')
// 列表是否完整：当前项目最近一次读取成功才算。首次加载完成前、或加载失败后，
// 列表可能缺项，计数与"满 40 个"的判断都不可信，新建要等它
const listed = ref(false)
const actionError = ref('')
const draft = ref<Draft | null>(null)
const saving = ref(false)
const conflict = ref('')
const saveError = ref('')
const deleting = ref('')
let loadSeq = 0
let saveSeq = 0
// 冲突后的重新加载在途：防连点，各自重开一次草稿
let reloading = false

const characterNames = computed(() => new Set(props.characters.map((c) => c.name)))
const nameOf = computed(() => new Map(props.characters.map((c) => [c.character_id, c.name])))

// 新建草稿、或编辑中被删掉的物件不在列表里，放在最前面
const rows = computed(() => {
  const list = objects.value.map((o) => ({ key: o.object_id, obj: o as WorldObject | null }))
  const d = draft.value
  if (d && !list.some((r) => r.key === d.objectId)) list.unshift({ key: d.objectId || 'new', obj: null })
  return list
})

const audienceOptions = computed(() => {
  const d = draft.value
  if (!d) return []
  const opts = props.characters.map((c) => ({ id: c.character_id, name: c.name }))
  // 名单里找不到的角色照样显示、照样提交：悄悄删掉等于替用户改了知情范围，交给后端 422
  for (const id of d.known_by) {
    if (!opts.some((o) => o.id === id)) opts.push({ id, name: '（角色已不存在）' })
  }
  return opts
})

const blockers = computed(() => {
  const d = draft.value
  if (!d) return []
  const out: string[] = []
  const name = oneLine(d.name)
  if (!name) out.push('名称不能为空')
  else if (name.length > NAME_CHARS) out.push(`名称超过 ${NAME_CHARS} 字`)
  else if (characterNames.value.has(name)) out.push(`名称「${name}」与角色同名，动作里每提到这个角色都会被当成提到了物件`)
  for (const alias of d.aliases) {
    if (alias === name) continue
    if (alias.length < ALIAS_MIN_CHARS) out.push(`别名「${alias}」只有一个字，几乎每一轮都会被当成提到了这个物件`)
    else if (alias.length > ALIAS_CHARS) out.push(`别名「${alias.slice(0, ALIAS_CHARS)}…」超过 ${ALIAS_CHARS} 字`)
    else if (characterNames.value.has(alias)) out.push(`别名「${alias}」与角色同名`)
  }
  // 放不下的别名留在输入框里：此时保存会把它们丢掉，先拦下
  const pending = oneLine(d.aliasInput)
  if (pending && d.aliases.length >= MAX_ALIASES) {
    out.push(`别名最多 ${MAX_ALIASES} 个，输入框里的「${pending}」未添加：删掉一些别名后回车，或清空输入框`)
  }
  if (d.visibility === 'private' && !d.known_by.length) out.push('私有物件至少要选一位知情者')
  return out
})

function oneLine(s: string): string {
  return s.split(/\s+/).filter(Boolean).join(' ')
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}

function audienceText(o: WorldObject): string {
  return o.known_by.map((id) => nameOf.value.get(id) || '（角色已不存在）').join('、')
}

/** 返回本次读取是否成功且仍是最新一次；被作废或失败时列表未更新，调用方不能据此重开草稿。 */
async function load(): Promise<boolean> {
  const pid = props.projectId
  const seq = ++loadSeq
  if (!pid) {
    objects.value = []
    listed.value = false
    return false
  }
  loading.value = true
  loadError.value = ''
  try {
    const list = await api.listObjects(pid)
    if (seq !== loadSeq || pid !== props.projectId) return false
    objects.value = list
    listed.value = true
    return true
  } catch (err) {
    if (seq === loadSeq) {
      loadError.value = errorText(err, '物件加载失败')
      listed.value = false
    }
    return false
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/**
 * 写入成功、本地列表已更新之后调用。
 *
 * 在途的读取可能是在写入之前发出的，晚回来会把列表盖回旧修订号（再编辑必 409），必须作废；
 * 但写入的响应只有这一个物件，不像分镜稿那样是整份 —— 只作废不补，在途读取本该带回的
 * 其他物件（构建刚抽出的、首次加载的全部）就丢了。所以作废的方式是重新发一次：它在写入
 * 之后发出，读到的一定已含本次写入。列表本就不完整时同理。
 */
function refreshAfterWrite() {
  if (loading.value || !listed.value) void load()
}

watch(
  () => props.projectId,
  () => {
    // 草稿属于上一个项目：带着它切过去再保存，会把 A 的物件建进 B
    draft.value = null
    conflict.value = ''
    saveError.value = ''
    actionError.value = ''
    objects.value = []
    listed.value = false
    saveSeq++
    saving.value = false
    deleting.value = ''
    void load()
  },
  { immediate: true },
)

watch(
  () => props.refreshKey,
  () => void load(),
)

function openDraft(o: WorldObject | null) {
  draft.value = {
    projectId: props.projectId,
    objectId: o?.object_id ?? '',
    revision: o?.revision ?? 0,
    name: o?.name ?? '',
    aliases: [...(o?.aliases ?? [])],
    aliasInput: '',
    public_description: o?.public_description ?? '',
    hidden_rules: [...(o?.hidden_rules ?? [])],
    // 新物件默认隐藏：拿不准时收紧，与构建期判定同一口径
    visibility: o?.visibility ?? 'hidden',
    known_by: [...(o?.known_by ?? [])],
    requestKey: '',
    requestId: '',
    gone: false,
  }
  conflict.value = ''
  saveError.value = ''
  actionError.value = ''
}

function startCreate() {
  if (draft.value || !listed.value || objects.value.length >= MAX_OBJECTS) return
  openDraft(null)
}

function startEdit(o: WorldObject) {
  if (draft.value) return
  openDraft(o)
}

function cancelEdit() {
  // 保存在途时不让取消：请求照样会落盘，取消后列表却不知道
  if (saving.value) return
  draft.value = null
  conflict.value = ''
  saveError.value = ''
}

function editable(): Draft | null {
  const d = draft.value
  return d && !saving.value && !d.gone ? d : null
}

function addAlias() {
  const d = editable()
  if (!d) return
  // 一次粘贴多个：按中英文逗号、顿号拆开。超过上限的留在输入框里，由 blockers 提示 ——
  // 清空输入框等于替用户截断，与"超限不截断"同一条原则
  const overflow: string[] = []
  for (const part of d.aliasInput.split(/[,，、]/)) {
    const alias = oneLine(part)
    if (!alias || d.aliases.includes(alias) || overflow.includes(alias)) continue
    if (d.aliases.length >= MAX_ALIASES) overflow.push(alias)
    else d.aliases.push(alias)
  }
  d.aliasInput = overflow.join('、')
}

function removeAlias(index: number) {
  editable()?.aliases.splice(index, 1)
}

function addRule() {
  const d = editable()
  if (d && d.hidden_rules.length < MAX_RULES) d.hidden_rules.push('')
}

function removeRule(index: number) {
  editable()?.hidden_rules.splice(index, 1)
}

function moveRule(index: number, delta: number) {
  const rules = editable()?.hidden_rules
  const target = index + delta
  if (!rules || target < 0 || target >= rules.length) return
  const [rule] = rules.splice(index, 1)
  rules.splice(target, 0, rule)
}

function setVisibility(v: Visibility) {
  const d = editable()
  // known_by 留在草稿里：切走再切回 private 不必重选；提交时非 private 一律发空名单
  if (d) d.visibility = v
}

function toggleAudience(id: string) {
  const d = editable()
  if (!d) return
  const i = d.known_by.indexOf(id)
  if (i >= 0) d.known_by.splice(i, 1)
  else d.known_by.push(id)
}

function newRequestId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
  )
}

function toFields(d: Draft): WorldObjectFields {
  return {
    name: oneLine(d.name),
    aliases: [...d.aliases],
    public_description: d.public_description.trim(),
    hidden_rules: d.hidden_rules.map(oneLine).filter(Boolean),
    visibility: d.visibility,
    known_by: d.visibility === 'private' ? [...d.known_by] : [],
  }
}

function upsert(saved: WorldObject) {
  const i = objects.value.findIndex((o) => o.object_id === saved.object_id)
  if (i >= 0) objects.value.splice(i, 1, saved)
  else objects.value.push(saved)
}

async function save() {
  const d = draft.value
  if (!d || saving.value || d.gone || blockers.value.length) return
  if (d.projectId !== props.projectId) {
    // 切项目时已清空；这里是最后一道：宁可丢草稿也不写错项目
    draft.value = null
    return
  }
  const fields = toFields(d)
  const creating = !d.objectId
  if (creating) {
    if (!d.requestId) d.requestId = newRequestId()
  } else {
    const key = JSON.stringify(fields)
    if (key !== d.requestKey) {
      d.requestKey = key
      d.requestId = newRequestId()
    }
  }
  const token = ++saveSeq
  saving.value = true
  conflict.value = ''
  saveError.value = ''
  try {
    const saved = creating
      ? await api.createObject(d.projectId, { ...fields, request_id: d.requestId })
      : await api.updateObject(d.projectId, d.objectId, {
          ...fields,
          revision: d.revision,
          request_id: d.requestId,
        })
    if (draft.value !== d) return
    upsert(saved)
    draft.value = null
    refreshAfterWrite()
  } catch (err) {
    if (draft.value !== d) return
    const status = err instanceof ApiError ? err.status : 0
    if (status === 409) {
      conflict.value = '物件已被他人修改。草稿已保留供对照；重新加载会以最新内容重开编辑，这里未保存的修改将丢弃。'
    } else if (status === 404 && !creating) {
      d.gone = true
      saveError.value = '这个物件已被删除。草稿已保留，可以另存为新物件。'
      void load()
    } else if (status === 422 && creating && errorText(err, '').includes('幂等键')) {
      // 上一次提交其实已经建好了，只是没收到响应；之后草稿又被改过
      saveError.value = '这份草稿上次提交时已经建好了（当时没收到响应），列表已刷新。要继续修改，请取消草稿后编辑列表里那一项。'
      void load()
    } else {
      saveError.value = errorText(err, '保存失败')
    }
  } finally {
    if (token === saveSeq) saving.value = false
  }
}

function saveAsNew() {
  const d = draft.value
  if (!d || !d.gone || saving.value) return
  d.objectId = ''
  d.revision = 0
  d.gone = false
  d.requestKey = ''
  d.requestId = newRequestId()
  saveError.value = ''
  void save()
}

async function reloadAfterConflict() {
  const d = draft.value
  if (!d || saving.value || reloading) return
  reloading = true
  let ok: boolean
  try {
    ok = await load()
  } finally {
    reloading = false
  }
  if (draft.value !== d) return
  if (!ok) {
    // 列表还是旧的：用它重开草稿只会带着旧修订号再撞一次 409。保留草稿与冲突状态，可直接重试
    conflict.value = '物件已被他人修改，但重新加载失败。草稿已保留，请稍后再点重新加载。'
    return
  }
  draft.value = null
  conflict.value = ''
  saveError.value = ''
  const latest = objects.value.find((o) => o.object_id === d.objectId)
  if (latest) openDraft(latest)
  else actionError.value = '这个物件已被删除。'
}

async function remove(o: WorldObject) {
  if (deleting.value || draft.value?.objectId === o.object_id) return
  if (!confirm(`删除物件「${o.name}」？无法恢复。已经建好的场景若勾选了它，运行时会跳过它。`)) return
  const pid = props.projectId
  deleting.value = o.object_id
  actionError.value = ''
  try {
    await api.deleteObject(pid, o.object_id)
    if (pid !== props.projectId) return
    objects.value = objects.value.filter((x) => x.object_id !== o.object_id)
    refreshAfterWrite()
  } catch (err) {
    if (pid === props.projectId) actionError.value = errorText(err, '删除失败')
  } finally {
    if (deleting.value === o.object_id) deleting.value = ''
  }
}
</script>

<template>
  <div class="objects">
    <div class="section-title">
      <span>物件 <span class="num">{{ listed ? `${objects.length} / ${MAX_OBJECTS}` : '列表未加载' }}</span></span>
      <div class="head-actions">
        <button class="icon sm" title="重新加载" :disabled="loading" @click="load"><Icon name="refresh" :size="14" /></button>
        <button
          class="ghost small-btn"
          :disabled="!!draft || !listed || objects.length >= MAX_OBJECTS"
          :title="
            !listed
              ? '物件列表加载完成后才能新建'
              : objects.length >= MAX_OBJECTS
                ? `每个项目最多 ${MAX_OBJECTS} 个物件`
                : '新建物件'
          "
          @click="startCreate"
        >
          <Icon name="plus" :size="14" />新建
        </button>
      </div>
    </div>
    <p class="dim hint">
      角色可以对物件做动作。隐藏规则只给导演与环境层看，不进任何角色的上下文；可见性与公开描述要等环境裁决上线后才进入角色视野，目前只用于记录动作。
    </p>

    <p v-if="loadError" class="notice danger"><Icon name="alert" :size="15" />{{ loadError }}</p>
    <p v-if="actionError" class="notice danger"><Icon name="alert" :size="15" />{{ actionError }}</p>
    <p v-if="loading && !objects.length && !draft" class="dim small">加载中</p>
    <p v-else-if="!objects.length && !draft && !loadError" class="dim small">
      还没有物件。构建时会从种子文本里抽取，也可以手动新建。
    </p>

    <ul v-if="rows.length" class="object-list">
      <li v-for="row in rows" :key="row.key">
        <!-- 编辑视图 -->
        <div v-if="draft && (row.obj === null || row.obj.object_id === draft.objectId)" class="editor">
          <fieldset :disabled="saving || draft.gone" class="edit-fields">
            <div class="field">
              <label>名称</label>
              <input v-model="draft.name" placeholder="例如：玻璃王冠" />
            </div>
            <div class="field">
              <label>别名<span class="dim label-hint">动作里提到名称或别名才会被识别，最多 {{ MAX_ALIASES }} 个</span></label>
              <div class="chips">
                <span v-for="(a, i) in draft.aliases" :key="a" class="tag">
                  {{ a }}
                  <button type="button" class="icon xs" :title="`移除别名「${a}」`" @click="removeAlias(i)"><Icon name="close" :size="12" /></button>
                </span>
                <!-- 满了也不禁用：放不下的别名留在这里，用户得能清掉它 -->
                <input
                  v-model="draft.aliasInput"
                  class="alias-input"
                  :placeholder="draft.aliases.length >= MAX_ALIASES ? `已满 ${MAX_ALIASES} 个` : '输入后回车，多个用顿号分隔'"
                  @keydown.enter.prevent="addAlias"
                  @blur="addAlias"
                />
              </div>
            </div>
            <div class="field">
              <label>公开描述<span class="dim label-hint">在场的人肉眼可见的部分</span></label>
              <textarea v-model="draft.public_description" rows="2"></textarea>
            </div>
            <div class="field">
              <label class="private-label"><Icon name="private" :size="13" />隐藏规则<span class="dim label-hint">触发条件、机关、真实功能，一行一条，最多 {{ MAX_RULES }} 条</span></label>
              <div v-for="(_, i) in draft.hidden_rules" :key="i" class="rule-row">
                <input v-model="draft.hidden_rules[i]" placeholder="例如：只有王室血脉戴上才会投出记忆" />
                <button type="button" class="icon sm" title="上移" :disabled="i === 0" @click="moveRule(i, -1)"><Icon name="arrow-up" :size="14" /></button>
                <button type="button" class="icon sm" title="下移" :disabled="i === draft.hidden_rules.length - 1" @click="moveRule(i, 1)"><Icon name="arrow-down" :size="14" /></button>
                <button type="button" class="icon sm danger" title="删除这条规则" @click="removeRule(i)"><Icon name="close" :size="14" /></button>
              </div>
              <button type="button" class="ghost add" :disabled="draft.hidden_rules.length >= MAX_RULES" @click="addRule">
                <Icon name="plus" :size="14" />新增规则
              </button>
            </div>
            <div class="field">
              <label>可见性</label>
              <div class="seg" role="group" aria-label="可见性">
                <button
                  v-for="(v, key) in VISIBILITY"
                  :key="key"
                  type="button"
                  :aria-pressed="draft.visibility === key"
                  :title="v.hint"
                  @click="setVisibility(key)"
                >
                  {{ v.label }}
                </button>
              </div>
              <p class="dim small">{{ VISIBILITY[draft.visibility].hint }}</p>
            </div>
            <div v-if="draft.visibility === 'private'" class="field">
              <label>知情者</label>
              <div class="chips">
                <button
                  v-for="c in audienceOptions"
                  :key="c.id"
                  type="button"
                  class="chip"
                  :aria-pressed="draft.known_by.includes(c.id)"
                  @click="toggleAudience(c.id)"
                >
                  {{ c.name }}
                </button>
                <span v-if="!audienceOptions.length" class="dim small">项目里还没有角色</span>
              </div>
            </div>
          </fieldset>

          <ul v-if="blockers.length" class="blockers">
            <li v-for="b in blockers" :key="b" class="notice warn"><Icon name="alert" :size="15" />{{ b }}</li>
          </ul>
          <p v-if="conflict" class="notice danger">
            <Icon name="alert" :size="15" /><span>{{ conflict }} <button class="ghost inline" @click="reloadAfterConflict">重新加载</button></span>
          </p>
          <p v-if="saveError" class="notice danger"><Icon name="alert" :size="15" />{{ saveError }}</p>
          <div class="edit-actions">
            <button class="ghost" :disabled="saving" @click="cancelEdit">取消</button>
            <button v-if="draft.gone" class="primary" :disabled="saving || blockers.length > 0" @click="saveAsNew">另存为新物件</button>
            <button v-else class="primary" :disabled="saving || blockers.length > 0" @click="save">
              {{ saving ? '保存中' : draft.objectId ? '保存' : '新建' }}
            </button>
          </div>
        </div>

        <!-- 只读视图 -->
        <article v-else-if="row.obj" class="object">
          <header class="object-head">
            <span class="object-name">{{ row.obj.name }}</span>
            <span class="tag" :title="VISIBILITY[row.obj.visibility]?.hint">
              {{ VISIBILITY[row.obj.visibility]?.label ?? row.obj.visibility }}<template
                v-if="row.obj.visibility === 'private'"
              > · {{ audienceText(row.obj) }}</template>
            </span>
            <span class="spacer"></span>
            <button class="icon sm" title="编辑" :disabled="!!draft" @click="startEdit(row.obj)"><Icon name="edit" :size="14" /></button>
            <button
              class="icon sm danger"
              title="删除"
              :disabled="deleting === row.obj.object_id"
              @click="remove(row.obj)"
            >
              <Icon :name="deleting === row.obj.object_id ? 'spinner' : 'trash'" :size="14" />
            </button>
          </header>
          <p v-if="row.obj.aliases.length" class="dim small">别名：{{ row.obj.aliases.join('、') }}</p>
          <p class="desc">{{ row.obj.public_description || '（无公开描述）' }}</p>
          <details v-if="row.obj.hidden_rules.length" class="rules">
            <summary><Icon name="chevron-right" :size="14" class="caret" />隐藏规则 {{ row.obj.hidden_rules.length }} 条（仅导演可见）</summary>
            <ol>
              <li v-for="(r, i) in row.obj.hidden_rules" :key="i">{{ r }}</li>
            </ol>
          </details>
          <p v-else class="dim small">无隐藏规则</p>
        </article>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.head-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}
.small-btn {
  height: 26px;
  padding: 0 8px;
  font-weight: 400;
}
.hint {
  font-size: 12px;
  line-height: 1.6;
  margin-bottom: 10px;
}
.small {
  font-size: 12.5px;
}
.notice {
  margin-bottom: 8px;
}
.object-list {
  list-style: none;
  display: flex;
  flex-direction: column;
}
.object-list > li {
  padding: 12px 0;
  border-top: 1px solid var(--line);
}
.object-list > li:first-child {
  border-top: 0;
  padding-top: 4px;
}
.object {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.object-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.object-name {
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.desc {
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--ink-2);
  white-space: pre-wrap;
}
.rules {
  font-size: 12.5px;
}
.rules summary {
  list-style: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: var(--private);
}
.rules summary::-webkit-details-marker {
  display: none;
}
.caret {
  transition: transform 0.15s;
}
.rules[open] .caret {
  transform: rotate(90deg);
}
.rules ol {
  margin: 6px 0 0;
  padding: 8px 10px 8px 28px;
  border-radius: var(--r-sm);
  background: var(--private-soft);
  color: var(--private);
  line-height: 1.7;
}
.editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.edit-fields {
  border: none;
  margin: 0;
  padding: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.label-hint {
  font-weight: 400;
  margin-left: 8px;
}
.private-label {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--private);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.alias-input {
  flex: 1;
  min-width: 180px;
}
button.icon.xs {
  width: 16px;
  height: 16px;
  padding: 0;
  margin-right: -4px;
}
.chip {
  height: 26px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--r-xs);
  background: var(--hover);
  color: var(--ink-2);
  font-size: 13px;
}
.chip[aria-pressed='true'] {
  background: var(--ink);
  color: var(--panel);
}
.rule-row {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 6px;
}
.rule-row input {
  flex: 1;
}
.add {
  align-self: flex-start;
}
.blockers {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.blockers .notice {
  margin: 0;
}
.inline {
  height: 22px;
  padding: 0 6px;
  color: inherit;
  text-decoration: underline;
}
.edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
