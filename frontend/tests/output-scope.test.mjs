// Run the real Output.vue script with mocked Vue refs and HTTP, without a browser.
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/pages/Output.vue', import.meta.url), 'utf8')
const script = source
  .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  .replace(/^import .*$/gm, '')
const template = source.match(/<template>([\s\S]*?)<\/template>/)[1]
const compiled = ts.transpileModule(script + `
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

// --- 以下为 PR review 修复的回归测试 ---

test('in-flight reload cannot overwrite a manual branch selection', async () => {
  // #9：重置若在 await 之前同步执行，就绕开了竞态守卫 —— 用户手选的分支
  // 会被静默改回路由值，点生成便导出了全部分支。
  const h = harness('')
  h.requests[0].resolve(tree)
  await h.ready
  h.branchId.value = 'if'
  h.scopeError.value = ''

  const reload = h.loadScope()
  assert.equal(h.branchId.value, 'if', '响应回来之前不得改写用户选择')
  h.requests[1].resolve(tree)
  await reload
})

test('scope errors always offer a retry affordance', () => {
  // #8：分支不存在/参数无效会把 scopeReady 置真，按 !scopeReady 显示重试
  // 等于这两条路径没有出口；项目无分支时下拉框选不动，用户彻底卡死。
  const retry = template.match(/<button v-if="([^"]*)"[^>]*@click="loadScope"/)
  assert.ok(retry, '重试按钮必须存在且绑定 loadScope')
  assert.equal(retry[1].replace(/\s/g, ''), 'scopeError')
})

test('retry recovers from a deleted-branch error without touching the select', async () => {
  const h = harness('deleted')
  h.requests[0].resolve(tree)
  await h.ready
  assert.ok(h.scopeError.value)
  assert.equal(h.scopeReady.value, true, '该路径确实把 scopeReady 置真')

  // 用户改用"全部分支"后重试，应恢复到可生成状态。
  h.route.query.branch = ''
  const retry = h.loadScope()
  h.requests[1].resolve(tree)
  await retry
  assert.equal(h.scopeError.value, '')
  await h.generate()
  assert.equal(h.generated[0].branch_id, null)
})
