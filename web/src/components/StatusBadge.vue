<template>
  <span class="badge" :class="'tone-' + tone">
    <span class="dot" aria-hidden="true"></span>{{ label }}
  </span>
</template>
<script setup>
defineProps({ label: { type: String, default: '' }, tone: { type: String, default: 'muted' } })
</script>
