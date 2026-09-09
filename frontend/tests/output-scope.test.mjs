// Run the real Output.vue script with mocked Vue refs and HTTP, without a browser.
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/pages/Output.vue', import.meta.url), 'utf8')
  .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  .replace(/^import .*$/gm, '')
const compiled = ts.transpileModule(source + `
globalThis.subject = { branchId, scopeReady, scopeError, generate, loadScope };
`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const tree = { project_id: 'p', roots: [{ branch: { branch_id: 'if' }, children: [] }] }
function harness(branch = 'if') {
  const requests = [], generated = [], route = { query: { branch } }
  const context = {
    route, console,
    defineProps: () => ({ projectId: 'p' }),
    useRoute: () => route,
    ref: value => ({ value }),
    watch: (_getter, callback) => { context.ready = callback() },
    api: {
      getBranches: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
      generateOutput: async (_project, payload) => { generated.push(payload); return { content: 'ok' } },
    },
  }
  runInNewContext(compiled, context)
  return { ...context.subject, requests, generated, route, ready: context.ready }
}

test('pending branch lookup cannot generate; valid IF scope is retained', async () => {
  const h = harness()
  await h.generate()
  assert.equal(h.generated.length, 0)
  h.requests[0].resolve(tree)
  await h.ready
  await h.generate()
  assert.equal(h.generated[0].branch_id, 'if')
})

test('deleted branch cannot silently export all branches', async () => {
  const h = harness('deleted')
  h.requests[0].resolve(tree)
  await h.ready
  assert.ok(h.scopeError.value)
  await h.generate()
  assert.equal(h.generated.length, 0)
  // The select change explicitly clears the validation message.
  h.branchId.value = 'if'
  h.scopeError.value = ''
  await h.generate()
  assert.equal(h.generated[0].branch_id, 'if')
})

test('failed branch lookup blocks export and supports retry', async () => {
  const h = harness()
  h.requests[0].reject(new Error('offline'))
  await h.ready
  await h.generate()
  assert.equal(h.scopeReady.value, false)
  assert.equal(h.generated.length, 0)
  const retry = h.loadScope()
  h.requests[1].resolve(tree)
  await retry
  await h.generate()
  assert.equal(h.generated[0].branch_id, 'if')
})

test('explicit all-branches scope remains available after loading', async () => {
  const h = harness('')
  h.requests[0].resolve(tree)
  await h.ready
  await h.generate()
  assert.equal(h.generated[0].branch_id, null)
})

test('out-of-order responses cannot replace the current route scope', async () => {
  const h = harness('old')
  h.route.query.branch = 'if'
  const newer = h.loadScope()
  h.requests[1].resolve(tree)
  await newer
  h.requests[0].resolve({ project_id: 'p', roots: [] })
  await h.ready
  assert.equal(h.scopeError.value, '')
  await h.generate()
  assert.equal(h.generated[0].branch_id, 'if')
})

test('repeated query parameters require explicit scope selection', async () => {
  const h = harness(['if', 'other'])
  h.requests[0].resolve(tree)
  await h.ready
  await h.generate()
  assert.ok(h.scopeError.value)
  assert.equal(h.generated.length, 0)
})
