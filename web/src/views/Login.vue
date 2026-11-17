<template>
  <div class="center">
    <form class="card" @submit.prevent="submit">
      <h1>frpc 管理端</h1>
      <label>用户名<input v-model="username" autocomplete="username" /></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" /></label>
      <p v-if="error" class="err">{{ error }}</p>
      <button :disabled="busy">{{ busy ? '登录中…' : '登录' }}</button>
    </form>
  </div>
</template>
<script setup>
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
const router = useRouter(); const route = useRoute()
const username = ref(''); const password = ref(''); const error = ref(''); const busy = ref(false)
async function submit() {
  busy.value = true; error.value = ''
  try {
    const res = await fetch('./api/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.value, password: password.value }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok || !data.ok) { error.value = data.error || '登录失败'; return }
    router.replace(route.query.next || '/overview')
  } catch (e) { error.value = '网络错误：' + e.message }
  finally { busy.value = false }
}
</script>
