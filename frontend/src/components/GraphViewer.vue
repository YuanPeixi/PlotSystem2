<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { Graph } from '@antv/g6'
import type { GraphData } from '@/types'

const props = defineProps<{ data: GraphData }>()

const container = ref<HTMLDivElement | null>(null)
let graph: Graph | null = null

const COLORS: Record<string, string> = {
  Character: '#4d79ff',
  Location: '#3ec46d',
  Event: '#f0a020',
  Concept: '#9aa0bf',
}

function render() {
  if (!container.value) return
  const g6data = {
    nodes: props.data.nodes.map((n) => ({
      id: n.id,
      data: { label: n.label, nodeType: n.nodeType },
      style: { fill: COLORS[n.nodeType] || COLORS.Concept, labelText: n.label },
    })),
    edges: props.data.edges.map((e, i) => ({
      id: `e${i}`,
      source: e.source,
      target: e.target,
      style: { labelText: e.relType },
    })),
  }

  if (graph) {
    graph.setData(g6data)
    graph.render()
    return
  }

  // 节点数自适应：节点越多，斥力越大、连线越长、画布越大
  const n = g6data.nodes.length || 1
  const nodeStrength = -120 - Math.min(n * 8, 600)
  const linkDistance = 160 + Math.min(n * 4, 240)

  graph = new Graph({
    container: container.value,
    autoFit: 'view',
    autoResize: true,
    padding: 30,
    data: g6data,
    node: {
      style: {
        size: 36,
        labelFill: '#f0f0fa',
        labelFontSize: 12,
        labelPlacement: 'bottom',
        labelBackground: true,
        labelBackgroundFill: 'rgba(15,15,45,0.7)',
        labelBackgroundRadius: 4,
        labelPadding: [2, 6],
      },
    },
    edge: {
      style: {
        stroke: '#3a3f5e',
        labelFill: '#a4abd0',
        labelFontSize: 10,
        endArrow: true,
      },
    },
    layout: {
      type: 'force',
      preventOverlap: true,
      nodeSize: 60,
      nodeSpacing: 30,
      nodeStrength,
      linkDistance,
      edgeStrength: 0.6,
      collideStrength: 0.95,
      alpha: 0.9,
      alphaDecay: 0.02,
    },
    behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
  })
  graph.render()
}

onMounted(render)
watch(() => props.data, render, { deep: true })

onBeforeUnmount(() => {
  graph?.destroy()
  graph = null
})
</script>

<template>
  <div class="graph-wrap">
    <div ref="container" class="graph-canvas"></div>
    <div class="legend">
      <span><i style="background:#4d79ff"></i>人物</span>
      <span><i style="background:#3ec46d"></i>地点</span>
      <span><i style="background:#f0a020"></i>事件</span>
      <span><i style="background:#9aa0bf"></i>概念</span>
    </div>
    <div v-if="!data.nodes.length" class="empty dim">暂无图谱数据，请先上传种子文本并构建。</div>
  </div>
</template>

<style scoped>
.graph-wrap {
  position: relative;
  height: 100%;
  min-height: 420px;
}
.graph-canvas {
  width: 100%;
  height: 100%;
}
.legend {
  position: absolute;
  top: 12px;
  right: 12px;
  display: flex;
  gap: 14px;
  font-size: 12px;
  color: var(--text-dim);
}
.legend i {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 5px;
}
.empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
