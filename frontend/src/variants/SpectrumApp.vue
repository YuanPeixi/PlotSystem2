<script setup lang="ts">
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { computed, ref } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import type { IconName } from '@/components/ui/icons'
import { useProjectStore } from '@/stores/project'
import { theme, toggleTheme } from '@/composables/theme'
const route = useRoute()
const store = useProjectStore()
const compact = ref(false)
const projectId = computed(() => (route.params.projectId as string) || store.current?.project_id || '')
const nav = computed<{ to: string; label: string; description: string; icon: IconName; needsProject: boolean }[]>(() => [
  { to: '/', label: '工作台', description: '项目与世界设定', icon: 'workspace', needsProject: false },
  { to: `/director/${projectId.value}`, label: '导演台', description: '场景与角色推演', icon: 'director', needsProject: true },
  { to: `/branches/${projectId.value}`, label: '分支图', description: '探索剧情走向', icon: 'branches', needsProject: true },
  { to: `/output/${projectId.value}`, label: '导出作品', description: '整理与输出成品', icon: 'output', needsProject: true },
])
</script>
<template>
  <div class="studio">
    <a class="skip-link" href="#main-content">跳到内容</a>
    <header class="app-bar">
      <RouterLink to="/" class="brand"><span class="brand-mark">P<span>·</span></span><strong>PlotSystem</strong><span class="brand-divider"></span><span class="brand-caption">故事创作工作室</span></RouterLink>
      <div class="app-tools"><span class="project-title">{{ store.current?.name || '未选择项目' }}</span><button class="icon" :title="theme === 'dark' ? '切换到亮色' : '切换到暗色'" :aria-label="theme === 'dark' ? '切换到亮色' : '切换到暗色'" @click="toggleTheme"><Icon :name="theme === 'dark' ? 'sun' : 'moon'" /></button></div>
    </header>
    <div class="studio-body" :class="{ compact }">
      <aside class="sidebar">
        <div class="nav-label">工作空间</div>
        <nav aria-label="主导航">
          <template v-for="item in nav" :key="item.label">
            <span v-if="item.needsProject && !projectId" class="nav-item disabled" :title="`${item.label}：请先打开项目`" aria-disabled="true"><Icon :name="item.icon" :size="18" /><span class="nav-copy"><strong>{{ item.label }}</strong><small>{{ item.description }}</small></span></span>
            <RouterLink v-else :to="item.to" class="nav-item" :title="item.label"><Icon :name="item.icon" :size="18" /><span class="nav-copy"><strong>{{ item.label }}</strong><small>{{ item.description }}</small></span></RouterLink>
          </template>
        </nav>
        <div class="sidebar-note"><Icon name="world" :size="22" /><strong>一个世界，多种可能。</strong><p>从种子文本到角色行动，<br />让每个故事有迹可循。</p></div>
        <button class="collapse-button ghost" :aria-pressed="compact" :aria-label="compact ? '展开导航' : '收起导航'" @click="compact = !compact"><Icon name="sidebar" :size="16" /><span class="nav-copy">收起导航</span></button>
      </aside>
      <main id="main-content" class="content" tabindex="-1"><RouterView /></main>
    </div>
  </div>
</template>
<style scoped>
.studio { height: 100%; display: flex; flex-direction: column; }
.app-bar { height: 60px; flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; padding: 0 24px; border-bottom: 1px solid var(--line); background: var(--panel); gap: 16px; }
.brand { display: flex; align-items: center; gap: 10px; }
.brand-mark { display: grid; grid-template-columns: auto auto; align-items: center; width: 30px; height: 32px; padding: 0 6px; border-radius: 6px; color: #fff; background: var(--brand); font-size: 23px; font-weight: 700; line-height: 1; }
.brand-mark span { color: #ffd4bd; }
.brand strong { font-size: 16px; letter-spacing: -.4px; }
.brand-divider { width: 1px; height: 18px; background: var(--line-strong); margin-inline: 8px; }
.brand-caption { color: var(--ink-2); font-size: 12px; }
.app-tools { display: flex; align-items: center; gap: 16px; min-width: 0; }
.project-title { color: var(--ink-2); font-size: 12px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.studio-body { display: grid; grid-template-columns: 196px minmax(0, 1fr); flex: 1; min-height: 0; }
.sidebar { background: var(--panel-2); border-right: 1px solid var(--line); display: flex; flex-direction: column; padding: 26px 12px 12px; }
.nav-label { font-size: 11px; font-weight: 600; color: var(--ink-2); padding: 0 14px 14px; }
nav { display: flex; flex-direction: column; gap: 5px; }
.nav-item { display: flex; align-items: center; gap: 12px; padding: 12px 14px; color: var(--ink-2); border-radius: 6px; min-height: 56px; transition: background-color .15s; }
.nav-item:hover { background: var(--hover); color: var(--ink); }
.nav-item.router-link-active { background: var(--spot-soft); color: var(--spot-ink); box-shadow: inset 3px 0 0 var(--spot); }
.nav-copy strong { display: block; font-size: 13px; font-weight: 600; }
.nav-copy small { display: block; font-size: 10px; margin-top: 2px; color: var(--ink-2); }
.nav-item.disabled { opacity: .5; cursor: not-allowed; }
.sidebar-note { margin-top: auto; padding: 36px 14px 24px; color: var(--ink-2); }
.sidebar-note strong { display: block; font-size: 11px; margin-top: 12px; }
.sidebar-note p { font-size: 10px; line-height: 1.8; margin-top: 8px; }
.collapse-button { justify-content: flex-start; font-size: 12px; color: var(--ink-2); }
.content { min-width: 0; overflow: auto; position: relative; }
.compact { grid-template-columns: 64px minmax(0, 1fr); }
.compact .nav-copy, .compact .nav-label, .compact .sidebar-note { display: none; }
.compact .nav-item { padding: 12px 10px; }
.skip-link { position: fixed; top: -80px; left: 20px; z-index: 100; background: var(--panel); padding: 12px; border: 2px solid var(--spot); }
.skip-link:focus { top: 6px; }
@media (max-width: 1000px) { .studio-body { grid-template-columns: 64px minmax(0, 1fr); } .nav-copy, .nav-label, .sidebar-note, .collapse-button { display: none; } .nav-item { padding: 12px 10px; } }
@media (max-width: 600px) { .app-bar { height: 54px; padding-inline: 14px; } .brand-caption, .brand-divider, .project-title { display: none; } .studio-body { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); } .sidebar { padding: 5px 8px; border-right: 0; border-bottom: 1px solid var(--line); } nav { flex-direction: row; justify-content: space-around; } .nav-item { flex: 1; min-height: 42px; justify-content: center; padding: 8px 4px; gap: 6px; } .nav-copy { display: block; } .nav-copy small { display: none; } .nav-copy strong { font-size: 11px; } .nav-item.router-link-active { box-shadow: inset 0 -2px 0 var(--spot); } }
</style>
