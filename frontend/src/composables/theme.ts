import { ref, watch } from 'vue'
import { uiStyle } from './uiStyle'

export type Theme = 'light' | 'dark'

function key() {
  return `plotsystem.${uiStyle.value}.theme`
}

function initial(): Theme {
  try {
    const stored = localStorage.getItem(key())
    if (stored === 'light' || stored === 'dark') return stored
  } catch {
    // Use the selected style's default.
  }
  return uiStyle.value === 'glass' ? 'dark' : 'light'
}

/** 每种风格独立保存明暗偏好。 */
export const theme = ref<Theme>(initial())
document.documentElement.dataset.theme = theme.value

export function applyTheme(t: Theme) {
  theme.value = t
  document.documentElement.dataset.theme = t
  try {
    localStorage.setItem(key(), t)
  } catch {
    // 隐私模式下写不进去就只在本次会话生效
  }
}

export function toggleTheme() {
  applyTheme(theme.value === 'dark' ? 'light' : 'dark')
}

watch(uiStyle, () => {
  theme.value = initial()
  document.documentElement.dataset.theme = theme.value
})

/** G6 这类在 JS 里配色的库读不到 CSS 变量，渲染前按当前主题取一次计算值。 */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
