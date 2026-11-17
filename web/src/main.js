import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './style.css'
import { get, setSession, setUnauthorizedHandler, clearSession } from './lib/api.js'

const routes = [
  { path: '/', redirect: '/overview' },
  { path: '/login', component: () => import('./views/Login.vue'), meta: { public: true, title: '登录' } },
  { path: '/overview', component: () => import('./views/Overview.vue'), meta: { title: '概览' } },
  { path: '/mappings', component: () => import('./views/Mappings.vue'), meta: { title: '端口映射' } },
  { path: '/profiles', component: () => import('./views/Profiles.vue'), meta: { title: '服务端' } },
  { path: '/logs', component: () => import('./views/Logs.vue'), meta: { title: '日志与诊断' } },
  { path: '/diagnose', component: () => import('./views/Diagnose.vue'), meta: { title: '连接诊断' } },
  { path: '/settings', component: () => import('./views/Settings.vue'), meta: { title: '设置' } },
  // 兜底：未知路径回概览，避免刷新/后退时白屏
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

const router = createRouter({ history: createWebHashHistory(), routes })

let sessionChecked = false

setUnauthorizedHandler(() => {
  clearSession()
  sessionChecked = false
  if (router.currentRoute.value.path !== '/login') {
    router.replace({ path: '/login', query: { next: router.currentRoute.value.fullPath } })
  }
})

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  if (sessionChecked) return true
  try {
    const data = await get('/api/session')
    // 必须确认返回的是 JSON 信封（子路径/反代错误时可能拿到 HTML 200）
    if (data && data.username) { setSession(data); sessionChecked = true; return true }
  } catch (e) { /* 落到登录页 */ }
  return { path: '/login', query: { next: to.fullPath } }
})

router.afterEach((to) => {
  document.title = (to.meta.title ? to.meta.title + ' · ' : '') + 'frpc 管理端'
})

createApp(App).use(router).mount('#app')
