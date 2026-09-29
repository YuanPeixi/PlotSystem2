// Run the real Director.vue script with mocked stores/router, without a browser.
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/pages/Director.vue', import.meta.url), 'utf8')
const script = source
  .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  .replace(/^import .*$/gm, '')
const compiled = ts.transpileModule(script + `
globalThis.subject = {
  plan, intent, draft, planning, composeError, creating, composing, branchId,
  startCompose, cancelCompose, startScene,
};
`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

/**
 * planImpl / createImpl 缺省立即返回；传入函数可换成手动控制的 deferred，
 * 用来构造"响应迟到"的时序。
 */
function harness({ goal = '', answers = [], planImpl, createImpl } = {}) {
  const props = { projectId: 'p1' }
  const project = { current: { project_id: 'p1', narrative_goal: goal } }
  const plans = [], confirms = [], creates = [], started = []
  const context = {
    console,
    // mock 抛出的错误来自外层 realm，不共用 Error 的话 instanceof 恒为假
    Error,
    defineProps: () => props,
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn() } }),
    watch: () => {},
    nextTick: async () => {},
    onMounted: () => {},
    onBeforeUnmount: () => {},
    useRoute: () => ({ query: {} }),
    useRouter: () => ({ replace: () => {}, push: () => {} }),
    useCharacterStore: () => ({ characters: [] }),
    useProjectStore: () => project,
    useDirectorStore: () => ({ snapshots: [], branchTree: { roots: [] }, loadSnapshots: async () => {} }),
    useSceneStore: () => ({
      plan: async (pid, branch, intent) => {
        plans.push({ pid, branch, intent })
        return planImpl ? planImpl(plans.length) : { name: '规划' }
      },
      createScene: async (pid, payload) => {
        creates.push(payload)
        return createImpl ? createImpl(creates.length) : { scene_id: `s${creates.length}`, branch_id: payload.branch_id }
      },
      startNewScene: async (scene) => { started.push(scene.scene_id); return true },
    }),
    api: { listScenes: async () => [] },
    confirm: (msg) => { confirms.push(msg); return answers.shift() ?? true },
    alert: () => {},
  }
  runInNewContext(compiled, context)
  return { ...context.subject, props, project, plans, confirms, creates, started }
}

const DRAFT = () => ({
  name: '雨夜',
  description: '',
  participating_characters: ['c1'],
  location: '酒馆',
  initial_conditions: {},
  max_turns: 6,
  opening_narration: '',
  speaker_mode: 'round_robin',
})

test('planning without a narrative goal asks first, and declining does not call the director', async () => {
  const h = harness({ answers: [false] })
  await h.plan()
  assert.equal(h.confirms.length, 1)
  assert.match(h.confirms[0], /主线目标/)
  assert.equal(h.plans.length, 0)
})

test('once confirmed for a project, later plans do not ask again', async () => {
  const h = harness({ answers: [true] })
  h.intent.value = '雨夜对峙'
  await h.plan()
  await h.plan()
  assert.equal(h.confirms.length, 1)
  assert.equal(h.plans.length, 2)
  assert.equal(h.plans[0].intent, '雨夜对峙')
})

test('a declined prompt is asked again next time', async () => {
  const h = harness({ answers: [false, true] })
  await h.plan()
  await h.plan()
  assert.equal(h.confirms.length, 2)
  assert.equal(h.plans.length, 1)
})

test('switching to another project without a goal asks again', async () => {
  const h = harness({ answers: [true, true] })
  await h.plan()
  h.props.projectId = 'p2'
  h.project.current = { project_id: 'p2', narrative_goal: '' }
  await h.plan()
  assert.equal(h.confirms.length, 2)
})

test('a project with a narrative goal plans without asking', async () => {
  const h = harness({ goal: '扳倒丞相' })
  await h.plan()
  assert.equal(h.confirms.length, 0)
  assert.equal(h.plans.length, 1)
})

test('a whitespace-only goal counts as missing', async () => {
  const h = harness({ goal: '   ', answers: [false] })
  await h.plan()
  assert.equal(h.confirms.length, 1)
})

// ---- 迟到响应与在途请求 ----

test('a plan response arriving after cancel does not reopen the composer', async () => {
  const d = deferred()
  const h = harness({ goal: 'g', planImpl: () => d.promise })
  h.startCompose()
  const p = h.plan()
  h.cancelCompose()
  d.resolve(DRAFT())
  await p
  assert.equal(h.draft.value, null)
  assert.equal(h.composing.value, false)
})

test('a plan requested on branch A is dropped if the user switched to branch B', async () => {
  const d = deferred()
  const h = harness({ goal: 'g', planImpl: () => d.promise })
  h.branchId.value = 'A'
  h.startCompose()
  const p = h.plan()
  h.branchId.value = 'B'
  d.resolve(DRAFT())
  await p
  assert.equal(h.draft.value, null)
})

test('only the latest replan wins, and the stale one does not clear "planning"', async () => {
  const first = deferred(), second = deferred()
  const h = harness({ goal: 'g', planImpl: n => (n === 1 ? first.promise : second.promise) })
  h.startCompose()
  const p1 = h.plan()
  const p2 = h.plan()
  first.resolve({ ...DRAFT(), name: '旧' })
  await p1
  assert.equal(h.draft.value, null)
  assert.equal(h.planning.value, true)
  second.resolve({ ...DRAFT(), name: '新' })
  await p2
  assert.equal(h.draft.value.name, '新')
  assert.equal(h.planning.value, false)
})

test('a failed plan surfaces the error and resets planning', async () => {
  const h = harness({ goal: 'g', planImpl: () => Promise.reject(new Error('导演超时')) })
  h.startCompose()
  await h.plan()
  assert.equal(h.composeError.value, '导演超时')
  assert.equal(h.planning.value, false)
})

test('double-clicking start while the create request is in flight creates one scene', async () => {
  const d = deferred()
  const h = harness({ createImpl: () => d.promise })
  h.branchId.value = 'A'
  h.startCompose()
  h.draft.value = DRAFT()
  const p1 = h.startScene()
  const p2 = h.startScene()
  assert.equal(h.creating.value, true)
  d.resolve({ scene_id: 's1', branch_id: 'A' })
  await Promise.all([p1, p2])
  assert.equal(h.creates.length, 1)
  assert.deepEqual(h.started, ['s1'])
  assert.equal(h.creating.value, false)
  assert.equal(h.draft.value, null)
})

test('a failed create keeps the draft and shows why', async () => {
  const h = harness({ createImpl: () => Promise.reject(new Error('分支不存在')) })
  h.branchId.value = 'A'
  h.startCompose()
  h.draft.value = DRAFT()
  await h.startScene()
  assert.equal(h.composeError.value, '分支不存在')
  assert.equal(h.draft.value.name, '雨夜')
  assert.equal(h.composing.value, true)
  assert.deepEqual(h.started, [])
})

test('a scene created after the user left the composer is not started', async () => {
  const d = deferred()
  const h = harness({ createImpl: () => d.promise })
  h.branchId.value = 'A'
  h.startCompose()
  h.draft.value = DRAFT()
  const p = h.startScene()
  h.branchId.value = 'B'
  d.resolve({ scene_id: 's1', branch_id: 'A' })
  await p
  assert.deepEqual(h.started, [])
})

test('the scene is created on the branch it was planned for', async () => {
  const h = harness()
  h.branchId.value = 'A'
  h.startCompose()
  h.draft.value = DRAFT()
  await h.startScene()
  assert.equal(h.creates[0].branch_id, 'A')
})
