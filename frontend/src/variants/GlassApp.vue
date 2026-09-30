<script setup lang="ts">
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { computed } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import type { IconName } from '@/components/ui/icons'
import { useProjectStore } from '@/stores/project'
import { theme, toggleTheme } from '@/composables/theme'

const route = useRoute()
const projectStore = useProjectStore()
const projectId = computed(() => (route.params.projectId as string) || projectStore.current?.project_id || '')
const nav = computed<{ to: string; label: string; icon: IconName; needsProject: boolean }[]>(() => [
  { to: '/', label: '工作台', icon: 'workspace', needsProject: false },
  { to: `/director/${projectId.value}`, label: '导演台', icon: 'director', needsProject: true },
  { to: `/branches/${projectId.value}`, label: '分支图', icon: 'branches', needsProject: true },
  { to: `/output/${projectId.value}`, label: '输出导出', icon: 'output', needsProject: true },
])
</script>

<template>
  <div class="layout">
    <a class="skip-link" href="#main-content">跳到内容</a>
    <header class="studio-bar">
      <RouterLink to="/" class="brand" aria-label="PlotSystem 首页"><span class="brand-mark"><Icon name="branches" :size="24" /></span><span>PlotSystem<small>STORY STUDIO</small></span></RouterLink>
      <nav aria-label="主导航">
        <template v-for="item in nav" :key="item.label">
          <span v-if="item.needsProject && !projectId" class="nav-item disabled" :title="`${item.label}（先打开项目）`" aria-disabled="true"><Icon :name="item.icon" :size="17" /><span>{{ item.label }}</span></span>
          <RouterLink v-else :to="item.to" class="nav-item"><Icon :name="item.icon" :size="17" /><span>{{ item.label }}</span></RouterLink>
        </template>
      </nav>
      <div class="studio-tools"><span class="project-context"><Icon name="world" :size="14" />{{ projectStore.current?.name || '创作工作空间' }}</span><button class="icon" :aria-label="theme === 'dark' ? '切换到亮色' : '切换到暗色'" :title="theme === 'dark' ? '切换到亮色' : '切换到暗色'" @click="toggleTheme"><Icon :name="theme === 'dark' ? 'sun' : 'moon'" /></button></div>
    </header>
    <main id="main-content" class="content" tabindex="-1"><RouterView /></main>
  </div>
</template>

<style scoped>
.layout { height: 100%; display: flex; flex-direction: column; padding: 22px 32px 0; gap: 22px; max-width: 1920px; margin: auto; }
.studio-bar { display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 78px; padding: 12px 22px; border: 1px solid var(--glass-edge); border-radius: 20px; background: var(--glass); backdrop-filter: blur(18px); box-shadow: var(--glass-shadow); flex-shrink: 0; }
.brand { display: flex; align-items: center; gap: 12px; font-size: 19px; font-weight: 650; letter-spacing: -.5px; }
.brand small { display: block; font-size: 9px; letter-spacing: 2.8px; font-weight: 500; color: var(--ink-2); margin-top: 1px; }
.brand-mark { display: grid; place-items: center; width: 42px; height: 42px; background: var(--active-glass); border: 1px solid var(--glass-edge); border-radius: 13px; color: var(--spot-ink); }
nav { display: flex; gap: 6px; padding: 4px; background: var(--hover); border: 1px solid var(--line); border-radius: 14px; }
.nav-item { display: flex; align-items: center; gap: 9px; padding: 10px 20px; min-height: 42px; border-radius: 10px; color: var(--ink-2); font-size: 13px; transition: background .18s, color .18s; white-space: nowrap; }
.nav-item:hover { background: var(--hover); color: var(--ink); }
.nav-item.router-link-active { background: var(--active-glass); color: var(--ink); box-shadow: inset 0 0 0 1px var(--glass-edge); }
.nav-item.disabled { opacity: .45; cursor: not-allowed; }
.studio-tools { display: flex; align-items: center; gap: 16px; }
.project-context { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--ink-2); max-width: 170px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.content { flex: 1; min-height: 0; overflow: auto; border-radius: 18px 18px 0 0; }
.skip-link { position: fixed; top: -80px; left: 30px; z-index: 100; padding: 12px; background: var(--panel); border-radius: 12px; }
.skip-link:focus { top: 8px; }
@media (max-width: 1100px) { .project-context { display: none; } .nav-item { padding-inline: 14px; } }
@media (max-width: 760px) { .layout { padding: 12px 12px 0; gap: 14px; } .studio-bar { flex-wrap: wrap; padding: 14px; gap: 14px; } nav { order: 3; width: 100%; justify-content: space-around; } .nav-item { flex: 1; justify-content: center; padding: 10px 7px; gap: 6px; font-size: 12px; } .brand { font-size: 17px; } .brand-mark { width: 36px; height: 36px; } }
@media (max-width: 400px) { .nav-item { flex-direction: column; gap: 3px; } }
</style>
