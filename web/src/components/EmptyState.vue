<template>
  <div class="empty">
    <div class="empty-icon" aria-hidden="true">{{ icon }}</div>
    <h3>{{ title }}</h3>
    <p class="muted">{{ desc }}</p>
    <div class="empty-actions"><slot /></div>
  </div>
</template>
<script setup>
defineProps({
  icon: { type: String, default: '📭' },
  title: { type: String, default: '' },
  desc: { type: String, default: '' },
})
</script>
