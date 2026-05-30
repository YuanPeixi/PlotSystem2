<script setup lang="ts">
import type { BranchTree } from '@/types'

defineProps<{ tree: BranchTree }>()
defineEmits<{ (e: 'select', branchId: string): void }>()
</script>

<template>
  <div class="scene-tree">
    <div v-if="!tree.roots.length" class="dim" style="padding: 20px">暂无分支。构建项目后将自动创建主线。</div>
    <ul v-else class="tree-root">
      <li v-for="node in tree.roots" :key="node.branch.branch_id">
        <BranchNode :node="node" @select="$emit('select', $event)" />
      </li>
    </ul>
  </div>
</template>

<script lang="ts">
import { defineComponent, h } from 'vue'
import type { BranchTreeNode } from '@/types'

const BranchNode = defineComponent({
  name: 'BranchNode',
  props: { node: { type: Object as () => BranchTreeNode, required: true } },
  emits: ['select'],
  setup(props, { emit }) {
    const render = (node: BranchTreeNode): any =>
      h('div', { class: 'branch-node' }, [
        h(
          'div',
          {
            class: 'branch-pill',
            onClick: () => emit('select', node.branch.branch_id),
          },
          `🌿 ${node.branch.name || '未命名分支'}`,
        ),
        node.children.length
          ? h(
              'ul',
              { class: 'branch-children' },
              node.children.map((c) => h('li', {}, [render(c)])),
            )
          : null,
      ])
    return () => render(props.node)
  },
})
export default { components: { BranchNode } }
</script>

<style scoped>
.tree-root,
:deep(.branch-children) {
  list-style: none;
  padding-left: 18px;
}
:deep(.branch-pill) {
  display: inline-block;
  background: var(--accent);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 12px;
  margin: 4px 0;
  cursor: pointer;
}
:deep(.branch-pill:hover) {
  border-color: var(--highlight);
}
</style>
