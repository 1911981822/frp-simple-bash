<template>
  <div class="page">
    <header class="page-head">
      <div class="tabs">
        <span class="tab active">实时日志</span>
        <router-link class="tab" to="/diagnose">连接诊断</router-link>
      </div>
      <div class="page-tools">
        <input v-model.trim="keyword" class="log-search" aria-label="搜索日志关键字"
               placeholder="搜索关键字" />
        <select v-model="level" aria-label="日志级别筛选">
          <option value="">全部级别</option>
          <option value="error">只看错误</option>
          <option value="warn">错误+警告</option>
          <option value="info">错误+警告+信息</option>
        </select>
        <button class="ghost small" @click="toggle">{{ running ? '暂停' : '继续' }}</button>
        <button class="ghost small" @click="lines = []">清屏</button>
        <button class="ghost small" :disabled="!exists" @click="download">下载</button>
      </div>
    </header>
    <p v-if="streamError" class="alert tone-warn small">
      {{ streamError }}
      <button class="ghost small" @click="reconnect">重新连接</button>
    </p>
    <div class="logbox" ref="box" role="log" aria-label="frpc 实时日志" tabindex="0">
      <p v-if="!lines.length" class="muted">暂无日志；服务可能未启动，或日志已轮转。</p>
      <div v-for="(l, i) in shown" :key="i" class="logline" :class="lineTone(l)">{{ l }}</div>
    </div>
    <p class="muted small">显示最近 {{ shown.length }} 行（缓冲区上限 5000 行，超出自动丢弃最旧的）</p>
  </div>
</template>
<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { get, endpoint } from '../lib/api.js'

const lines = ref([]); const running = ref(true); const level = ref('')
const keyword = ref('')
const box = ref(null); const streamError = ref(''); const exists = ref(false)
let es = null; let errCount = 0; let hidden = false

function matchLevel(line) {
  if (!level.value) return true
  if (level.value === 'error') return /error|fatal|panic/i.test(line)
  if (level.value === 'warn') return /error|fatal|panic|warn/i.test(line)
  return /error|fatal|panic|warn|info/i.test(line)
}

const shown = computed(() => {
  const kw = keyword.value.toLowerCase()
  return lines.value
    .filter(matchLevel)
    .filter(l => !kw || l.toLowerCase().includes(kw))
    .slice(-500)
})

function lineTone(line) {
  if (/error|fatal|panic/i.test(line)) return 'tone-err'
  if (/warn/i.test(line)) return 'tone-warn'
  return ''
}

async function loadInitial() {
  try {
    const data = await get('/api/logs?lines=200')
    exists.value = !!data.exists
    // 首屏内容由 SSE 首推补齐；这里只在没有 SSE 时兜底
    if (!es) lines.value = data.lines || []
  } catch (e) { streamError.value = '读取日志失败：' + e.message }
}

function connect() {
  if (es) { es.close(); es = null }
  // 用 SSE 首推作为唯一来源：不再和 /api/logs 的 200 行叠加，
  // 否则最近 100 行会重复显示两遍。
  lines.value = []
  try {
    es = new EventSource(endpoint('/api/logs/stream'))
    es.addEventListener('log', ev => {
      if (hidden) return
      const payload = JSON.parse(ev.data)
      lines.value.push(payload.line)
      if (lines.value.length > 5000) lines.value = lines.value.slice(-4000)
      scroll()
    })
    es.addEventListener('reset', () => { lines.value = [] })
    es.onerror = async () => {
      streamError.value = '实时连接已断开，正在重连…'
      if (++errCount < 3) return
      errCount = 0
      // EventSource 拿不到 401，主动探测一次会话；过期会由全局处理器跳登录
      try { await get('/api/session') } catch (e) { reconnect() }
    }
    es.onopen = () => { streamError.value = ''; errCount = 0; exists.value = true }
  } catch (e) { streamError.value = '无法建立实时连接：' + e.message }
}

function toggle() { running.value = !running.value; if (running.value) scroll() }

function reconnect() {
  streamError.value = ''
  connect()
}

function download() {
  // 同源 GET，浏览器自动带 Cookie
  const link = document.createElement('a')
  link.href = endpoint('/api/logs/download')
  link.download = 'frpc.log'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

function scroll() {
  nextTick(() => {
    if (box.value && running.value && !hidden) box.value.scrollTop = box.value.scrollHeight
  })
}

function onVisible() {
  hidden = document.hidden
  if (!hidden) scroll()
}

onMounted(async () => {
  await loadInitial()
  connect()
  scroll()
  document.addEventListener('visibilitychange', onVisible)
})
onUnmounted(() => {
  if (es) es.close()
  document.removeEventListener('visibilitychange', onVisible)
})
</script>
<style scoped>
.log-search { width: 180px; min-width: 120px; }
</style>
