<template>
  <div v-if="notice.text" class="alert" :class="'tone-' + (notice.tone || 'info')"
       role="status" aria-live="polite">
    <div class="alert-head">
      <span>{{ notice.text }}</span>
      <span class="notice-actions">
        <slot />
        <button class="ghost small" aria-label="关闭提示" @click="clear">×</button>
      </span>
    </div>
    <p v-if="notice.hint" class="alert-hint">{{ notice.hint }}</p>
    <p v-if="notice.requestId" class="alert-meta">请求 ID：{{ notice.requestId }}</p>
  </div>
</template>
<script setup>
import { reactive, watch, onUnmounted } from 'vue'

const notice = reactive({ text: '', hint: '', tone: 'info', requestId: '' })
let timer = null

function show(text, tone = 'info', hint = '', autoHideMs = 0, requestId = '') {
  notice.text = text; notice.tone = tone; notice.hint = hint || ''
  notice.requestId = requestId || ''
  if (timer) { clearTimeout(timer); timer = null }
  if (autoHideMs > 0) timer = setTimeout(clear, autoHideMs)
}
function ok(text, hint = '') { show(text, 'ok', hint, 5000) }
function err(error) {
  const message = error && error.message ? error.message : String(error || '操作失败')
  const hint = error && error.hint ? error.hint : ''
  const requestId = error && error.requestId ? error.requestId : ''
  show(message, 'err', hint, 0, requestId)   // 失败常驻，必须由用户关闭
}
function warn(text, hint = '') { show(text, 'warn', hint, 8000) }
function clear() { notice.text = ''; notice.hint = '' }
onUnmounted(() => { if (timer) clearTimeout(timer) })

defineExpose({ show, ok, err, warn, clear, notice })
</script>
