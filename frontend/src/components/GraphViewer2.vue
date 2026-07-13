<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Graph } from '@antv/g6'
import type { GraphData, GraphEdge, GraphNode } from '@/types'

const props = defineProps<{ data: GraphData }>()

const container = ref<HTMLDivElement | null>(null)
let graph: Graph | null = null

const COLORS: Record<string, string> = {
  Character: '#4d79ff',
  Location: '#3ec46d',
  Event: '#f0a020',
  Concept: '#9aa0bf',
}

const TYPE_LABELS: Record<string, string> = {
  Character: '人物',
  Location: '地点',
  Event: '事件',
  Concept: '概念',
}

const searchQuery = ref('')
const selectedType = ref('all')
const selectedRelation = ref('all')
const neighborsOnly = ref(false)
const showRelationLabels = ref(false)
const selectedNodeId = ref<string | null>(null)

const typeOptions = computed(() => {
  const types = new Set(props.data.nodes.map((node) => node.nodeType))
  return [...types].sort()
})

const relationOptions = computed(() => {
  const relations = new Set(props.data.edges.map((edge) => edge.relType))
  return [...relations].sort()
})

const normalizedQuery = computed(() => searchQuery.value.trim().toLocaleLowerCase())

const matchingNodeIds = computed(() => {
  const ids = new Set<string>()
  props.data.nodes.forEach((node) => {
    const matchesQuery = !normalizedQuery.value || node.label.toLocaleLowerCase().includes(normalizedQuery.value)
    const matchesType = selectedType.value === 'all' || node.nodeType === selectedType.value
    if (matchesQuery && matchesType) ids.add(node.id)
  })
  return ids
})

const selectedNode = computed<GraphNode | null>(() => {
  if (!selectedNodeId.value) return null
  return props.data.nodes.find((node) => node.id === selectedNodeId.value) || null
})

const selectedRelations = computed(() => {
  if (!selectedNodeId.value) return []
  return props.data.edges.filter(
    (edge) => edge.source === selectedNodeId.value || edge.target === selectedNodeId.value,
  )
})

const selectedNeighborIds = computed(() => {
  const ids = new Set<string>()
  if (!selectedNodeId.value) return ids
  props.data.edges.forEach((edge) => {
    if (selectedRelation.value !== 'all' && edge.relType !== selectedRelation.value) return
    if (edge.source === selectedNodeId.value) ids.add(edge.target)
    if (edge.target === selectedNodeId.value) ids.add(edge.source)
  })
  return ids
})

const filteredNodes = computed(() => {
  return props.data.nodes.filter((node) => {
    if (!matchingNodeIds.value.has(node.id)) return false
    if (neighborsOnly.value && selectedNodeId.value) {
      return node.id === selectedNodeId.value || selectedNeighborIds.value.has(node.id)
    }
    return true
  })
})

const filteredNodeIds = computed(() => new Set(filteredNodes.value.map((node) => node.id)))

const filteredEdges = computed(() => {
  return props.data.edges.filter((edge) => {
    if (selectedRelation.value !== 'all' && edge.relType !== selectedRelation.value) return false
    return filteredNodeIds.value.has(edge.source) && filteredNodeIds.value.has(edge.target)
  })
})

const visibleCount = computed(() => `${filteredNodes.value.length} / ${props.data.nodes.length}`)

function typeLabel(type: string) {
  return TYPE_LABELS[type] || type
}

function nodeLabel(id: string) {
  return props.data.nodes.find((node) => node.id === id)?.label || id
}

function edgeTouchesSelected(edge: GraphEdge) {
  return edge.source === selectedNodeId.value || edge.target === selectedNodeId.value
}

