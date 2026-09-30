import { ref } from 'vue'
import spectrumUrl from '@/styles/spectrum.css?url'
import liquidUrl from '@/styles/liquid-glass.css?url'
import glassUrl from '@/styles/glassmorphism.css?url'

export type UiStyle = 'spectrum' | 'liquid' | 'glass'

const STORAGE_KEY = 'plotsystem.uiStyle'
const urls: Record<UiStyle, string> = { spectrum: spectrumUrl, liquid: liquidUrl, glass: glassUrl }

function initialStyle(): UiStyle {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'spectrum' || stored === 'liquid' || stored === 'glass') return stored
  } catch {
    // Storage may be unavailable; the current session can still switch styles.
  }
  return 'spectrum'
}

export const uiStyle = ref<UiStyle>(initialStyle())

export function applyUiStyle(style: UiStyle) {
  uiStyle.value = style
  document.documentElement.dataset.uiStyle = style
  let link = document.querySelector<HTMLLinkElement>('link[data-ui-style]')
  if (!link) {
    link = document.createElement('link')
    link.rel = 'stylesheet'
    link.dataset.uiStyle = ''
    document.head.appendChild(link)
  }
  link.href = urls[style]
  try {
    localStorage.setItem(STORAGE_KEY, style)
  } catch {
    // The style stays active until this page is closed.
  }
}

applyUiStyle(uiStyle.value)
