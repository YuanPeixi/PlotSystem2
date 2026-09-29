<script setup lang="ts">
/**
 * 分支图：多分支是系统的核心，完整的树在这里看。
 * 纵轴是第几场、横轴是分支，同一场的不同 IF 线排在同一行，方便横向比较。
 * 分叉画实线，从来源场景右侧伸出；回滚重演（承接某场开场前的快照）画虚线。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useDirectorStore } from '@/stores/director'
import { useProjectStore } from '@/stores/project'
import { useSceneStore } from '@/stores/scenes'
import PageHeader from '@/components/ui/PageHeader.vue'
import Icon from '@/components/ui/Icon.vue'
import { branchColors, layoutBranchMap, type MapCell } from '@/utils/branches'
import type { Scene } from '@/types'

const props = defineProps<{ projectId: string }>()
const router = useRouter()
const directorStore = useDirectorStore()
const projectStore = useProjectStore()
const sceneStore = useSceneStore()

const scenes = ref<Scene[]>([])
const loading = ref(true)
const error = ref('')
const lineageOnly = ref(false)
const grid = ref<HTMLElement | null>(null)

const STATUS: Record<string, string> = {
  pending: '未开演',
  running: '推演中',
  paused: '已中断',
  completed: '已完成',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    if (projectStore.current?.project_id !== props.projectId) await projectStore.selectProject(props.projectId)
    const [list] = await Promise.all([
      api.listScenes(props.projectId),
      directorStore.loadBranches(props.projectId),
      directorStore.loadSnapshots(props.projectId),
    ])
    scenes.value = list
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(() => props.projectId, load)

const layout = computed(() => layoutBranchMap(directorStore.branchTree, scenes.value))
const colors = computed(() => branchColors(directorStore.branchTree))
const snapLabel = computed(() => new Map(directorStore.snapshots.map((s) => [s.snapshot_id, s.label])))
const cellById = computed(() => new Map(layout.value.cells.map((c) => [c.scene.scene_id, c])))

// 分支的首场若是回滚重演，表头写"回滚自"而不是"分叉自"
const replayBranches = computed(() => {
  const ids = new Set<string>()
  const seen = new Set<string>()
  for (const c of [...layout.value.cells].sort((x, y) => (x.scene.created_at || '').localeCompare(y.scene.created_at || ''))) {
    if (seen.has(c.scene.branch_id)) continue
    seen.add(c.scene.branch_id)
    if (c.replay) ids.add(c.scene.branch_id)
  }
  return ids
})

function origin(branchId: string, forkFrom: string | null): string {
  if (!forkFrom) return '起点'
  const verb = replayBranches.value.has(branchId) ? '回滚自' : '分叉自'
  return `${verb}${snapLabel.value.get(forkFrom) || '快照 ' + forkFrom.slice(0, 6)}`
}

// 当前谱系：从导演台正在看的那一场沿来源一路回溯
const currentId = computed(() =>
  sceneStore.currentScene?.project_id === props.projectId ? sceneStore.currentScene.scene_id : '',
)
const lineage = computed(() => {
  const ids = new Set<string>()
  let c = cellById.value.get(currentId.value)
  while (c && !ids.has(c.scene.scene_id)) {
    ids.add(c.scene.scene_id)
    c = cellById.value.get(c.fromSceneId)
  }
  return ids
})

function open(cell: MapCell) {
  void router.push(`/director/${props.projectId}?scene=${cell.scene.scene_id}`)
}

function statusLine(s: Scene): string {
  const label = STATUS[s.status] || s.status
  return s.turns_completed ? `${label}，${s.turns_completed} 轮` : label
}

// ---- 连线：布局完成后按单元格的实际位置计算 ----
interface Edge {
  d: string
  color: string
  dashed: boolean
  inLineage: boolean
  start?: { x: number; y: number }
}
const edges = ref<Edge[]>([])
function measure() {
  const g = grid.value
  if (!g) return
  const box = g.getBoundingClientRect()
  const rect = (id: string) => g.querySelector<HTMLElement>(`[data-scene="${id}"]`)?.getBoundingClientRect()
  const out: Edge[] = []
  for (const cell of layout.value.cells) {
    if (!cell.fromSceneId) continue
    const a = rect(cell.fromSceneId)
    const b = rect(cell.scene.scene_id)
    if (!a || !b) continue
    const color = colors.value.get(cell.scene.branch_id) || 'var(--line-strong)'
    const inLineage = lineage.value.has(cell.scene.scene_id) && lineage.value.has(cell.fromSceneId)
    const tx = b.left + b.width / 2 - box.left
    const ty = b.top - box.top
    if (cell.replay && Math.abs(a.top - b.top) < 2) {
      // 回滚重演与来源同一行：从来源右侧横连到目标左侧
      const y = a.top + a.height / 2 - box.top
      out.push({
        d: `M${a.right - box.left},${y} H${b.left - box.left}`,
        color,
        dashed: true,
        inLineage,
        start: { x: a.right - box.left, y },
      })
    } else if (Math.abs(a.left + a.width / 2 - (b.left + b.width / 2)) < 2) {
      out.push({ d: `M${tx},${a.bottom - box.top} V${ty}`, color, dashed: cell.replay, inLineage })
    } else {
      // 跨列：从来源右侧横出，圆角转下，落到目标顶部
      const sx = a.right - box.left
      const sy = a.top + a.height / 2 - box.top
      const r = Math.min(10, Math.abs(ty - sy) / 2)
      out.push({
        d: `M${sx},${sy} H${tx - r} Q${tx},${sy} ${tx},${sy + r} V${ty}`,
        color,
        dashed: cell.replay,
        inLineage,
        start: { x: sx, y: sy },
      })
    }
  }
  edges.value = out
}
let observer: ResizeObserver | null = null
watch(
  [layout, grid],
  async () => {
    await nextTick()
    measure()
    if (grid.value && !observer) {
      observer = new ResizeObserver(() => measure())
      observer.observe(grid.value)
    }
  },
  { immediate: true },
)
watch(lineage, () => measure())
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div class="branch-page">
    <PageHeader :context="projectStore.current?.name" title="分支图">
      <div v-if="currentId" class="seg" role="group" aria-label="显示范围">
        <button :aria-pressed="lineageOnly" @click="lineageOnly = true">只看当前谱系</button>
        <button :aria-pressed="!lineageOnly" @click="lineageOnly = false">全部分支</button>
      </div>
      <button class="icon" title="重新加载" :disabled="loading" @click="load"><Icon name="refresh" /></button>
    </PageHeader>

    <div class="legend">
      <span><i class="lg"></i>分叉</span>
      <span><i class="lg dashed"></i>回滚重演</span>
      <span><span class="status-dot running"></span>当前场景</span>
      <span><span class="status-dot pending"></span>未开演</span>
      <span class="dim hint">点任意一场，在导演台打开它</span>
    </div>

    <div class="map-scroll">
      <p v-if="error" class="notice danger"><Icon name="alert" :size="15" />{{ error }}</p>
      <p v-else-if="loading && !scenes.length" class="dim empty">加载中</p>
      <p v-else-if="!layout.branches.length" class="dim empty">还没有分支。先在工作台完成项目构建。</p>

      <div
        v-else
        ref="grid"
        class="map"
        :class="{ 'lineage-only': lineageOnly }"
        :style="{
          gridTemplateColumns: `56px repeat(${layout.branches.length}, 184px)`,
          gridTemplateRows: `auto repeat(${Math.max(1, layout.maxDepth)}, 58px)`,
        }"
      >
        <svg class="edges" aria-hidden="true">
          <g v-for="(e, i) in edges" :key="i" :class="{ faded: lineageOnly && !e.inLineage }">
            <path :d="e.d" :style="{ stroke: e.color }" :stroke-dasharray="e.dashed ? '4 4' : undefined" />
            <rect
              v-if="e.start"
              :x="e.start.x - 3"
              :y="e.start.y - 3"
              width="6"
              height="6"
              rx="1.5"
              :style="{ stroke: e.color }"
            />
          </g>
        </svg>

        <div
          v-for="(b, lane) in layout.branches"
          :key="b.branch_id"
          class="lane-head"
          :style="{ gridColumn: lane + 2 }"
        >
          <span class="swatch" :style="{ background: colors.get(b.branch_id) }"></span>
          <b :title="b.name">{{ b.name || '未命名分支' }}</b>
          <small :title="origin(b.branch_id, b.fork_from_snapshot_id)">{{ origin(b.branch_id, b.fork_from_snapshot_id) }}</small>
        </div>

        <div v-for="d in layout.maxDepth" :key="d" class="row-label num" :style="{ gridRow: d + 1 }">
          第 {{ d }} 场
        </div>

        <button
          v-for="c in layout.cells"
          :key="c.scene.scene_id"
          class="cell"
          :class="[
            c.scene.status,
            { current: c.scene.scene_id === currentId, faded: lineageOnly && !lineage.has(c.scene.scene_id) },
          ]"
          :data-scene="c.scene.scene_id"
          :style="{ gridRow: c.depth + 1, gridColumn: c.lane + 2, '--c': colors.get(c.scene.branch_id) }"
          :title="c.scene.description || c.scene.name"
          @click="open(c)"
        >
          <b>{{ c.scene.name || '未命名场景' }}</b>
          <small>
            <span class="status-dot" :class="c.scene.status"></span>{{ statusLine(c.scene) }}
          </small>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.branch-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.legend {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  padding: 10px 24px;
  border-bottom: 1px solid var(--line);
  font-size: 12px;
  color: var(--ink-2);
}
.legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.lg {
  display: inline-block;
  width: 18px;
  border-top: 1.5px solid var(--ink-3);
}
.lg.dashed {
  border-top-style: dashed;
}
.hint {
  margin-left: auto;
}
.map-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 20px 24px 48px;
}
.empty {
  padding: 48px 0;
  text-align: center;
}
.map {
  position: relative;
  display: grid;
  width: max-content;
  column-gap: 28px;
  row-gap: 26px;
}
.map > :not(svg) {
  position: relative;
  z-index: 1;
}
.edges {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
  pointer-events: none;
  z-index: 0;
}
.edges path {
  fill: none;
  stroke-width: 1.5;
}
.edges rect {
  fill: var(--bg);
  stroke-width: 1.5;
}
.edges .faded {
  opacity: 0.2;
}
.lane-head {
  grid-row: 1;
  align-self: end;
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr);
  column-gap: 6px;
  align-items: center;
  padding-bottom: 2px;
  min-width: 0;
}
.lane-head b {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lane-head small {
  grid-column: 2;
  font-size: 11.5px;
  color: var(--ink-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-label {
  grid-column: 1;
  align-self: center;
  font-size: 12px;
  color: var(--ink-3);
}
.cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 2px;
  height: 58px;
  padding: 8px 12px 8px 16px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--panel);
  text-align: left;
  min-width: 0;
  transition: border-color 0.12s, opacity 0.15s;
}
.cell::before {
  content: '';
  position: absolute;
  left: 6px;
  top: 10px;
  bottom: 10px;
  width: 3px;
  border-radius: 2px;
  background: var(--c);
}
.cell:hover {
  background: var(--panel);
  border-color: var(--ink-3);
}
.cell b {
  font-weight: 600;
  font-size: 13.5px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cell small {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--ink-3);
  font-size: 12px;
}
.cell.pending {
  border-style: dashed;
  background: transparent;
}
.cell.current {
  border-color: var(--spot);
  box-shadow: 0 0 0 3px var(--spot-soft);
}
.cell.current small {
  color: var(--spot-ink);
}
.cell.faded {
  opacity: 0.28;
}
</style>