function buildG6Data() {
  const hasSelection = Boolean(selectedNodeId.value)
  return {
    nodes: filteredNodes.value.map((node) => {
      const isSelected = node.id === selectedNodeId.value
      const isNeighbor = selectedNeighborIds.value.has(node.id)
      const isDimmed = hasSelection && !isSelected && !isNeighbor
      return {
        id: node.id,
        data: { label: node.label, nodeType: node.nodeType },
        style: {
          fill: COLORS[node.nodeType] || COLORS.Concept,
          fillOpacity: isDimmed ? 0.22 : 1,
          size: isSelected ? 46 : 34,
          labelText: node.label,
          labelFill: isDimmed ? '#555b78' : '#e6e6f0',
          labelFontSize: isSelected ? 13 : 11,
          labelPlacement: 'bottom' as const,
          labelMaxWidth: 120,
          labelWordWrap: true,
        },
      }
    }),
    edges: filteredEdges.value.map((edge, index) => {
      const isSelected = edgeTouchesSelected(edge)
      return {
        id: `graph2-edge-${index}-${edge.source}-${edge.target}`,
        source: edge.source,
        target: edge.target,
        data: { relType: edge.relType },
        style: {
          stroke: isSelected ? '#e94560' : '#3a3f5e',
          lineWidth: isSelected ? 2 : 1,
          opacity: hasSelection && !isSelected ? 0.18 : 0.78,
          labelText: showRelationLabels.value || isSelected ? edge.relType : '',
          labelFill: isSelected ? '#ff9bad' : '#858ba9',
          labelFontSize: 10,
          endArrow: true,
        },
      }
    }),
  }
}

async function render() {
  if (!container.value) return
  const g6data = buildG6Data()

  if (graph) {
    graph.setData(g6data)
    await graph.render()
    return
  }

  graph = new Graph({
    container: container.value,
    autoFit: 'view',
    data: g6data,
    node: {
      type: 'circle',
      style: {
        stroke: '#1a1a2e',
        lineWidth: 2,
        labelFill: '#e6e6f0',
        labelFontSize: 11,
        labelPlacement: 'bottom',
      },
    },
    edge: {
      style: {
        stroke: '#3a3f5e',
        labelFill: '#858ba9',
        labelFontSize: 10,
        endArrow: true,
      },
    },
    layout: {
      type: 'd3-force',
      preventOverlap: true,
      nodeSize: 46,
      nodeSpacing: 12,
      collideStrength: 1,
      collideIterations: 4,
      nodeStrength: -140,
      linkDistance: 150,
      iterations: 800,
      animation: false,
    },
    behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element-force'],
  })

  graph.on('node:click', (event: any) => {
    const id = event.target?.id
    if (typeof id === 'string') selectedNodeId.value = id
  })
  graph.on('canvas:click', () => {
    selectedNodeId.value = null
  })
  await graph.render()
}

async function fitView() {
  await graph?.fitView()
}

async function resetView() {
  searchQuery.value = ''
  selectedType.value = 'all'
  selectedRelation.value = 'all'
  neighborsOnly.value = false
  selectedNodeId.value = null
  await nextTick()
  await fitView()
}

function clearSelection() {
  selectedNodeId.value = null
}

watch(
  () => props.data,
  async () => {
    if (selectedNodeId.value && !props.data.nodes.some((node) => node.id === selectedNodeId.value)) {
      selectedNodeId.value = null
    }
    await render()
  },
  { deep: true },
)

watch(
  [searchQuery, selectedType, selectedRelation, neighborsOnly, showRelationLabels, selectedNodeId],
  () => render(),
)

onMounted(render)

onBeforeUnmount(() => {
  graph?.destroy()
  graph = null
})
</script>

