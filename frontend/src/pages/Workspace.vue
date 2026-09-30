<script setup lang="ts">
import { computed } from 'vue'
import { uiStyle } from '@/composables/uiStyle'
import SpectrumWorkspace from '@/variants/SpectrumWorkspace.vue'
import LiquidWorkspace from '@/variants/LiquidWorkspace.vue'
import GlassWorkspace from '@/variants/GlassWorkspace.vue'

const workspace = computed(() => ({
  spectrum: SpectrumWorkspace,
  liquid: LiquidWorkspace,
  glass: GlassWorkspace,
})[uiStyle.value])
</script>

<template>
  <component :is="workspace" :key="uiStyle" />
</template>
