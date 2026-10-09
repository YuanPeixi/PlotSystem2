// 舞台底栏导演建议的纯函数测试（直接转译 src/utils/decision.ts，不需要浏览器）。
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/decision.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const { stageDecisionHint } = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'))

const ev = (extra = {}) => ({ scene_id: 's1', narrative_goal_score: 7, recommended_decision: 'next_scene', ...extra })

test('recommended decision is highlighted without extra text', () => {
  for (const d of ['continue', 'next_scene', 'rollback']) {
    assert.deepEqual(stageDecisionHint(ev({ recommended_decision: d }), 's1'), { recommended: d, note: '' })
  }
})

test('no evaluation yet: say so, highlight nothing', () => {
  assert.deepEqual(stageDecisionHint(null, 's1'), { recommended: '', note: '评估中…' })
})

test("another scene's evaluation is treated as not arrived yet", () => {
  assert.deepEqual(stageDecisionHint(ev({ scene_id: 'old' }), 's1'), { recommended: '', note: '评估中…' })
})

test('unavailable evaluation (-1 scores) never yields a recommendation', () => {
  assert.deepEqual(stageDecisionHint(ev({ narrative_goal_score: -1 }), 's1'), {
    recommended: '',
    note: '评估不可用',
  })
})

test('unknown decision value highlights nothing', () => {
  assert.deepEqual(stageDecisionHint(ev({ recommended_decision: 'retry' }), 's1'), { recommended: '', note: '' })
})
