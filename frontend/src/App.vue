<script setup lang="ts">
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { computed, ref } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import type { IconName } from '@/components/ui/icons'
import { useProjectStore } from '@/stores/project'
import { theme, toggleTheme } from '@/composables/theme'

const route = useRoute()
const projectStore = useProjectStore()
// 工作台没有路由参数，退到当前打开的项目，否则从工作台点不进导演台
const projectId = computed(
  () => (route.params.projectId as string) || projectStore.current?.project_id || '',
)

const nav = computed<{ to: string; label: string; icon: IconName; needsProject: boolean }[]>(() => [
  { to: '/', label: '工作台', icon: 'workspace', needsProject: false },
  { to: `/director/${projectId.value}`, label: '导演台', icon: 'director', needsProject: true },
  { to: `/branches/${projectId.value}`, label: '分支图', icon: 'branches', needsProject: true },
  { to: `/output/${projectId.value}`, label: '输出导出', icon: 'output', needsProject: true },
])

// 宽屏默认展开、可手动收起；1440 以下默认图标栏、可手动展开（见样式）
const collapsed = ref(false)
const expanded = ref(false)
function toggleSidebar() {
  if (window.matchMedia('(max-width: 1440px)').matches) expanded.value = !expanded.value
  else collapsed.value = !collapsed.value
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar" :class="{ collapsed, expanded }">
      <div class="brand">
        <svg class="brand-mark" viewBox="0 0 26 26" aria-hidden="true">
          <rect x="1" y="1" width="24" height="24" rx="7" fill="var(--ink)" />
          <path
            d="M8 18V8h5.2a3.3 3.3 0 0 1 0 6.6H8"
            fill="none"
            stroke="var(--panel)"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <circle cx="17.5" cy="18" r="1.8" fill="var(--spot)" />
        </svg>
        <span class="brand-name">PlotSystem</span>
      </div>
      <nav class="nav">
        <template v-for="item in nav" :key="item.label">
          <!-- 没有打开项目时不渲染成链接：指向 / 的链接会被当成当前页高亮 -->
          <span
            v-if="item.needsProject && !projectId"
            class="nav-item disabled"
            :title="`${item.label}（先在工作台打开一个项目）`"
          >
            <Icon :name="item.icon" />
            <span class="label">{{ item.label }}</span>
          </span>
          <RouterLink v-else :to="item.to" class="nav-item" :title="item.label">
            <Icon :name="item.icon" />
            <span class="label">{{ item.label }}</span>
          </RouterLink>
        </template>
      </nav>
      <div class="sidebar-foot">
        <button class="icon" :title="theme === 'dark' ? '切换到亮色' : '切换到暗色'" @click="toggleTheme">
          <Icon :name="theme === 'dark' ? 'sun' : 'moon'" />
        </button>
        <button class="icon" title="展开或收起侧栏" @click="toggleSidebar">
          <Icon name="sidebar" />
        </button>
      </div>
    </aside>
    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  height: 100%;
}
.sidebar {
  width: 216px;
  border-right: 1px solid var(--line);
  padding: 14px 10px 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.18s ease;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 5px 18px;
}
.brand-mark {
  width: 26px;
  height: 26px;
  flex: none;
}
.brand-name {
  font-weight: 600;
  font-size: 15px;
  white-space: nowrap;
}
.nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 34px;
  padding: 0 10px;
  border-radius: var(--r-sm);
  color: var(--ink-2);
  white-space: nowrap;
  transition: background-color 0.12s, color 0.12s;
}
.nav-item:hover {
  background: var(--hover);
  color: var(--ink);
}
.nav-item.router-link-active {
  background: var(--panel);
  color: var(--ink);
  box-shadow: 0 0 0 1px var(--line);
}
.nav-item.disabled {
  opacity: 0.4;
  pointer-events: none;
}
.sidebar-foot {
  display: flex;
  gap: 2px;
  padding-top: 8px;
}

/* 收起态：只留图标 */
.sidebar.collapsed {
  width: 56px;
}
.sidebar.collapsed .brand-name,
.sidebar.collapsed .label {
  display: none;
}
.sidebar.collapsed .nav-item {
  justify-content: center;
  padding: 0;
}
.sidebar.collapsed .sidebar-foot {
  flex-direction: column;
  align-items: center;
}
@media (max-width: 1440px) {
  .sidebar:not(.expanded) {
    width: 56px;
  }
  .sidebar:not(.expanded) .brand-name,
  .sidebar:not(.expanded) .label {
    display: none;
  }
  .sidebar:not(.expanded) .nav-item {
    justify-content: center;
    padding: 0;
  }
  .sidebar:not(.expanded) .sidebar-foot {
    flex-direction: column;
    align-items: center;
  }
}

.content {
  min-width: 0;
  height: 100%;
  overflow: auto;
  position: relative;
}
</style>
