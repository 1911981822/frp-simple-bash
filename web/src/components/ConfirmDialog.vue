<template>
  <div v-if="open" class="mask" @click.self="cancel">
    <div class="dialog" role="dialog" aria-modal="true">
      <h3>{{ title }}</h3>
      <p v-if="message">{{ message }}</p>
      <ul v-if="items && items.length" class="dialog-list">
        <li v-for="(it, i) in items" :key="i">{{ it }}</li>
      </ul>
      <label v-if="requireText" class="dialog-require">
        请输入 <code>{{ requireText }}</code> 以确认
        <input ref="input" v-model="typed" autocomplete="off" />
      </label>
      <p v-if="error" class="err">{{ error }}</p>
      <div class="dialog-actions">
        <button class="ghost" @click="cancel">取消</button>
        <button :class="danger ? 'danger' : ''" :disabled="busy || !confirmed" @click="confirm">
          {{ busy ? '处理中…' : confirmLabel }}
        </button>
      </div>
    </div>
  </div>
</template>
<script setup>
import { computed, nextTick, ref, watch } from 'vue'
const props = defineProps({
  open: Boolean, title: String, message: String, items: Array,
  confirmLabel: { type: String, default: '确认' },
  danger: Boolean, busy: Boolean, requireText: { type: String, default: '' },
  error: { type: String, default: '' },
})
const emit = defineEmits(['confirm', 'cancel'])
const typed = ref('')
const input = ref(null)
const confirmed = computed(() => !props.requireText || typed.value === props.requireText)
watch(() => props.open, async (v) => {
  if (v) { typed.value = ''; await nextTick(); if (props.requireText && input.value) input.value.focus() }
})
function confirm() { if (confirmed.value && !props.busy) emit('confirm') }
function cancel() { if (!props.busy) emit('cancel') }
</script>
