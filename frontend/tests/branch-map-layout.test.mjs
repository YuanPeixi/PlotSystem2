// 分支图布局与谱系工具的纯函数测试（直接转译 src/utils/branches.ts，不需要浏览器）。
import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/branches.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const mod = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'))
const { layoutBranchMap, lineageOf, branchColors, flattenBranches } = mod

const branch = (id, parent = null, fork = null) => ({
  branch_id: id, project_id: 'p', parent_branch_id: parent, fork_from_snapshot_id: fork,
  name: id, scenes: [], director_notes: '',
})
let clock = 0
const scene = (id, branch_id, { parent = null, restore = '', before = `${id}-before`, after = `${id}-after` } = {}) => ({
  scene_id: id, project_id: 'p', branch_id, parent_scene_id: parent, name: id, description: '',
  participating_characters: [], location: '', initial_conditions: {}, max_turns: 6, status: 'completed',
  snapshot_id_before: before, snapshot_id_after: after, restore_snapshot_id: restore,
  turns_completed: 6, speaker_mode: 'round_robin', dialogue_log: [],
  created_at: `2026-09-29T00:00:${String(clock++).padStart(2, '0')}`,
})

// 主线 m1→m2→m3→m4；IF 线从 m3 结束后分叉；重演线从 m4 开场前回滚；IF 线下再分叉一条
const tree = {
  project_id: 'p',
  roots: [{
    branch: branch('main'),
    children: [
      { branch: branch('if', 'main', 'm3-after'), children: [{ branch: branch('if2', 'if', 'i4-after'), children: [] }] },
      { branch: branch('replay', 'main', 'm4-before'), children: [] },
    ],
  }],
}
const scenes = [
  scene('m1', 'main'),
  scene('m2', 'main', { parent: 'm1' }),
  scene('m3', 'main', { parent: 'm2' }),
  scene('m4', 'main', { parent: 'm3' }),
  scene('i4', 'if', { parent: 'm3', restore: 'm3-after' }),
  scene('i5', 'if'), // 手建场景：没有 parent，接在本分支上一场之后
  scene('j5', 'if2', { parent: 'i4', restore: 'i4-after' }),
  scene('r4', 'replay', { parent: 'm4', restore: 'm4-before' }),
]
const cells = () => new Map(layoutBranchMap(tree, scenes).cells.map((c) => [c.scene.scene_id, c]))

test('the main line stacks one scene per row', () => {
  const c = cells()
  assert.deepEqual(['m1', 'm2', 'm3', 'm4'].map((id) => c.get(id).depth), [1, 2, 3, 4])
  assert.equal(c.get('m2').fromSceneId, 'm1')
})

test('a fork after a scene starts on the next row of its own lane', () => {
  const c = cells()
  assert.equal(c.get('i4').depth, 4)
  assert.equal(c.get('i4').fromSceneId, 'm3')
  assert.equal(c.get('i4').replay, false)
  assert.notEqual(c.get('i4').lane, c.get('m4').lane)
})

test('a rollback replays the same scene number, drawn as a replay edge', () => {
  const c = cells()
  assert.equal(c.get('r4').depth, 4)
  assert.equal(c.get('r4').replay, true)
  assert.equal(c.get('r4').fromSceneId, 'm4')
})

test('a hand-made scene without parent continues after the previous scene on its branch', () => {
  const c = cells()
  assert.equal(c.get('i5').depth, 5)
  assert.equal(c.get('i5').fromSceneId, 'i4')
})

test('nested forks keep counting from their source', () => {
  assert.equal(cells().get('j5').depth, 5)
  assert.equal(layoutBranchMap(tree, scenes).maxDepth, 5)
})

test('a corrupted parent cycle does not hang the layout', () => {
  const loop = [scene('a', 'main', { parent: 'b' }), scene('b', 'main', { parent: 'a' })]
  const out = layoutBranchMap(tree, loop)
  assert.equal(out.cells.length, 2)
})

test('scenes on unknown branches are left out instead of crashing', () => {
  const out = layoutBranchMap(tree, [scene('x', 'ghost')])
  assert.equal(out.cells.length, 0)
})

test('lineage walks from the root to the branch; colors give the root the ink color', () => {
  assert.deepEqual(lineageOf(tree, 'if2').map((n) => n.branch.branch_id), ['main', 'if', 'if2'])
  assert.deepEqual(lineageOf(tree, 'nope'), [])
  const colors = branchColors(tree)
  assert.equal(colors.get('main'), 'var(--b-main)')
  assert.equal(new Set(flattenBranches(tree).map((b) => colors.get(b.branch_id))).size, 4)
})
