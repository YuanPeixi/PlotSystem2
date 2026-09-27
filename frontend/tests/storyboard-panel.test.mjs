// Run the real StoryboardPanel.vue script with mocked Vue reactivity and HTTP, without a browser.
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/components/StoryboardPanel.vue', import.meta.url), 'utf8')
const script = source
  .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  .replace(/^import .*$/gm, '')
const compiled = ts.transpileModule(script + `
globalThis.subject = { board, draft, conflict, saveError, loadError, loading, saving, load, startEdit, addBeat,
  removeBeat, moveBeat, save, cancelEdit, reloadAfterConflict };
`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText


function board(branch, overrides = {}) {
  return {
    project_id: 'p', branch_id: branch, memo: '', goal_revision: 'r', fork_origin: null,
    changelog: [], revision: 3, next_beat_seq: 3, updated_at: '', goal_stale: false,
    narrative_goal: '揭露叛徒', current_goal_revision: 'r',
    outline: [
      { beat_id: 'b1', title: '查账', description: '', status: 'planned', resolved_scene_id: '' },
      { beat_id: 'b2', title: '对质', description: '当面', status: 'done', resolved_scene_id: 's' },
    ],
    ...overrides,
  }
}

/** watch 的最小替身：记录 getter，flush() 时比较新旧值并触发回调。 */
function harness(branch = 'main') {
  const props = { projectId: 'p', branchId: branch, refreshKey: null }
  const gets = [], puts = [], watchers = []
  const context = {
    console,
    defineProps: () => props,
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn() } }),
    watch: (getter, callback, opts) => {
      const w = { getter, callback, last: getter() }
      watchers.push(w)
      if (opts?.immediate) callback()
    },
    api: {
      getStoryboard: (_p, b) => new Promise((resolve, reject) => gets.push({ branch: b, resolve, reject })),
      updateStoryboard: (_p, b, payload) =>
        new Promise((resolve, reject) => puts.push({ branch: b, payload, resolve, reject })),
    },
  }
  // ApiError 必须建在沙箱自己的 realm 里：组件用 instanceof Error 判错误，跨 realm 的 Error 不认
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
  return { ...context.subject, props, gets, puts, flush, ApiError: context.ApiError }
}

const tick = () => new Promise(r => setTimeout(r, 0))

test('switching branch drops the draft and never saves it into the new branch', async () => {
  const h = harness('A')
  h.gets[0].resolve(board('A'))
  await tick()
  h.startEdit()
  h.draft.value.memo = 'A 线的草稿'
  h.props.branchId = 'B'
  h.flush()
  assert.equal(h.draft.value, null)
  assert.equal(h.board.value, null)
  await h.save()
  assert.equal(h.puts.length, 0)
  assert.equal(h.gets.at(-1).branch, 'B')
})

test('a slow response for the previous branch cannot overwrite the current panel', async () => {
  const h = harness('A')
  h.props.branchId = 'B'
  h.flush()
  h.gets[1].resolve(board('B', { memo: 'B' }))
  await tick()
  h.gets[0].resolve(board('A', { memo: 'A' }))
  await tick()
  assert.equal(h.board.value.memo, 'B')
})

test('save sends back existing beat ids, omits ids for new beats, and carries the revision', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.startEdit()
  h.addBeat()
  h.draft.value.outline[2].title = '设局'
  h.addBeat() // 空标题的新节拍不提交
  h.moveBeat(2, -2)
  h.draft.value.confirmGoal = true
  const saving = h.save()
  const { payload, branch } = h.puts[0]
  assert.equal(branch, 'main')
  assert.equal(payload.revision, 3)
  assert.equal(payload.confirm_goal, true)
  assert.deepEqual(payload.outline.map(b => b.beat_id ?? null), [null, 'b1', 'b2'])
  assert.equal(payload.outline[0].title, '设局')
  assert.equal(payload.outline[2].status, 'done')
  h.puts[0].resolve(board('main', { revision: 4 }))
  await saving
  assert.equal(h.draft.value, null)
  assert.equal(h.board.value.revision, 4)
})

test('409 keeps the draft, shows a reload prompt, and does not retry', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.startEdit()
  h.draft.value.memo = '我的修改'
  const saving = h.save()
  h.puts[0].reject(new h.ApiError('分镜稿已被修改', 409))
  await saving
  assert.ok(h.conflict.value)
  assert.equal(h.draft.value.memo, '我的修改')
  assert.equal(h.puts.length, 1)
  const reload = h.reloadAfterConflict()
  assert.equal(h.draft.value, null)
  h.gets[1].resolve(board('main', { revision: 5 }))
  await reload
  assert.equal(h.board.value.revision, 5)
})

test('other failures surface as a save error, not a conflict', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.startEdit()
  const saving = h.save()
  h.puts[0].reject(new h.ApiError('节拍标题超过 24 tokens', 422))
  await saving
  assert.equal(h.conflict.value, '')
  assert.match(h.saveError.value, /24 tokens/)
  assert.ok(h.draft.value)
})

test('refresh key reloads only when not editing', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.props.refreshKey = { scene_id: 's1' }
  h.flush()
  assert.equal(h.gets.length, 2)
  h.gets[1].resolve(board('main'))
  await tick()
  h.startEdit()
  h.props.refreshKey = { scene_id: 's2' }
  h.flush()
  assert.equal(h.gets.length, 2)
})

