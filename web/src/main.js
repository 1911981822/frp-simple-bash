import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './style.css'
import Login from './views/Login.vue'
import Overview from './views/Overview.vue'
import Mappings from './views/Mappings.vue'
import Profiles from './views/Profiles.vue'
import Logs from './views/Logs.vue'
import Diagnose from './views/Diagnose.vue'
import Backups from './views/Backups.vue'
import Settings from './views/Settings.vue'

const routes = [
  { path: '/', redirect: '/overview' },
  { path: '/login', component: Login, meta: { public: true } },
  { path: '/overview', component: Overview },
  { path: '/mappings', component: Mappings },
  { path: '/profiles', component: Profiles },
  { path: '/logs', component: Logs },
  { path: '/diagnose', component: Diagnose },
  { path: '/backups', component: Backups },
  { path: '/settings', component: Settings },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  try {
    const res = await fetch('./api/session', { credentials: 'same-origin' })
    if (res.ok) return true
  } catch (e) { /* 网络异常也回登录页 */ }
  return { path: '/login', query: { next: to.fullPath } }
})

createApp(App).use(router).mount('#app')