<template>
  <div class="graph-viewer2">
    <div class="toolbar">
      <input v-model="searchQuery" class="search" type="search" placeholder="搜索节点名称" />
      <select v-model="selectedType" aria-label="节点类型">
        <option value="all">全部节点类型</option>
        <option v-for="type in typeOptions" :key="type" :value="type">{{ typeLabel(type) }}</option>
      </select>
      <select v-model="selectedRelation" aria-label="关系类型">
        <option value="all">全部关系类型</option>
        <option v-for="relation in relationOptions" :key="relation" :value="relation">{{ relation }}</option>
      </select>
      <label class="check-control">
        <input v-model="neighborsOnly" type="checkbox" :disabled="!selectedNode" />
        <span>仅看邻居</span>
      </label>
      <label class="check-control">
        <input v-model="showRelationLabels" type="checkbox" />
        <span>关系文字</span>
      </label>
      <span class="count dim">{{ visibleCount }}</span>
      <button class="tool-button ghost" type="button" @click="fitView">适配</button>
      <button class="tool-button ghost" type="button" @click="resetView">重置</button>
    </div>

    <div class="viewer-body">
      <div class="graph-stage">
        <div ref="container" class="graph-canvas"></div>
        <div class="legend">
          <span v-for="type in typeOptions" :key="type">
            <i :style="{ background: COLORS[type] || COLORS.Concept }"></i>{{ typeLabel(type) }}
          </span>
        </div>
        <div v-if="data.nodes.length && !filteredNodes.length" class="empty dim">没有符合当前筛选条件的节点</div>
        <div v-if="!data.nodes.length" class="empty dim">暂无图谱数据，请先构建项目</div>
      </div>

      <aside class="detail-panel" :class="{ 'empty-panel': !selectedNode }">
        <template v-if="selectedNode">
          <div class="detail-heading">
            <div>
              <span class="type-dot" :style="{ background: COLORS[selectedNode.nodeType] || COLORS.Concept }"></span>
              <span class="dim">{{ typeLabel(selectedNode.nodeType) }}</span>
            </div>
            <button class="close-button" type="button" aria-label="清除选择" @click="clearSelection">×</button>
          </div>
          <h4>{{ selectedNode.label }}</h4>
          <div class="detail-stat">
            <span class="dim">相连关系</span>
            <strong>{{ selectedRelations.length }}</strong>
          </div>
          <div class="relation-list">
            <div v-for="(edge, index) in selectedRelations" :key="`${edge.source}-${edge.target}-${index}`" class="relation-item">
              <span class="relation-name">{{ edge.relType }}</span>
              <span class="relation-target">{{ nodeLabel(edge.source === selectedNode.id ? edge.target : edge.source) }}</span>
            </div>
            <span v-if="!selectedRelations.length" class="dim">暂无关系</span>
          </div>
          <button class="focus-button" type="button" @click="neighborsOnly = true">聚焦邻居</button>
        </template>
        <div v-else class="detail-empty dim">
          <strong>选择一个节点</strong>
          <span>查看关系和节点详情</span>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.graph-viewer2 {
  height: 100%;
  min-height: 420px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.toolbar .search {
  width: 156px;
}

.toolbar select {
  width: auto;
  min-width: 112px;
  padding: 7px 9px;
  font-size: 12px;
}

.check-control {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin: 0;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-dim);
}

.check-control input {
  width: auto;
  accent-color: var(--highlight);
}

.count {
  margin-left: auto;
  font-size: 12px;
  white-space: nowrap;
}

.tool-button {
  padding: 6px 9px;
  font-size: 12px;
}

.viewer-body {
  min-height: 0;
  flex: 1;
  display: flex;
  gap: 10px;
}

.graph-stage {
  position: relative;
  min-width: 0;
  flex: 1;
  min-height: 300px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: rgba(15, 52, 96, 0.16);
}

.graph-canvas {
  width: 100%;
  height: 100%;
}

.legend {
  position: absolute;
  left: 10px;
  bottom: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  max-width: 80%;
  font-size: 11px;
  color: var(--text-dim);
  pointer-events: none;
}

.legend i {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 4px;
  border-radius: 50%;
}

.detail-panel {
  width: 204px;
  flex: 0 0 204px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: rgba(26, 26, 46, 0.55);
  overflow: auto;
}

.empty-panel {
  display: flex;
  align-items: center;
  justify-content: center;
}

.detail-heading,
.detail-stat,
.relation-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.detail-heading > div {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.type-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}

.close-button {
  border: 0;
  padding: 0 5px;
  background: transparent;
  color: var(--text-dim);
  font-size: 20px;
  line-height: 1;
}

.detail-panel h4 {
  margin: 20px 0 14px;
  color: var(--text);
  font-size: 17px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.detail-stat {
  padding: 8px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}

.relation-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}

.relation-item {
  align-items: flex-start;
  font-size: 12px;
}

.relation-name {
  color: #ff9bad;
  overflow-wrap: anywhere;
}

.relation-target {
  max-width: 98px;
  color: var(--text-dim);
  text-align: right;
  overflow-wrap: anywhere;
}

.focus-button {
  width: 100%;
  margin-top: 16px;
  padding: 7px 8px;
  font-size: 12px;
}

.detail-empty {
  display: flex;
  flex-direction: column;
  gap: 5px;
  text-align: center;
  font-size: 12px;
}

.detail-empty strong {
  color: var(--text);
  font-size: 13px;
}

.empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  text-align: center;
}

@media (max-width: 720px) {
  .viewer-body {
    flex-direction: column;
  }

  .detail-panel {
    width: 100%;
    flex-basis: auto;
    min-height: 128px;
  }

  .empty-panel {
    min-height: 96px;
  }

  .toolbar .search {
    flex: 1 1 140px;
  }

  .count {
    margin-left: 0;
  }
}
</style>
