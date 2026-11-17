<template>
  <div class="page">
    <header class="page-head">
      <div class="tabs">
        <span class="tab active">实时日志</span>
        <router-link class="tab" to="/diagnose">连接诊断</router-link>
      </div>
      <div class="page-tools">
        <select v-model="level" class="small">
          <option value="">全部级别</option>
          <option value="error">只看错误</option>
          <option value="warn">错误+警告</option>
        </select>
        <button class="ghost small" @click="toggle">{{ running ? '暂停' : '继续' }}</button>
        <button class="ghost small" @click="lines = []">清屏</button>
      </div>
    </header>
    <p v-if="streamError" class="alert tone-err small">{{ streamError }}</p>
    <div class="logbox" ref="box">
      <p v-if="!lines.length" class="muted">暂无日志；服务未启动或日志已轮转。</p>
      <div v-for="(l, i) in shown" :key="i" class="logline" :class="lineTone(l)">{{ l }}</div>
    </div>
  </div>
</template>
<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { get } from '../lib/api.js'

const lines = ref([]); const running = ref(true); const level = ref('')
const box = ref(null); const streamError = ref('')
let source = null; let es = null

const shown = computed(() => {
  if (!level.value) return lines.value.slice(-500)
  if (level.value === 'error') return lines.value.filter(l => /error|fatal|panic/i.test(l)).slice(-500)
  return lines.value.filter(l => /error|fatal|panic|warn/i.test(l)).slice(-500)
})

function lineTone(line) {
  if (/error|fatal|panic/i.test(line)) return 'tone-err'
  if (/warn/i.test(line)) return 'tone-warn'
  return ''
}

async function loadInitial() {
  try {
    const data = await get('/api/logs?lines=200')
    lines.value = data.lines || []
  } catch (e) { streamError.value = '读取日志失败：' + e.message }
}

function scroll() {
  nextTick(() => { if (box.value && running.value) box.value.scrollTop = box.value.scrollHeight })
}

function toggle() { running.value = !running.value; if (running.value) scroll() }

onMounted(async () => {
  await loadInitial(); scroll()
  // SSE 实时尾随（浏览器原生 EventSource，自动重连）
  try {
    es = new EventSource(new URL('api/logs/stream', document.baseURI.replace(/[^/]*$/, '')).toString())
    es.addEventListener('log', ev => {
      const payload = JSON.parse(ev.data)
      if (!running.value) return
      lines.value.push(payload.line)
      if (lines.value.length > 5000) lines.value = lines.value.slice(-4000)
      scroll()
    })
    es.addEventListener('reset', () => { lines.value = [] })
    es.onerror = () => { streamError.value = '实时连接已断开，正在重连…' }
    es.onopen = () => { streamError.value = '' }
  } catch (e) { streamError.value = '无法建立实时连接：' + e.message }
})
onUnmounted(() => { if (es) es.close(); if (source) source.close() })
</script>
