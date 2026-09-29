import { createRouter, createWebHistory } from 'vue-router'
import Workspace from '@/pages/Workspace.vue'
import Director from '@/pages/Director.vue'
import BranchMap from '@/pages/BranchMap.vue'
import Output from '@/pages/Output.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'workspace', component: Workspace },
    { path: '/director/:projectId', name: 'director', component: Director, props: true },
    { path: '/branches/:projectId', name: 'branches', component: BranchMap, props: true },
    { path: '/output/:projectId', name: 'output', component: Output, props: true },
  ],
})

export default router
