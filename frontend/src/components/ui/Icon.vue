<script setup lang="ts">
import { computed } from 'vue'
import { ICONS, type IconName } from './icons'

const props = withDefaults(defineProps<{ name: IconName; size?: number }>(), { size: 18 })
const markup = computed(() => ICONS[props.name])
</script>

<template>
  <!-- 纯装饰：可访问名称由外层按钮的 title / aria-label 提供 -->
  <svg
    class="icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="markup"
  />
</template>

<style scoped>
.icon {
  flex: none;
  display: inline-block;
  vertical-align: -0.2em;
}
</style>
