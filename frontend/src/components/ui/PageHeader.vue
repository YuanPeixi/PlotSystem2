<script setup lang="ts">
defineProps<{ title: string; context?: string }>()
</script>

<template>
  <header class="page-header">
    <div class="crumbs">
      <template v-if="context">
        <span class="context">{{ context }}</span>
        <span class="sep">/</span>
      </template>
      <h1>{{ title }}</h1>
      <slot name="meta" />
    </div>
    <div class="actions">
      <slot />
    </div>
  </header>
</template>

<style scoped>
.page-header {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 58px;
  padding: 10px 24px;
  border-bottom: 1px solid var(--line);
  background: var(--material);
}
.crumbs {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}
.context {
  color: var(--ink-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 30%;
}
.sep {
  color: var(--ink-3);
}
h1 {
  font-size: 16px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
@media (max-width: 720px) {
  .page-header { flex-wrap: wrap; padding: 12px 16px; }
  .actions { flex-wrap: wrap; }
  .context,
  .sep {
    display: none;
  }
}
</style>
