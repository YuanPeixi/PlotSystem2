// Run the real ObjectsPanel.vue script with mocked Vue reactivity and HTTP, without a browser.
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/components/ObjectsPanel.vue', import.meta.url), 'utf8')
const script = source
  .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  .replace(/^import .*$/gm, '')
const compiled = ts.transpileModule(script + `
globalThis.subject = { objects, listed, draft, saving, conflict, saveError, loadError, actionError, blockers, rows,
  load, startCreate, startEdit, cancelEdit, addAlias, removeAlias, addRule, removeRule, moveRule,
  setVisibility, toggleAudience, save, saveAsNew, reloadAfterConflict, remove };
`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

const CHARACTERS = [
  { character_id: 'c1', name: '伊莎贝尔' },
  { character_id: 'c2', name: '诺安' },
]

function obj(id, overrides = {}) {
  return {
    object_id: id, project_id: 'p', name: `物件${id}`, aliases: ['水晶冠'], public_description: '剔透',
    hidden_rules: ['王室血脉戴上才会投影'], visibility: 'global', known_by: [], revision: 2,
    ...overrides,
  }
}

/** watch 的最小替身：记录 getter，flush() 时比较新旧值并触发回调。 */
function harness(project = 'p', { confirmed = true } = {}) {
  const props = { projectId: project, characters: CHARACTERS, refreshKey: 0 }
  const lists = [], creates = [], updates = [], deletes = [], watchers = []
  const context = {
    console,
    defineProps: () => props,
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn() } }),
    watch: (getter, callback, opts) => {
      watchers.push({ getter, callback, last: getter() })
      if (opts?.immediate) callback()
    },
    confirm: () => confirmed,
    api: {
      listObjects: p => new Promise((resolve, reject) => lists.push({ project: p, resolve, reject })),
      createObject: (p, payload) => new Promise((resolve, reject) => creates.push({ project: p, payload, resolve, reject })),
      updateObject: (p, oid, payload) =>
        new Promise((resolve, reject) => updates.push({ project: p, oid, payload, resolve, reject })),
      deleteObject: (p, oid) => new Promise((resolve, reject) => deletes.push({ project: p, oid, resolve, reject })),
    },
  }
  // ApiError 必须建在沙箱自己的 realm 里：组件用 instanceof 判错误，跨 realm 的类不认
  runInNewContext(
    'globalThis.ApiError = class ApiError extends Error { constructor(m, s) { super(m); this.status = s } }',
    context,
  )
  runInNewContext(compiled, context)
  const flush = () => {
    for (const w of watchers) {
      const now = w.getter()
      if (now !== w.last) { w.last = now; w.callback() }
    }
  }
  return { ...context.subject, props, lists, creates, updates, deletes, flush, ApiError: context.ApiError }
}

const tick = () => new Promise(r => setTimeout(r, 0))

async function loaded(list = [obj('o1')], project = 'p') {
  const h = harness(project)
  h.lists[0].resolve(list)
  await tick()
  return h
}

test('switching project drops the draft and never saves it into the new project', async () => {
  const h = await loaded()
  h.startEdit(h.objects.value[0])
  h.draft.value.name = 'A 项目的改动'
  h.props.projectId = 'q'
  h.flush()
  assert.equal(h.draft.value, null)
  assert.equal(h.objects.value.length, 0)
  await h.save()
  assert.equal(h.updates.length, 0)
  assert.equal(h.lists.at(-1).project, 'q')
})

test('a stale list response arriving after a save does not roll the object back', async () => {
  const h = await loaded()
  h.load()
  const stale = h.lists.at(-1)
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '新的描述'
  const saving = h.save()
  h.updates[0].resolve(obj('o1', { public_description: '新的描述', revision: 3 }))
  await saving
  stale.resolve([obj('o1')])
  await tick()
  assert.equal(h.objects.value[0].revision, 3)
  assert.equal(h.objects.value[0].public_description, '新的描述')
})

