import type { Branch, BranchTree, BranchTreeNode, Scene } from '@/types'

/** 主线用墨色，其余按深度优先顺序循环取色；同一分支在所有视图里颜色一致。 */
const PALETTE = ['var(--b2)', 'var(--b3)', 'var(--b4)', 'var(--b5)', 'var(--b6)']

export function flattenBranches(tree: BranchTree): Branch[] {
  const out: Branch[] = []
  const walk = (n: BranchTreeNode) => {
    out.push(n.branch)
    n.children.forEach(walk)
  }
  tree.roots.forEach(walk)
  return out
}

export function branchColors(tree: BranchTree): Map<string, string> {
  const colors = new Map<string, string>()
  let i = 0
  const walk = (n: BranchTreeNode, isRoot: boolean) => {
    colors.set(n.branch.branch_id, isRoot ? 'var(--b-main)' : PALETTE[i++ % PALETTE.length])
    n.children.forEach((c) => walk(c, false))
  }
  tree.roots.forEach((r) => walk(r, true))
  return colors
}

/** 从根到目标分支的路径（含目标）；找不到时返回空数组。 */
export function lineageOf(tree: BranchTree, branchId: string): BranchTreeNode[] {
  const path: BranchTreeNode[] = []
  const dfs = (n: BranchTreeNode): boolean => {
    path.push(n)
    if (n.branch.branch_id === branchId) return true
    for (const c of n.children) if (dfs(c)) return true
    path.pop()
    return false
  }
  for (const r of tree.roots) if (dfs(r)) return path
  return []
}

export interface MapCell {
  scene: Scene
  /** 第几场（从 1 开始），同一场的 IF 线排在同一行 */
  depth: number
  lane: number
  /** 画连线的来源场景；空 = 本分支第一场且无来源 */
  fromSceneId: string
  /** 回滚重演：从某场"开场前"的快照分叉，与来源同一行 */
  replay: boolean
}

/**
 * 分支图布局：纵轴是场次深度，横轴是分支。
 *
 * 深度沿因果链推：分叉/下一场的首场 parent_scene_id 指向来源场景（工单08 I4）；
 * 若它承接的是来源场景的"开场前"快照（回滚重演），就是同一场重来，深度不加一。
 * 手建场景没有 parent，接在本分支上一场之后。
 */
export function layoutBranchMap(tree: BranchTree, scenes: Scene[]) {
  const branches = flattenBranches(tree)
  const lanes = new Map(branches.map((b, i) => [b.branch_id, i]))
  const byId = new Map(scenes.map((s) => [s.scene_id, s]))
  const byBranch = new Map<string, Scene[]>()
  for (const s of [...scenes].sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''))) {
    if (!lanes.has(s.branch_id)) continue
    const list = byBranch.get(s.branch_id) ?? []
    list.push(s)
    byBranch.set(s.branch_id, list)
  }

  const cells = new Map<string, MapCell>()
  const resolving = new Set<string>()
  const resolve = (s: Scene): MapCell => {
    const done = cells.get(s.scene_id)
    if (done) return done
    // 数据损坏成环时按根处理，不让布局卡死
    if (resolving.has(s.scene_id)) return { scene: s, depth: 1, lane: 0, fromSceneId: '', replay: false }
    resolving.add(s.scene_id)
    const siblings = byBranch.get(s.branch_id) ?? []
    const prev = siblings[siblings.indexOf(s) - 1]
    const parent = s.parent_scene_id ? byId.get(s.parent_scene_id) : undefined
    let depth = 1
    let from = ''
    let replay = false
    if (parent) {
      const p = resolve(parent)
      replay = !!s.restore_snapshot_id && s.restore_snapshot_id === parent.snapshot_id_before
      depth = replay ? p.depth : p.depth + 1
      from = parent.scene_id
    } else if (prev) {
      depth = resolve(prev).depth + 1
      from = prev.scene_id
    }
    const cell = { scene: s, depth, lane: lanes.get(s.branch_id) ?? 0, fromSceneId: from, replay }
    cells.set(s.scene_id, cell)
    resolving.delete(s.scene_id)
    return cell
  }
  scenes.forEach((s) => lanes.has(s.branch_id) && resolve(s))

  const maxDepth = Math.max(0, ...[...cells.values()].map((c) => c.depth))
  return { branches, cells: [...cells.values()], maxDepth }
}