/** A 上开始保存 → 切到 B → 切回 A 并加载完，此时 A 的旧保存还没回来。 */
async function saveThenRoundTrip() {
  const h = harness('A')
  h.gets[0].resolve(board('A'))
  await tick()
  h.startEdit()
  h.draft.value.memo = '第一份'
  const saving = h.save()
  h.props.branchId = 'B'
  h.flush()
  h.props.branchId = 'A'
  h.flush()
  h.gets.at(-1).resolve(board('A'))
  await tick()
  return { h, saving }
}

test('a late save response cannot clear a draft started after switching away and back', async () => {
  const { h, saving } = await saveThenRoundTrip()
  h.startEdit()
  h.draft.value.memo = '新草稿'
  h.puts[0].resolve(board('A', { revision: 4 }))
  await saving
  assert.equal(h.draft.value.memo, '新草稿')
  assert.equal(h.board.value.revision, 3)
})

test('a late save failure is not reported against a newer draft', async () => {
  const { h, saving } = await saveThenRoundTrip()
  h.startEdit()
  h.puts[0].reject(new h.ApiError('分镜稿已被修改', 409))
  await saving
  assert.equal(h.conflict.value, '')
  assert.equal(h.saveError.value, '')
  assert.ok(h.draft.value)
})

test('a save still in flight on the previous branch does not block saving here', async () => {
  const { h } = await saveThenRoundTrip()
  h.startEdit()
  void h.save()
  assert.equal(h.puts.length, 2)
})

test('clearing the title of an existing beat is refused locally instead of deleting it', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.startEdit()
  h.draft.value.outline[1].title = '   '
  await h.save()
  assert.equal(h.puts.length, 0)
  assert.match(h.saveError.value, /b2/)
  assert.equal(h.draft.value.outline.length, 2)
})

test('retrying an unchanged draft reuses its request id; editing it issues a new one', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.startEdit()
  h.addBeat()
  h.draft.value.outline[2].title = '新节拍'
  const first = h.save()
  h.puts[0].reject(new Error('Network Error')) // 响应丢了：客户端不知道新节拍拿到了什么 ID
  await first
  const retry = h.save()
  h.puts[1].reject(new Error('Network Error'))
  await retry
  assert.ok(h.puts[0].payload.request_id)
  assert.equal(h.puts[1].payload.request_id, h.puts[0].payload.request_id)
  assert.deepEqual(h.puts[1].payload, h.puts[0].payload)
  h.draft.value.memo = '改了内容'
  void h.save()
  assert.notEqual(h.puts[2].payload.request_id, h.puts[0].payload.request_id)
})

test('confirming the goal sends the revision of the goal text the user was shown', async () => {
  const h = harness()
  h.gets[0].resolve(board('main', { goal_stale: true, narrative_goal: '新目标', current_goal_revision: 'r2' }))
  await tick()
  h.startEdit()
  assert.equal(h.draft.value.goalText, '新目标')
  h.draft.value.confirmGoal = true
  void h.save()
  assert.equal(h.puts[0].payload.confirm_goal, true)
  assert.equal(h.puts[0].payload.goal_revision_seen, 'r2')
})

test('edits typed while a save is in flight are kept, not cleared by the response', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.startEdit()
  h.draft.value.memo = 'A'
  const saving = h.save()
  // 模板里编辑区在保存中是只读的；这里模拟绕过它（如输入法合成事件）的那次改动
  h.draft.value.memo = 'A 然后继续输入 B'
  h.puts[0].resolve(board('main', { revision: 4, memo: 'A' }))
  await saving
  assert.equal(h.draft.value.memo, 'A 然后继续输入 B')
  assert.equal(h.draft.value.revision, 3) // 不改基准：下次保存冲突就走 409 提示
  assert.equal(h.board.value.revision, 4)
  assert.match(h.saveError.value, /尚未保存/)
})

test('beat editing actions are ignored while saving', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.startEdit()
  void h.save()
  assert.equal(h.saving.value, true)
  h.addBeat()
  h.removeBeat(0)
  h.moveBeat(0, 1)
  assert.deepEqual(h.draft.value.outline.map(b => b.beat_id), ['b1', 'b2'])
})

test('a refresh issued before a successful save cannot roll the panel back', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.props.refreshKey = { scene_id: 's1' } // 评估到达，刷新发出、尚未返回
  h.flush()
  h.startEdit()
  h.draft.value.memo = '改'
  const saving = h.save()
  h.puts[0].resolve(board('main', { revision: 4, memo: '改' }))
  await saving
  h.gets[1].resolve(board('main', { revision: 3 }))
  await tick()
  assert.equal(h.board.value.revision, 4)
  assert.equal(h.board.value.memo, '改')
})

test('a successful save clears a load error left by an earlier failed refresh', async () => {
  const h = harness()
  h.gets[0].resolve(board('main'))
  await tick()
  h.props.refreshKey = { scene_id: 's1' }
  h.flush()
  h.gets[1].reject(new Error('Network Error'))
  await tick()
  assert.ok(h.loadError.value)
  h.startEdit()
  h.draft.value.memo = '改'
  const saving = h.save()
  h.puts[0].resolve(board('main', { revision: 4, memo: '改' }))
  await saving
  assert.equal(h.board.value.revision, 4)
  assert.equal(h.loadError.value, '')
})