test('409 keeps the draft, does not retry, and reload reopens on the latest revision', async () => {
  const h = await loaded()
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '我的改动'
  const saving = h.save()
  h.updates[0].reject(new h.ApiError('物件已被修改', 409))
  await saving
  assert.ok(h.conflict.value)
  assert.equal(h.draft.value.public_description, '我的改动')
  assert.equal(h.updates.length, 1)

  const reloading = h.reloadAfterConflict()
  h.lists.at(-1).resolve([obj('o1', { revision: 5, public_description: '别人的改动' })])
  await reloading
  assert.equal(h.draft.value.revision, 5)
  assert.equal(h.draft.value.public_description, '别人的改动')
  assert.equal(h.conflict.value, '')
})

test('a failed reload after 409 keeps the conflicting draft and stays retryable', async () => {
  const h = await loaded([obj('o1', { revision: 2 })])
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '我的改动'
  const saving = h.save()
  h.updates[0].reject(new h.ApiError('物件已被修改', 409))
  await saving

  let reloading = h.reloadAfterConflict()
  h.lists.at(-1).reject(new Error('网络错误'))
  await reloading
  // 列表还是旧的：不得拿修订号 2 重开草稿，冲突状态保留以便重试
  assert.equal(h.draft.value.public_description, '我的改动')
  assert.equal(h.draft.value.revision, 2)
  assert.match(h.conflict.value, /重新加载失败/)

  reloading = h.reloadAfterConflict()
  h.lists.at(-1).resolve([obj('o1', { revision: 5, public_description: '别人的改动' })])
  await reloading
  assert.equal(h.draft.value.revision, 5)
  assert.equal(h.conflict.value, '')
})

test('aliases beyond the limit stay in the input and block saving instead of vanishing', async () => {
  const h = await loaded([obj('o1', { aliases: ['甲甲', '乙乙', '丙丙', '丁丁', '戊戊'] })])
  h.startEdit(h.objects.value[0])
  h.draft.value.aliasInput = '己己、庚庚、辛辛'
  h.addAlias()
  assert.equal(h.draft.value.aliases.length, 6)
  assert.equal(h.draft.value.aliasInput, '庚庚、辛辛')
  assert.ok(h.blockers.value.some(b => b.includes('未添加')))
  await h.save()
  assert.equal(h.updates.length, 0)

  // 删掉一个再回车，剩下的照常加进去
  h.removeAlias(0)
  h.addAlias()
  assert.equal(h.draft.value.aliasInput, '辛辛')
  h.draft.value.aliasInput = ''
  assert.equal(h.blockers.value.length, 0)
})

test('update: an unchanged retry reuses the idempotency key, changed content gets a new one', async () => {
  const h = await loaded()
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '第一版'
  let saving = h.save()
  h.updates[0].reject(new Error('网络错误'))
  await saving
  saving = h.save()
  h.updates[1].reject(new Error('网络错误'))
  await saving
  assert.equal(h.updates[1].payload.request_id, h.updates[0].payload.request_id)

  h.draft.value.public_description = '第二版'
  saving = h.save()
  h.updates[2].reject(new Error('网络错误'))
  await saving
  assert.notEqual(h.updates[2].payload.request_id, h.updates[0].payload.request_id)
})

test('create: one draft keeps one idempotency key even after its content changes', async () => {
  const h = await loaded([])
  h.startCreate()
  h.draft.value.name = '玻璃王冠'
  let saving = h.save()
  h.creates[0].reject(new Error('网络错误'))
  await saving
  h.draft.value.public_description = '改了内容再提交'
  saving = h.save()
  assert.equal(h.creates[1].payload.request_id, h.creates[0].payload.request_id)
  h.creates[1].resolve(obj('o9', { name: '玻璃王冠', revision: 0 }))
  await saving
  assert.equal(h.draft.value, null)
  assert.equal(h.objects.value.at(-1).object_id, 'o9')
})

