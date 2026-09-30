import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
// 对白与输出用的衬线字体：自托管、按 unicode-range 分片，只下载页面上出现过的字
import '@fontsource/noto-serif-sc/400.css'
import '@fontsource/noto-serif-sc/600.css'
import './styles/global.css'
import './styles/spectrum.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
