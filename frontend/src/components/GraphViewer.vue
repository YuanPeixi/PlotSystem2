<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { Graph } from '@antv/g6'
import { cssVar, theme } from '@/composables/theme'
import type { GraphData } from '@/types'

const props = defineProps<{ data: GraphData }>()

const container = ref<HTMLDivElement | null>(null)
let graph: Graph | null = null

/** 按当前主题取色：G6 在 JS 里配色，读不到 CSS 变量，每次渲染前现取 */
function palette() {
  return {
    Character: cssVar('--g-character'),
    Location: cssVar('--g-location'),
    Event: cssVar('--g-event'),
    Concept: cssVar('--g-concept'),
    label: cssVar('--ink'),
    edgeLabel: cssVar('--ink-3'),
    edge: cssVar('--line-strong'),
  }
}

function render() {
  if (!container.value) return
  const c = palette()
  const fillOf = (t: string) => (c as Record<string, string>)[t] || c.Concept
  const g6data = {
    nodes: props.data.nodes.map((n) => ({
      id: n.id,
      data: { label: n.label, nodeType: n.nodeType },
      style: { fill: fillOf(n.nodeType), labelText: n.label },
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

  graph = new Graph({
    container: container.value,
    autoFit: 'view',
    data: g6data,
    node: {
      style: {
        size: 36,
        labelFill: c.label,
        labelFontSize: 12,
        labelPlacement: 'bottom',
      },
    },
    edge: {
      style: { stroke: c.edge, labelFill: c.edgeLabel, labelFontSize: 10, endArrow: true },
    },
    // G6 的 force（不是 d3-force）把 nodeStrength 当斥力权重用：正数才是互相排斥，
    // 类型注释写反了。曾配成 -60，节点互相吸引、再被向心力拉拢，整张图塌成一团。
    // preventOverlap 要配 nodeSize 才生效。
    layout: {
      type: 'force',
      preventOverlap: true,
      nodeSize: 36,
      nodeSpacing: 16,
      nodeStrength: 1000,
      edgeStrength: 200,
      linkDistance: 140,
      gravity: 8,
    },
    behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
  })
  graph.render()
}

onMounted(render)
watch(() => props.data, render, { deep: true })
// 节点/连线的颜色是创建时写死进 G6 的，换主题只能重建
watch(theme, () => {
  graph?.destroy()
  graph = null
  render()
})

onBeforeUnmount(() => {
  graph?.destroy()
  graph = null
})
</script>

<template>
  <div class="graph-wrap">
    <div ref="container" class="graph-canvas"></div>
    <div class="legend">
      <span><i style="background: var(--g-character)"></i>人物</span>
      <span><i style="background: var(--g-location)"></i>地点</span>
      <span><i style="background: var(--g-event)"></i>事件</span>
      <span><i style="background: var(--g-concept)"></i>概念</span>
    </div>
    <div v-if="!data.nodes.length" class="empty dim">还没有图谱数据。上传种子文本并构建后会出现在这里。</div>
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
  color: var(--ink-2);
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