test('create replay conflict (422 on the idempotency key) refreshes the list instead of retrying', async () => {
  const h = await loaded([])
  h.startCreate()
  h.draft.value.name = '玻璃王冠'
  const saving = h.save()
  h.creates[0].reject(new h.ApiError("幂等键 'x' 已用于另一份内容，请换一个", 422))
  await saving
  assert.match(h.saveError.value, /已经建好了/)
  assert.equal(h.lists.length, 2)
  assert.equal(h.creates.length, 1)
})

test('the draft is read-only while saving', async () => {
  const h = await loaded()
  h.startEdit(h.objects.value[0])
  h.draft.value.aliasInput = '冠冕'
  const saving = h.save()
  h.addAlias()
  h.addRule()
  h.removeRule(0)
  h.removeAlias(0)
  h.setVisibility('hidden')
  h.cancelEdit()
  assert.deepEqual([...h.draft.value.aliases], ['水晶冠'])
  assert.equal(h.draft.value.hidden_rules.length, 1)
  assert.equal(h.draft.value.visibility, 'global')
  h.updates[0].resolve(obj('o1', { revision: 3 }))
  await saving
})

test('private without audience is blocked locally; switching away sends an empty audience', async () => {
  const h = await loaded()
  h.startEdit(h.objects.value[0])
  h.setVisibility('private')
  assert.ok(h.blockers.value.some(b => b.includes('知情者')))
  await h.save()
  assert.equal(h.updates.length, 0)

  h.toggleAudience('c1')
  assert.equal(h.blockers.value.length, 0)
  h.setVisibility('global')
  const saving = h.save()
  assert.deepEqual([...h.updates[0].payload.known_by], [])
  h.updates[0].resolve(obj('o1', { revision: 3 }))
  await saving
})

test('one-character aliases and names clashing with characters are blocked locally', async () => {
  const h = await loaded([])
  h.startCreate()
  h.draft.value.name = '诺安'
  assert.ok(h.blockers.value.some(b => b.includes('与角色同名')))
  h.draft.value.name = '玻璃王冠'
  h.draft.value.aliasInput = '冠、王冠'
  h.addAlias()
  assert.deepEqual([...h.draft.value.aliases], ['冠', '王冠'])
  assert.ok(h.blockers.value.some(b => b.includes('只有一个字')))
  await h.save()
  assert.equal(h.creates.length, 0)
})

test('object deleted while editing (404) keeps the draft and can be saved as a new object', async () => {
  const h = await loaded()
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '舍不得丢的描述'
  const saving = h.save()
  h.updates[0].reject(new h.ApiError('物件不存在', 404))
  await saving
  assert.equal(h.draft.value.gone, true)
  assert.equal(h.draft.value.public_description, '舍不得丢的描述')
  h.lists.at(-1).resolve([])
  await tick()

  h.saveAsNew()
  assert.equal(h.creates.length, 1)
  assert.equal(h.creates[0].payload.public_description, '舍不得丢的描述')
  assert.notEqual(h.creates[0].payload.request_id, h.updates[0].payload.request_id)
  h.creates[0].resolve(obj('o2', { public_description: '舍不得丢的描述' }))
  await tick()
  assert.equal(h.draft.value, null)
})

test('creation is disabled at the project limit', async () => {
  const h = await loaded(Array.from({ length: 40 }, (_, i) => obj(`o${i}`)))
  h.startCreate()
  assert.equal(h.draft.value, null)
})

test('delete removes the object even if an older list response arrives later', async () => {
  const h = await loaded([obj('o1'), obj('o2')])
  h.load()
  const stale = h.lists.at(-1)
  const removing = h.remove(h.objects.value[0])
  h.deletes[0].resolve({ deleted: 'o1', existed: true })
  await removing
  stale.resolve([obj('o1'), obj('o2')])
  await tick()
  assert.deepEqual(h.objects.value.map(o => o.object_id), ['o2'])
  // 作废的方式是补发一次：它在删除之后发出
  assert.equal(h.lists.length, 3)
  h.lists.at(-1).resolve([obj('o2')])
  await tick()
  assert.deepEqual(h.objects.value.map(o => o.object_id), ['o2'])
})

