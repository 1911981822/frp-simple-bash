<template>
  <div v-if="open" class="mask" @click.self="onMask">
    <div ref="dialogEl" class="dialog" role="dialog" aria-modal="true"
         :aria-labelledby="titleId" tabindex="-1" @keydown="onKeydown">
      <h3 :id="titleId">{{ title }}</h3>
      <p v-if="message">{{ message }}</p>
      <ul v-if="items && items.length" class="dialog-list">
        <li v-for="(it, i) in items" :key="i">{{ it }}</li>
      </ul>
      <label v-if="requireText" class="dialog-require">
        请输入 <code>{{ requireText }}</code> 以确认
        <input ref="input" v-model="typed" autocomplete="off" />
      </label>
      <p v-if="error" class="err" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button class="ghost" :disabled="busy" @click="cancel">取消</button>
        <button v-if="extraLabel" class="ghost" :disabled="busy" @click="$emit('extra')">
          {{ extraLabel }}
        </button>
        <button :class="danger ? 'danger' : ''" :disabled="busy || !confirmed" @click="confirm">
          {{ busy ? '处理中…' : confirmLabel }}
        </button>
      </div>
    </div>
  </div>
</template>
<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
const props = defineProps({
  open: Boolean, title: String, message: String, items: Array,
  confirmLabel: { type: String, default: '确认' },
  extraLabel: { type: String, default: '' },
  danger: Boolean, busy: Boolean, requireText: { type: String, default: '' },
  error: { type: String, default: '' },
})
const emit = defineEmits(['confirm', 'cancel', 'extra'])
const typed = ref('')
const input = ref(null)
const dialogEl = ref(null)
const titleId = 'dlg-' + Math.random().toString(36).slice(2, 9)
let lastFocus = null
const confirmed = computed(() => !props.requireText || typed.value === props.requireText)

watch(() => props.open, async (open) => {
  if (open) {
    lastFocus = document.activeElement
    typed.value = ''
    await nextTick()
    if (props.requireText && input.value) input.value.focus()
    else if (dialogEl.value) dialogEl.value.focus()
  } else if (lastFocus && lastFocus.focus) {
    lastFocus.focus()                 // 关闭后把焦点还给触发按钮
    lastFocus = null
  }
})

function onKeydown(ev) {
  if (ev.key === 'Escape') {
    // busy 时禁止关闭：避免请求进行中弹层消失、失败原因看不到
    if (!props.busy) { ev.preventDefault(); cancel() }
    return
  }
  if (ev.key !== 'Tab' || !dialogEl.value) return
  const focusable = dialogEl.value.querySelectorAll(
    'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])')
  if (!focusable.length) return
  const first = focusable[0]; const last = focusable[focusable.length - 1]
  if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus() }
  else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus() }
}
function confirm() { if (confirmed.value && !props.busy) emit('confirm') }
function cancel() { if (!props.busy) emit('cancel') }
function onMask() { if (!props.busy) emit('cancel') }

// 打开时锁滚动，避免背景跟着滚
watch(() => props.open, (open) => {
  if (typeof document === 'undefined') return
  document.body.style.overflow = open ? 'hidden' : ''
})
onUnmounted(() => { if (typeof document !== 'undefined') document.body.style.overflow = '' })
</script>
