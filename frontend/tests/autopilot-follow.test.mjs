// Run the real scenes store (AutoPilot 跟随逻辑) with mocked pinia/api/EventSource, without a browser.
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/stores/scenes.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(
  source.replace(/^import .*$/gm, '').replace('export const useSceneStore', 'globalThis.useSceneStore'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022 } },
).outputText

function session(overrides = {}) {
  return {
    session_id: 'ap1', project_id: 'p1', request_id: 'r', max_steps: 5,
    max_consecutive_rollbacks: 2, status: 'running', phase: 'running_scene',
    current_scene_id: 's1', steps_taken: 0, consecutive_rollbacks: 0,
    stop_reason: '', stop_message: '', steps: [],
    started_at: '2026-09-30T00:00:00', updated_at: '2026-09-30T00:00:01',
    ...overrides,
  }
}

const step = (next) => ({ scene_id: 's', decision_type: 'next_scene', next_scene_id: next, next_branch_id: 'b', at: '' })

/**
 * sceneImpl / autopilotImpl 缺省立即返回；传入函数可换成手动控制的 deferred 或抛错，
 * 用来构造"响应迟到 / 请求失败"的时序。默认把 store 绑定到项目 p1（导演页挂载时做的事）。
 */
async function makeStore({ scenes = {}, failStarts = 0, sceneImpl, autopilotImpl, bind = 'p1' } = {}) {
  const calls = { getScene: [], streams: [], start: [], intervals: 0, getAutopilot: 0 }
  class FakeEventSource {
    static CLOSED = 2
    constructor(url) { this.url = url; this.readyState = 1; calls.streams.push(url) }
    addEventListener() {}
    close() { this.readyState = 2 }
  }
  const context = {
    console,
    Error,
    EventSource: FakeEventSource,
    setInterval: () => { calls.intervals++; return 1 },
    clearInterval: () => {},
    globalThis: {},
    defineStore: (_id, fn) => fn,
    ref: value => ({ value }),
    openSceneStream: id => new FakeEventSource(`/scenes/${id}/stream`),
    api: {
      getSceneById: async (id) => {
        calls.getScene.push(id)
        const base = { scene_id: id, branch_id: 'b', status: 'running', dialogue_log: [], ...(scenes[id] || {}) }
        return sceneImpl ? sceneImpl(id, calls.getScene.length, base) : base
      },
      getEvaluation: async () => null,
      getDecision: async () => null,
      getAutopilot: async (pid) => {
        calls.getAutopilot++
        return autopilotImpl ? autopilotImpl(pid, calls.getAutopilot) : null
      },
      startAutopilot: async (pid, payload) => {
        calls.start.push(payload)
        if (calls.start.length <= failStarts) throw new Error('网络超时')
        return session({ session_id: `ap-${calls.start.length}` })
      },
    },
  }
  runInNewContext(compiled, context)
  const store = context.globalThis.useSceneStore()
  if (bind) await store.bindAutopilot?.(bind)
  return { store, calls }
}

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const flush = () => new Promise(r => setTimeout(r, 0))

test('a newly started session takes the stage to its current scene', async () => {
  const { store, calls } = await makeStore()
  store.applyAutopilot(session())
  await flush()
  assert.deepEqual(calls.getScene, ['s1'])
  assert.equal(store.currentScene.value.scene_id, 's1')
  assert.equal(store.autopilotFollows.value, 1)
})

test('polls that bring no progress never drag the user back from another scene', async () => {
  const { store, calls } = await makeStore()
  store.applyAutopilot(session())
  await flush()
  // 用户点开别的场景翻看
  store.currentScene.value = { scene_id: 'other' }
  store.applyAutopilot(session({ updated_at: '2026-09-30T00:00:02' }))
  await flush()
  assert.deepEqual(calls.getScene, ['s1'])
  assert.equal(store.currentScene.value.scene_id, 'other')
})

test('a new step follows to the next scene; a stale poll cannot roll it back', async () => {
  const { store, calls } = await makeStore({ scenes: { s2: { status: 'pending' } } })
  store.applyAutopilot(session())
  await flush()
  store.applyAutopilot(session({ current_scene_id: 's2', steps_taken: 1, steps: [step('s2')], updated_at: '2026-09-30T00:00:05' }))
  await flush()
  assert.equal(store.currentScene.value.scene_id, 's2')
  // 下一场尚未开跑也要连上流：后端马上就会开演
  assert.ok(calls.streams.includes('/scenes/s2/stream'))
  // 迟到的轮询响应（更早的 updated_at）
  store.applyAutopilot(session({ updated_at: '2026-09-30T00:00:03' }))
  await flush()
  assert.equal(store.autopilot.value.current_scene_id, 's2')
  assert.deepEqual(calls.getScene, ['s1', 's2'])
})

