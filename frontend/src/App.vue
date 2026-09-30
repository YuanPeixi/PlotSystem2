<script setup lang="ts">
import { computed } from 'vue'
import Icon from '@/components/ui/Icon.vue'
import SpectrumApp from '@/variants/SpectrumApp.vue'
import LiquidApp from '@/variants/LiquidApp.vue'
import GlassApp from '@/variants/GlassApp.vue'
import { applyUiStyle, uiStyle, type UiStyle } from '@/composables/uiStyle'

const styles = [
  { id: 'spectrum', label: 'Spectrum', description: '专业创作工作台' },
  { id: 'liquid', label: 'Liquid Glass', description: '轻盈的液态玻璃' },
  { id: 'glass', label: 'Glassmorphism', description: '深色磨砂玻璃' },
] as const
const app = computed(() => ({ spectrum: SpectrumApp, liquid: LiquidApp, glass: GlassApp })[uiStyle.value])
function selectStyle(style: UiStyle) {
  applyUiStyle(style)
}
</script>

<template>
  <component :is="app" :key="uiStyle" />
  <div class="style-switcher">
    <Icon name="compare" :size="17" />
    <label for="ui-style">界面风格</label>
    <select id="ui-style" :value="uiStyle" @change="selectStyle(($event.target as HTMLSelectElement).value as UiStyle)">
      <option v-for="option in styles" :key="option.id" :value="option.id">
        {{ option.label }} · {{ option.description }}
      </option>
    </select>
  </div>
</template>

<style scoped>
.style-switcher { position: fixed; right: 18px; bottom: 18px; z-index: 100; display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 10px 0 14px; border: 1px solid var(--line-strong); border-radius: 24px; background: var(--panel); color: var(--ink); box-shadow: var(--shadow-float); font: 13px/1.4 var(--font-ui); }
.style-switcher label { margin: 0; color: var(--ink); font-size: 12px; font-weight: 600; }
.style-switcher select { width: auto; min-width: 132px; height: 34px; padding: 4px 26px 4px 8px; border: 0; background-color: var(--panel-2); color: var(--ink); font-size: 12px; }
@media (max-width: 600px) { .style-switcher { right: 10px; bottom: 10px; padding-left: 10px; } .style-switcher label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); } .style-switcher select { min-width: 116px; } }
</style>
