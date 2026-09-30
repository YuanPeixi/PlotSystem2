import { ref } from 'vue'

export type Theme = 'light' | 'dark'

const KEY = 'plotsystem.spectrum.theme'

function initial(): Theme {
  try {
    return localStorage.getItem(KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

/** 亮色为默认；选择持久化在本机。index.html 里有同逻辑的内联脚本，避免首屏闪白/闪黑。 */
export const theme = ref<Theme>(initial())

export function applyTheme(t: Theme) {
  theme.value = t
  document.documentElement.dataset.theme = t
  try {
    localStorage.setItem(KEY, t)
  } catch {
    // 隐私模式下写不进去就只在本次会话生效
  }
}

export function toggleTheme() {
  applyTheme(theme.value === 'dark' ? 'light' : 'dark')
}

/** G6 这类在 JS 里配色的库读不到 CSS 变量，渲染前按当前主题取一次计算值。 */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