test('a stopped session found on page load does not move the stage', async () => {
  const { store, calls } = await makeStore()
  store.applyAutopilot(session({ status: 'stopped', stop_reason: 'max_steps' }))
  await flush()
  assert.deepEqual(calls.getScene, [])
  assert.equal(calls.intervals, 0)
})

test('an automatic continue on an open stream keeps the stream instead of reattaching', async () => {
  const { store, calls } = await makeStore()
  store.applyAutopilot(session())
  await flush()
  store.evaluation.value = { scene_id: 's1' }
  store.applyAutopilot(session({
    steps_taken: 1, steps: [{ ...step('s1'), decision_type: 'continue' }], updated_at: '2026-09-30T00:00:05',
  }))
  await flush()
  assert.deepEqual(calls.getScene, ['s1'])
  assert.equal(store.evaluation.value, null)
})

test('retrying an unchanged start reuses the request id; changing parameters issues a new one', async () => {
  const { store, calls } = await makeStore({ failStarts: 1 })
  await assert.rejects(store.startAutopilot('p1', 's1', 5, 2))
  await store.startAutopilot('p1', 's1', 5, 2)
  await store.startAutopilot('p1', 's1', 3, 2)
  // 失败后原样重试：同一个幂等键，后端据此认出重放
  assert.equal(calls.start[0].request_id, calls.start[1].request_id)
  // 成功之后、或参数变了：新的一次开启
  assert.notEqual(calls.start[1].request_id, calls.start[2].request_id)
})

// ---- 评审修复：异步结果的提交边界 ----

test('a stale follow response arriving late cannot switch the stage or stream back', async () => {
  const late = deferred()
  const { store, calls } = await makeStore({
    sceneImpl: (id, _n, base) => (id === 's1' ? late.promise.then(() => base) : base),
  })
  store.applyAutopilot(session())                       // 跟随 s1（请求挂起）
  store.applyAutopilot(session({ current_scene_id: 's2', steps_taken: 1, steps: [step('s2')], updated_at: '2026-09-30T00:00:05' }))
  await flush()
  assert.equal(store.currentScene.value.scene_id, 's2')
  const streamsBefore = calls.streams.length

  late.resolve()                                        // s1 的旧响应这才回来
  await flush()
  assert.equal(store.currentScene.value.scene_id, 's2')
  assert.equal(calls.streams.length, streamsBefore, '旧响应不得再开 s1 的流')
})

test('a failed follow is retried on the next poll even though the session did not advance', async () => {
  const { store, calls } = await makeStore({
    sceneImpl: (id, n, base) => {
      if (n === 1) throw new Error('网络抖动')
      return base
    },
  })
  store.applyAutopilot(session())
  await flush()
  assert.equal(store.currentScene.value, null)
  // 下一次轮询：同一步、同一场，只是更新了时间戳
  store.applyAutopilot(session({ updated_at: '2026-09-30T00:00:02' }))
  await flush()
  assert.deepEqual(calls.getScene, ['s1', 's1'])
  assert.equal(store.currentScene.value.scene_id, 's1')
  // 到达之后的轮询不再重复跟随（用户翻看别处也不会被拽回）
  store.currentScene.value = { scene_id: 'other' }
  store.applyAutopilot(session({ updated_at: '2026-09-30T00:00:03' }))
  await flush()
  assert.equal(calls.getScene.length, 2)
})

test('a poll in flight when the page is left cannot revive the old session', async () => {
  const pending = deferred()
  let armed = false
  const { store, calls } = await makeStore({
    autopilotImpl: () => (armed ? pending.promise : null),
  })
  armed = true
  const poll = store.refreshAutopilot('p1')             // 离页前发出的轮询
  store.resetAutopilot()
  const intervals = calls.intervals
  pending.resolve(session())
  await poll
  await flush()
  assert.equal(store.autopilot.value, null)
  assert.equal(calls.intervals, intervals, '不得重新建立轮询')
  assert.deepEqual(calls.getScene, [])
})

test('a session of another project is ignored after switching projects', async () => {
  const { store, calls } = await makeStore({ bind: 'p2' })
  store.applyAutopilot(session())                       // p1 的会话（例如旧流上迟到的事件）
  await flush()
  assert.equal(store.autopilot.value, null)
  assert.deepEqual(calls.getScene, [])
})
