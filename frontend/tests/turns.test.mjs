// turn_update 合并与计数口径的纯函数测试（直接转译 src/utils/turns.ts，不需要浏览器）。
// stores/scenes.ts 的 turn_update 处理器只调 applyTurnUpdate，判新旧的逻辑都在这里。
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/turns.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const { applyTurnUpdate, characterTurnCount, isEnvironmentTurn } = await import(
  'data:text/javascript;base64,' + Buffer.from(js).toString('base64')
)

const turn = (id, revision = 0, extra = {}) => ({
  turn_id: id, scene_id: 's', turn_number: 1, character_id: 'c1', character_name: '伊莎贝尔',
  dialogue: null, action: '戴上王冠', inner_thought: null, revision, ...extra,
})

test('newer revision replaces the local turn in place', () => {
  const log = [turn('t1'), turn('t2')]
  const update = turn('t1', 1, { actions: [{ index: 0, status: 'resolved' }] })
  const next = applyTurnUpdate(log, update)
  assert.deepEqual(next.map((t) => t.turn_id), ['t1', 't2'])
  assert.equal(next[0], update)
  assert.equal(log[0].revision, 0, '不改入参')
})

test('stale update after a newer GET does not roll the turn back', () => {
  // 铺底 GET 先拿到 revision 2，队列里 revision 1 的事件随后才到
  assert.equal(applyTurnUpdate([turn('t1', 2)], turn('t1', 1)), null)
  assert.equal(applyTurnUpdate([turn('t1', 2)], turn('t1', 2)), null, '同版本不替换')
})

test('update for a turn the client does not have is ignored', () => {
  assert.equal(applyTurnUpdate([turn('t1')], turn('t9', 3)), null)
})

test('missing revision counts as 0 on both sides', () => {
  const { revision, ...old } = turn('t1')
  assert.notEqual(applyTurnUpdate([old], turn('t1', 1)), null)
  assert.equal(applyTurnUpdate([turn('t1', 1)], old), null)
})

test('only character turns count toward max_turns', () => {
  const log = [turn('a'), turn('e1', 0, { kind: 'environment' }), turn('b', 0, { kind: 'character' }),
    turn('e2', 0, { kind: 'environment' }), turn('old')]
  assert.equal(characterTurnCount(log), 3)
  assert.equal(isEnvironmentTurn({ kind: 'bogus' }), false, '非法取值按角色轮次，至少不让上限失效')
})
