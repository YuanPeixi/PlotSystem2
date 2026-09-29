<script setup lang="ts">
/**
 * 导演台左栏：只展示当前谱系（祖先链 + 当前分支 + 其直接子分支），
 * 完整的树交给分支图——分支再多，左栏也不会无限变长。
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import Icon from '@/components/ui/Icon.vue'
import { branchColors, flattenBranches, lineageOf } from '@/utils/branches'
import type { BranchTree, BranchTreeNode, Scene, SnapshotMeta } from '@/types'

const props = defineProps<{
  projectId: string
  tree: BranchTree
  branchId: string
  scenes: Scene[]
  currentSceneId: string
  snapshots: SnapshotMeta[]
  composing: boolean
}>()
const emit = defineEmits<{
  (e: 'select-branch', id: string): void
  (e: 'select-scene', id: string): void
  (e: 'compose'): void
}>()

const colors = computed(() => branchColors(props.tree))
const total = computed(() => flattenBranches(props.tree).length)

interface Row {
  node: BranchTreeNode
  indent: number
}
const rows = computed<Row[]>(() => {
  const path = lineageOf(props.tree, props.branchId)
  if (!path.length) return props.tree.roots.map((node) => ({ node, indent: 0 }))
  const out: Row[] = path.map((node, i) => ({ node, indent: i }))
  const here = path[path.length - 1]
  here.children.forEach((node) => out.push({ node, indent: path.length }))
  return out
})

const snapLabel = computed(() => new Map(props.snapshots.map((s) => [s.snapshot_id, s.label])))
function origin(node: BranchTreeNode): string {
  const sid = node.branch.fork_from_snapshot_id
  if (!sid) return ''
  return `分叉自${snapLabel.value.get(sid) || '快照 ' + sid.slice(0, 6)}`
}
</script>

<template>
  <nav class="rail" aria-label="分支与场景">
    <div class="section-title">
      分支
      <RouterLink :to="`/branches/${projectId}`" class="map-link" title="打开分支图">
        <Icon name="branches" :size="15" />
      </RouterLink>
    </div>
    <p v-if="!tree.roots.length" class="dim empty">暂无分支。构建项目后会自动创建主线。</p>
    <ul class="tree">
      <li v-for="r in rows" :key="r.node.branch.branch_id" :style="{ paddingLeft: r.indent * 14 + 'px' }">
        <button
          class="node"
          :aria-current="r.node.branch.branch_id === branchId"
          @click="emit('select-branch', r.node.branch.branch_id)"
        >
          <span class="swatch" :style="{ background: colors.get(r.node.branch.branch_id) }"></span>
          <span class="name">{{ r.node.branch.name || '未命名分支' }}</span>
        </button>
        <span v-if="origin(r.node)" class="origin">{{ origin(r.node) }}</span>
      </li>
    </ul>
    <RouterLink v-if="total > rows.length" :to="`/branches/${projectId}`" class="more">
      <Icon name="branches" :size="15" />在分支图中查看全部 {{ total }} 条分支
    </RouterLink>

    <div class="section-title">
      本分支场景
      <button class="icon sm" title="让导演规划下一场" :disabled="!branchId" @click="emit('compose')">
        <Icon name="plus" :size="15" />
      </button>
    </div>
    <ul class="scenes">
      <li v-for="(s, i) in scenes" :key="s.scene_id">
        <button
          :aria-current="!composing && s.scene_id === currentSceneId"
          @click="emit('select-scene', s.scene_id)"
        >
          <span class="n num">{{ i + 1 }}</span>
          <span class="name">{{ s.name || '未命名场景' }}</span>
          <span class="status-dot" :class="s.status" :title="s.status"></span>
        </button>
      </li>
      <li v-if="composing">
        <button aria-current="true">
          <span class="n num">{{ scenes.length + 1 }}</span>
          <span class="name dim">规划中的新场景</span>
          <span class="status-dot pending"></span>
        </button>
      </li>
    </ul>
    <p v-if="!scenes.length && !composing" class="dim empty">
      这条分支还没有场景。点上面的加号，让导演规划第一场。
    </p>
  </nav>
</template>

<style scoped>
.rail {
  padding: 14px 10px 24px;
}
.map-link {
  display: inline-grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: var(--r-sm);
  color: var(--ink-2);
}
.map-link:hover {
  background: var(--hover);
  color: var(--ink);
}
.section-title {
  padding: 0 8px;
}
.empty {
  font-size: 12.5px;
  padding: 4px 8px 12px;
}
.tree,
.scenes {
  list-style: none;
}
.tree li {
  position: relative;
}
.node,
.scenes button {
  width: 100%;
  justify-content: flex-start;
  border: 0;
  background: transparent;
  text-align: left;
}
.node {
  gap: 8px;
  height: 32px;
  padding: 0 8px;
}
.node:hover,
.scenes button:hover {
  background: var(--hover);
}
.node[aria-current='true'],
.scenes button[aria-current='true'] {
  background: var(--panel);
  box-shadow: 0 0 0 1px var(--line);
}
.name {
  overflow: hidden;
  text-overflow: ellipsis;
}
.origin {
  display: block;
  font-size: 11.5px;
  color: var(--ink-3);
  padding: 0 8px 4px 24px;
  margin-top: -3px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.more {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 8px;
  margin: 2px 0 6px;
  border-radius: var(--r-sm);
  color: var(--ink-2);
  font-size: 12.5px;
}
.more:hover {
  background: var(--hover);
  color: var(--ink);
}
.scenes button {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto;
  gap: 6px;
  height: auto;
  min-height: 34px;
  padding: 4px 8px;
}
.n {
  color: var(--ink-3);
  font-size: 12px;
}
.scenes .name {
  white-space: nowrap;
}
</style>