test('no follow-up read after a write when the list was complete and idle', async () => {
  const h = await loaded([obj('o1')])
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '改'
  const saving = h.save()
  h.updates[0].resolve(obj('o1', { revision: 3 }))
  await saving
  assert.equal(h.lists.length, 1)
})

test('creating is blocked until the first list load succeeds', async () => {
  const h = harness()
  h.startCreate()
  assert.equal(h.draft.value, null)
  h.lists[0].reject(new Error('网络错误'))
  await tick()
  h.startCreate()
  assert.equal(h.draft.value, null)
  assert.equal(h.listed.value, false)
  h.load()
  h.lists.at(-1).resolve([obj('o1')])
  await tick()
  h.startCreate()
  assert.ok(h.draft.value)
})

test('a save overlapping an in-flight load re-reads instead of trusting only the saved object', async () => {
  const h = await loaded([obj('o1'), obj('o2')])
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '改'
  h.load()
  const inflight = h.lists.at(-1)
  const saving = h.save()
  h.updates[0].resolve(obj('o1', { revision: 3, public_description: '改' }))
  await saving
  inflight.resolve([obj('o1'), obj('o2')])
  await tick()
  assert.equal(h.objects.value[0].revision, 3)
  const followUp = h.lists.at(-1)
  assert.notEqual(followUp, inflight)
  followUp.resolve([obj('o1', { revision: 3 }), obj('o2'), obj('o3')])
  await tick()
  assert.deepEqual(h.objects.value.map(o => o.object_id), ['o1', 'o2', 'o3'])
})

test('a build refresh overlapping a save still brings in the newly extracted objects', async () => {
  const h = await loaded([obj('o1')])
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '改'
  h.props.refreshKey = 1
  h.flush()
  const buildRefresh = h.lists.at(-1)
  const saving = h.save()
  h.updates[0].resolve(obj('o1', { revision: 3 }))
  await saving
  buildRefresh.resolve([obj('o1'), obj('built1')])
  await tick()
  h.lists.at(-1).resolve([obj('o1', { revision: 3 }), obj('built1'), obj('built2')])
  await tick()
  assert.deepEqual(h.objects.value.map(o => o.object_id), ['o1', 'built1', 'built2'])
  assert.equal(h.objects.value[0].revision, 3)
})

test('a build refresh overlapping a delete still brings in the newly extracted objects', async () => {
  const h = await loaded([obj('o1')])
  h.props.refreshKey = 1
  h.flush()
  const buildRefresh = h.lists.at(-1)
  const removing = h.remove(h.objects.value[0])
  h.deletes[0].resolve({ deleted: 'o1', existed: true })
  await removing
  buildRefresh.resolve([obj('o1'), obj('built1')])
  await tick()
  h.lists.at(-1).resolve([obj('built1')])
  await tick()
  assert.deepEqual(h.objects.value.map(o => o.object_id), ['built1'])
})

test('a failed load keeps its error visible and the list marked incomplete', async () => {
  const h = await loaded([obj('o1')])
  h.load()
  h.lists.at(-1).reject(new Error('网络错误'))
  await tick()
  assert.equal(h.listed.value, false)
  assert.ok(h.loadError.value)
  h.startCreate()
  assert.equal(h.draft.value, null)
})

test('refreshKey reloads the list but leaves an open draft alone', async () => {
  const h = await loaded()
  h.startEdit(h.objects.value[0])
  h.draft.value.public_description = '编辑中'
  h.props.refreshKey = 1
  h.flush()
  h.lists.at(-1).resolve([obj('o1'), obj('o2')])
  await tick()
  assert.equal(h.objects.value.length, 2)
  assert.equal(h.draft.value.public_description, '编辑中')
})
