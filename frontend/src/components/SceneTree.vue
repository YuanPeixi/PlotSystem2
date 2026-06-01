<script setup lang="ts">
import { computed } from 'vue'
import type { BranchTree, Scene } from '@/types'

const props = defineProps<{
  tree: BranchTree
  scenes?: Scene[]
  selectedBranchId?: string
  selectedSceneId?: string
}>()
const emit = defineEmits<{
  (e: 'select', branchId: string): void
  (e: 'select-scene', sceneId: string): void
}>()

/** 按分支聚合场景，按创建顺序排好（list_scenes 后端已按 created_at 排序）。 */
const scenesByBranch = computed<Record<string, Scene[]>>(() => {
  const map: Record<string, Scene[]> = {}
  for (const s of props.scenes || []) {
    if (!s.branch_id) continue
    ;(map[s.branch_id] ||= []).push(s)
  }
  return map
})

function clickBranch(id: string) {
  emit('select', id)
}
function clickScene(s: Scene) {
  emit('select-scene', s.scene_id)
}
</script>

<template>
  <div class="scene-tree">
    <div v-if="!tree.roots.length" class="dim" style="padding: 20px">
      暂无分支。构建项目后将自动创建主线。
    </div>
    <ul v-else class="tree-root">
      <li v-for="node in tree.roots" :key="node.branch.branch_id">
        <BranchNode
          :node="node"
          :scenes-by-branch="scenesByBranch"
          :selected-branch-id="selectedBranchId"
          :selected-scene-id="selectedSceneId"
          @select="clickBranch"
          @select-scene="clickScene"
        />
      </li>
    </ul>
  </div>
</template>

<script lang="ts">
import { defineComponent, h } from 'vue'
import type { BranchTreeNode as TNode, Scene as TScene } from '@/types'

const STATUS_TEXT: Record<string, string> = {
  pending: '待开始',
  running: '运行',
  paused: '暂停',
  completed: '完成',
}

const BranchNode = defineComponent({
  name: 'BranchNode',
  props: {
    node: { type: Object as () => TNode, required: true },
    scenesByBranch: { type: Object as () => Record<string, TScene[]>, required: true },
    selectedBranchId: { type: String, default: '' },
    selectedSceneId: { type: String, default: '' },
  },
  emits: ['select', 'select-scene'],
  setup(props, { emit }) {
    const renderScene = (s: TScene) =>
      h(
        'li',
        {
          class: ['scene-item', { active: props.selectedSceneId === s.scene_id }],
          onClick: (ev: Event) => {
            ev.stopPropagation()
            emit('select-scene', s)
          },
          title: s.description || s.name,
        },
        [
          h('span', { class: 'scene-name' }, `🎬 ${s.name || '未命名场景'}`),
          h(
            'span',
            { class: ['scene-badge', `b-${s.status || 'pending'}`] },
            STATUS_TEXT[s.status] || s.status,
          ),
        ],
      )

    const render = (node: TNode): any => {
      const scenes = props.scenesByBranch[node.branch.branch_id] || []
      return h('div', { class: 'branch-node' }, [
        h(
          'div',
          {
            class: ['branch-pill', { active: props.selectedBranchId === node.branch.branch_id }],
            onClick: () => emit('select', node.branch.branch_id),
          },
          [
            h('span', `🌿 ${node.branch.name || '未命名分支'}`),
            h('span', { class: 'scene-count' }, `（${scenes.length}）`),
          ],
        ),
        scenes.length
          ? h(
              'ul',
              { class: 'scene-list' },
              scenes.map((s) => renderScene(s)),
            )
          : null,
        node.children.length
          ? h(
              'ul',
              { class: 'branch-children' },
              node.children.map((c) =>
                h('li', { key: c.branch.branch_id }, [render(c)]),
              ),
            )
          : null,
      ])
    }
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
  display: inline-flex;
  align-items: center;
  background: var(--accent);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 12px;
  margin: 4px 0;
  cursor: pointer;
  font-weight: 500;
  transition: all 0.15s;
}
:deep(.branch-pill:hover),
:deep(.branch-pill.active) {
  border-color: var(--highlight);
  filter: brightness(1.15);
}
:deep(.scene-count) {
  color: var(--text-dim);
  margin-left: 4px;
  font-size: 12px;
  font-weight: 400;
}
:deep(.scene-list) {
  list-style: none;
  padding-left: 22px;
  margin: 4px 0;
}
:deep(.scene-item) {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 10px;
  margin: 3px 0;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
  font-size: 13px;
  gap: 6px;
}
:deep(.scene-item:hover) {
  background: var(--bg);
  border-color: var(--border);
}
:deep(.scene-item.active) {
  background: var(--bg);
  border-color: var(--highlight);
}
:deep(.scene-name) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 160px;
}
:deep(.scene-badge) {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 999px;
  white-space: nowrap;
  flex-shrink: 0;
}
:deep(.b-pending) {
  background: rgba(148, 163, 184, 0.25);
  color: #cbd5e1;
}
:deep(.b-running) {
  background: rgba(29, 78, 216, 0.5);
  color: #dbeafe;
}
:deep(.b-paused) {
  background: rgba(245, 158, 11, 0.3);
  color: #fde68a;
}
:deep(.b-completed) {
  background: rgba(34, 197, 94, 0.3);
  color: #bbf7d0;
}
</style>
