<template>
  <div class="alert tone-err">
    <div class="alert-head">
      <span>⚠ {{ error.message || '请求失败' }}</span>
      <button v-if="retry" class="ghost small" @click="$emit('retry')">重试</button>
    </div>
    <p v-if="error.hint" class="alert-hint">{{ error.hint }}</p>
    <p v-if="error.requestId" class="alert-meta">请求 ID：{{ error.requestId }}</p>
  </div>
</template>
<script setup>
defineProps({ error: { type: Object, required: true }, retry: { type: Function, default: null } })
defineEmits(['retry'])
</script>
