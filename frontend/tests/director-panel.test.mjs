// Run the real DirectorPanel.vue script with mocked Vue reactivity, without a browser.
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/components/DirectorPanel.vue', import.meta.url), 'utf8')
const script = source
  .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  .replace(/^import .*$/gm, '')
const compiled = ts.transpileModule(script + `
globalThis.subject = { scores, progress, goalMissing };
`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function evaluation(overrides = {}) {
  return {
    scene_id: 's1', synopsis: '', recommended_decision: 'next_scene', rollback_suggestion: null,
    narrative_goal_score: 2, dramatic_tension_score: 2, plot_deviation_score: 9,
    character_consistency_score: 8, story_progress: 0.6, story_progress_raw: 0.6,
    progress_stalled: false, is_ending_reached: false, ending_reason: '', unresolved_threads: [],
    world_state_delta: {}, evaluated_snapshot_id: '', goal_missing: false,
    ...overrides,
  }
}

function panel(ev) {
  const context = {
    defineProps: () => ({ evaluation: ev, sceneId: 's1' }),
    defineEmits: () => () => {},
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn() } }),
  }
  runInNewContext(compiled, context)
  return context.subject
}

const byLabel = (scores) => Object.fromEntries(scores.map(s => [s.label, s]))

test('goal-anchored scores are flagged and never shown as danger when the goal was missing', () => {
  const s = byLabel(panel(evaluation({ goal_missing: true })).scores.value)
  assert.equal(s['目标达成'].unanchored, true)
  assert.equal(s['目标达成'].danger, false)
  assert.equal(s['主线偏离'].unanchored, true)
  assert.equal(s['主线偏离'].danger, false)
  // 与主线目标无关的两项照常判红
  assert.equal(s['戏剧张力'].unanchored, false)
  assert.equal(s['戏剧张力'].danger, true)
})

test('with a goal the same scores are anchored and can be danger', () => {
  const p = panel(evaluation())
  const s = byLabel(p.scores.value)
  assert.equal(p.goalMissing.value, false)
  assert.equal(s['目标达成'].unanchored, false)
  assert.equal(s['目标达成'].danger, true)
  assert.equal(s['主线偏离'].danger, true)
  assert.equal(p.progress.value, 0.6)
})

test('progress is not drawn when the goal was missing, even if a stale number is stored', () => {
  // 字段上线前的记录：推进度里存着无目标时期的噪声
  const p = panel(evaluation({ goal_missing: true, story_progress: 0.6 }))
  assert.equal(p.goalMissing.value, true)
  assert.equal(p.progress.value, null)
})
