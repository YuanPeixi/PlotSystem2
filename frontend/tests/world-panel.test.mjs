import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/components/WorldPanel.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm, '')
const compiled = ts.transpileModule(script + `
globalThis.subject = {world, editing, error, saving, load, edit, save, attribute, value, valueType};
`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const tick = () => new Promise(r => setTimeout(r, 0))
const state = (revision) => ({ schema_version: 2, revision_id: revision, facts: {}, definitions: {}, observations: {}, legacy_records: {} })

function harness() {
  const props = { projectId: 'p', branchId: 'A', characters: [], objects: [], busy: false, refreshKey: 0 }
  const gets = [], puts = [], watchers = []
  let ids = 0
  const context = {
    defineProps: () => props, defineEmits: () => () => {},
    ref: value => ({value}), computed: fn => ({get value() { return fn() }}),
    watch: (getter, callback, options) => {
      watchers.push({getter, callback, last: JSON.stringify(getter())})
      if (options?.immediate) callback()
    },
    onBeforeUnmount: () => {}, crypto: {randomUUID: () => `request-${++ids}`},
    api: {
      getWorldState: (_pid, bid) => new Promise((resolve, reject) => gets.push({bid, resolve, reject})),
      getWorldEvents: () => Promise.resolve([]),
      worldCommand: (_pid, bid, payload) => new Promise((resolve, reject) => puts.push({bid, payload, resolve, reject})),
    },
  }
  runInNewContext('globalThis.RequestError = class extends Error {}', context)
  runInNewContext(compiled, context)
  const flush = () => {
    for (const watcher of watchers) {
      const next = JSON.stringify(watcher.getter())
      if (next !== watcher.last) { watcher.last = next; watcher.callback() }
    }
  }
  return {...context.subject, props, gets, puts, flush, RequestError: context.RequestError}
}

test('switching branches clears old facts and drops late reads', async () => {
  const h = harness()
  h.gets[0].resolve(state('A1')); await tick()
  h.edit(); h.props.branchId = 'B'; h.flush()
  assert.equal(h.world.value, null); assert.equal(h.editing.value, false)
  h.gets[1].resolve(state('B1')); await tick()
  assert.equal(h.world.value.revision_id, 'B1')
})

test('commands preserve typed false and send the version the editor read', async () => {
  const h = harness()
  h.gets[0].resolve(state('A1')); await tick()
  h.edit(); h.attribute.value = 'open'; h.valueType.value = 'boolean'; h.value.value = 'false'
  const pending = h.save()
  assert.equal(h.puts[0].payload.revision_id, 'A1')
  assert.equal(h.puts[0].payload.operations[0].value, false)
  h.puts[0].reject(new h.RequestError('版本冲突')); await pending
  assert.equal(h.editing.value, true); assert.match(h.error.value, /版本冲突/)
})

test('unchanged retry keeps the event key; editing produces a new key', async () => {
  const h = harness()
  h.gets[0].resolve(state('A1')); await tick()
  h.edit(); h.attribute.value = 'season'; h.value.value = 'winter'
  let pending = h.save(); h.puts[0].reject(new h.RequestError('失去响应')); await pending
  pending = h.save()
  assert.equal(h.puts[0].payload.request_id, h.puts[1].payload.request_id)
  h.puts[1].reject(new h.RequestError('重试失败')); await pending
  h.value.value = 'spring'; pending = h.save()
  assert.notEqual(h.puts[1].payload.request_id, h.puts[2].payload.request_id)
  h.puts[2].reject(new h.RequestError('终止')); await pending
})

test('a late save cannot clear a draft after switching away and back', async () => {
  const h = harness()
  h.gets[0].resolve(state('A1')); await tick()
  h.edit(); h.attribute.value = 'season'; const pending = h.save()
  h.props.branchId = 'B'; h.flush(); h.gets[1].resolve(state('B1')); await tick()
  h.props.branchId = 'A'; h.flush(); h.gets[2].resolve(state('A2')); await tick()
  h.edit(); h.attribute.value = 'new-attribute'
  h.puts[0].resolve(state('A3')); await pending
  assert.equal(h.editing.value, true); assert.equal(h.attribute.value, 'new-attribute')
  assert.equal(h.world.value.revision_id, 'A2')
})

test('running scenes and invalid booleans cannot produce a world command', async () => {
  const h = harness()
  h.gets[0].resolve(state('A1')); await tick()
  h.props.busy = true; h.edit(); assert.equal(h.editing.value, false)
  h.props.busy = false; h.edit(); h.attribute.value = 'open'; h.valueType.value = 'boolean'; h.value.value = ''
  await h.save(); assert.equal(h.puts.length, 0); assert.match(h.error.value, /请选择/)
})
