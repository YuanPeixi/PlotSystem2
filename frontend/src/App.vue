<script setup lang="ts">
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { computed } from 'vue'

const route = useRoute()
const projectId = computed(() => (route.params.projectId as string) || '')
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="logo">
        <span class="logo-mark">◈</span>
        <div>
          <div class="logo-title">PlotSystem</div>
          <div class="logo-sub">剧情推演</div>
        </div>
      </div>
      <nav>
        <RouterLink to="/" class="nav-item">📚 工作台</RouterLink>
        <RouterLink
          :to="projectId ? `/director/${projectId}` : '/'"
          class="nav-item"
          :class="{ disabled: !projectId }"
        >🎬 导演视角</RouterLink>
        <RouterLink
          :to="projectId ? `/output/${projectId}` : '/'"
          class="nav-item"
          :class="{ disabled: !projectId }"
        >📝 输出导出</RouterLink>
      </nav>
      <div class="sidebar-footer dim">v0.1.0 · MIT</div>
    </aside>
    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100%;
}
.sidebar {
  width: 220px;
  background: #12152b;
  border-right: 1px solid var(--border);
  padding: 20px 14px;
  display: flex;
  flex-direction: column;
}
.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 28px;
}
.logo-mark {
  font-size: 28px;
  color: var(--highlight);
}
.logo-title {
  font-size: 18px;
  font-weight: 700;
}
.logo-sub {
  font-size: 12px;
  color: var(--text-dim);
}
nav {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: 1;
}
.nav-item {
  color: var(--text);
  padding: 10px 12px;
  border-radius: 8px;
  transition: background 0.15s;
}
.nav-item:hover {
  background: var(--accent);
}
.nav-item.router-link-active {
  background: var(--accent);
  color: #fff;
}
.nav-item.disabled {
  opacity: 0.4;
  pointer-events: none;
}
.sidebar-footer {
  font-size: 12px;
  text-align: center;
}
.content {
  flex: 1;
  overflow: auto;
  padding: 24px;
}
</style>
