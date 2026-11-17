<template>
  <a class="skip" href="#main">跳到主内容</a>
  <div class="layout" :class="{ 'is-login': isLogin }">
    <aside v-if="!isLogin" class="side">
      <div class="brand">frpc<span class="brand-sub">管理端</span></div>
      <nav>
        <router-link v-for="item in nav" :key="item.path" :to="item.path">{{ item.label }}</router-link>
      </nav>
      <div class="side-foot">
        <div class="user">{{ who || '—' }}</div>
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
import { post, session, clearSession, copyText } from './lib/api.js'
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
let timer = null

async function logout() {
  try { await post('/api/logout') } catch (e) { /* 忽略 */ }
  clearSession()
  router.replace('/login')
}

onMounted(() => {
  timer = setInterval(() => {
    if (session().expiresIn > 0) left.value = Math.max(0, left.value - 1 || session().expiresIn)
  }, 1000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>
