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
globalThis.subject = { plan, intent, draft };
`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function harness({ goal = '', answers = [] } = {}) {
  const props = { projectId: 'p1' }
  const project = { current: { project_id: 'p1', narrative_goal: goal } }
  const plans = [], confirms = []
  const context = {
    console,
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
    useDirectorStore: () => ({ snapshots: [], branchTree: { roots: [] } }),
    useSceneStore: () => ({
      plan: async (pid, branch, intent) => { plans.push({ pid, branch, intent }); return { name: '规划' } },
    }),
    api: {},
    confirm: (msg) => { confirms.push(msg); return answers.shift() ?? true },
    alert: () => {},
  }
  runInNewContext(compiled, context)
  return { ...context.subject, props, project, plans, confirms }
}

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
