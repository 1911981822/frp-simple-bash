<template>
  <a class="skip" href="#main">跳到主内容</a>
  <div class="layout" :class="{ 'is-login': isLogin }">
    <aside v-if="!isLogin" class="side">
      <div class="brand">frpc<span class="brand-sub">管理端</span></div>
      <nav>
        <router-link v-for="item in nav" :key="item.path" :to="item.path">{{ item.label }}</router-link>
      </nav>
      <div class="side-foot">
        <div class="user">
          <span class="status-dot" :class="'tone-' + globalTone" aria-hidden="true"></span>
          {{ who || '—' }}
        </div>
        <div class="muted small" v-if="left > 0">会话剩余 {{ hhmmss(left) }}</div>
        <button class="ghost small" @click="logout">退出登录</button>
      </div>
    </aside>
    <main id="main" class="main"><router-view /></main>
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { get, post, session, clearSession } from './lib/api.js'
import { hhmmss } from './lib/format.js'

const route = useRoute(); const router = useRouter()
const isLogin = computed(() => route.path === '/login')
const nav = [
  { path: '/overview', label: '概览' },
  { path: '/mappings', label: '端口映射' },
  { path: '/profiles', label: '服务端（FRPS）' },
  { path: '/logs', label: '日志与诊断' },
  { path: '/settings', label: '设置' },
]
const who = computed(() => session().username)
const left = ref(0)
const globalState = ref('')
const timer = null || null
let tick = null
const globalTone = computed(() => {
  const state = String(globalState.value || '').toLowerCase()
  if (['running', 'active', 'started'].includes(state)) return 'ok'
  if (['failed', 'stopped', 'inactive', 'dead'].includes(state)) return 'err'
  return 'muted'
})

async function logout() {
  try { await post('/api/logout') } catch (e) { /* 忽略 */ }
  clearSession()
  router.replace('/login')
}

onMounted(() => {
  // 用服务端下发的过期时间初始化，然后本地逐秒递减。
  // 原实现 `left-1 || expiresIn` 在 left=0 时得到 -1（真值），永远走不到赋值分支。
  left.value = session().expiresIn || 0
  tick = setInterval(() => { if (left.value > 0) left.value -= 1 }, 1000)
  refreshGlobalState()
  stateTimer = setInterval(refreshGlobalState, 15000)
})
onUnmounted(() => {
  if (tick) clearInterval(tick)
  if (stateTimer) clearInterval(stateTimer)
})

let stateTimer = null
async function refreshGlobalState() {
  try { globalState.value = (await get('/api/status')).service_state || '' }
  catch (e) { /* 未登录或后端不可用时不打扰 */ }
}
</script>
