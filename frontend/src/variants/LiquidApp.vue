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

// 窄屏默认图标栏，支持手动展开。
const collapsed = ref(false)
const expanded = ref(false)
function toggleSidebar() {
  if (window.matchMedia('(max-width: 1100px)').matches) expanded.value = !expanded.value
  else collapsed.value = !collapsed.value
}
</script>

<template>
  <div class="layout">
    <a class="skip-link" href="#main-content">跳到内容</a>
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
        <span class="brand-name">PlotSystem<small>故事，由此展开</small></span>
      </div>
      <div class="nav-caption">创作空间</div>
      <nav class="nav" aria-label="主导航">
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
      <div class="sidebar-project">
        <Icon name="world" :size="20" />
        <div><small>当前项目</small><strong>{{ projectStore.current?.name || '等待一个新世界' }}</strong></div>
      </div>
      <div class="sidebar-foot">
        <button class="icon" :title="theme === 'dark' ? '切换到亮色' : '切换到暗色'" @click="toggleTheme">
          <Icon :name="theme === 'dark' ? 'sun' : 'moon'" />
        </button>
        <button class="icon" title="展开或收起侧栏" @click="toggleSidebar">
          <Icon name="sidebar" />
        </button>
      </div>
    </aside>
    <main id="main-content" class="content" tabindex="-1">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  height: 100%;
  padding: 20px;
  gap: 20px;
}
.sidebar {
  width: 224px;
  border: 1px solid var(--glass-edge);
  border-radius: 28px;
  padding: 26px 14px 16px;
  background: var(--glass);
  backdrop-filter: blur(24px) saturate(150%);
  box-shadow: var(--glass-shadow);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.18s ease;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 10px 36px;
}
.brand-mark {
  width: 34px;
  height: 34px;
  flex: none;
}
.brand-name {
  font-weight: 600;
  font-size: 18px;
  white-space: nowrap;
}
.nav {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 48px;
  padding: 0 14px;
  border-radius: 16px;
  color: var(--ink-2);
  white-space: nowrap;
  transition: background-color 0.12s, color 0.12s;
}
.nav-item:hover {
  background: var(--hover);
  color: var(--ink);
}
.nav-item.router-link-active {
  background: var(--active-glass);
  color: var(--spot-ink);
  box-shadow: inset 0 1px 0 var(--glass-edge), 0 5px 16px var(--spot-soft);
  font-weight: 600;
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
  width: 72px;
}
.sidebar.collapsed .brand-name,
.sidebar.collapsed .nav-caption,
.sidebar.collapsed .sidebar-project,
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
@media (max-width: 1100px) {
  .sidebar:not(.expanded) {
    width: 72px;
  }
  .sidebar:not(.expanded) .brand-name,
  .sidebar:not(.expanded) .nav-caption,
  .sidebar:not(.expanded) .sidebar-project,
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
  border-radius: 24px;
}
.brand-name small { display: block; font-size: 10px; font-weight: 400; color: var(--ink-2); letter-spacing: 2px; margin-top: 3px; }
.nav-caption { font-size: 11px; letter-spacing: 2px; color: var(--ink-2); margin: 0 14px 14px; }
.sidebar-project { display: flex; align-items: center; gap: 10px; padding: 16px 10px; border-top: 1px solid var(--line); }
.sidebar-project div { min-width: 0; }
.sidebar-project small { display: block; color: var(--ink-2); font-size: 11px; }
.sidebar-project strong { display: block; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 140px; }
.sidebar-foot { justify-content: space-between; }
.sidebar.collapsed .brand, .sidebar:not(.expanded) .brand { padding-left: 4px; padding-right: 4px; }
.skip-link { position: fixed; top: -80px; left: 30px; z-index: 100; padding: 12px; background: var(--panel); border-radius: 12px; }
.skip-link:focus { top: 8px; }
@media (max-width: 600px) {
  .layout { padding: 8px; gap: 8px; grid-template-columns: 56px minmax(0, 1fr); }
  .sidebar { padding: 18px 6px 10px; border-radius: 22px; }
  .sidebar:not(.expanded) { width: 56px; }
  .sidebar.expanded { position: absolute; inset: 8px auto 8px 8px; z-index: 30; background: var(--panel); }
  .brand { padding-bottom: 24px; }
  .content { border-radius: 18px; grid-column: 2; }
}
</style>
