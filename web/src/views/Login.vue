<template>
  <div class="center">
    <form class="card login" @submit.prevent="submit">
      <h1>frpc 管理端</h1>
      <p v-if="firstRun" class="alert tone-warn small">
        首次使用：密码在服务首次启动时生成，可用
        <code>sudo journalctl -u frp-simple-bash-web | grep 密码</code> 查看。
      </p>
      <label>用户名
        <input v-model="username" ref="userInput" autocomplete="username"
               autocapitalize="none" spellcheck="false" :disabled="busy" />
      </label>
      <label>密码
        <span class="pw">
          <input v-model="password" :type="showPw ? 'text' : 'password'"
                 autocomplete="current-password" :disabled="busy" />
          <button type="button" class="ghost small pw-toggle"
                  @click="showPw = !showPw">{{ showPw ? '隐藏' : '显示' }}</button>
        </span>
      </label>
      <p v-if="capsLock" class="muted small">大写锁定已开启</p>

      <div v-if="error" class="alert tone-err" role="alert" aria-live="assertive">
        <div class="alert-head">
          <span>{{ error.message }}</span>
          <button v-if="error.kind === 'network'" type="button" class="ghost small"
                  @click="submit">重试</button>
        </div>
        <p v-if="error.hint" class="alert-hint">{{ error.hint }}</p>
        <p v-if="lockLeft > 0" class="alert-meta">将在 {{ hhmmss(lockLeft) }} 后可重试</p>
      </div>

      <button type="submit" :disabled="busy || lockLeft > 0">
        {{ busy ? '登录中…' : (lockLeft > 0 ? '已锁定 ' + hhmmss(lockLeft) : '登录') }}
      </button>

      <details class="help">
        <summary>首次使用 / 忘记密码？</summary>
        <p>忘记密码：在服务器终端执行</p>
        <code class="block">sudo frpc web --set-password</code>
        <p>修改端口/绑定地址：</p>
        <code class="block">sudo frpc web --set-port 7501 --set-bind 127.0.0.1</code>
        <p>若被锁定：等待倒计时，或重启服务</p>
        <code class="block">sudo systemctl restart frp-simple-bash-web</code>
      </details>
    </form>
  </div>
</template>
<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { post, get, setSession, ApiError } from '../lib/api.js'
import { hhmmss } from '../lib/format.js'

const router = useRouter(); const route = useRoute()
const username = ref(''); const password = ref('')
const error = ref(null); const busy = ref(false); const showPw = ref(false)
const capsLock = ref(false); const userInput = ref(null); const firstRun = ref(false)
const lockLeft = ref(0)
let lockTimer = null

function startLock(seconds) {
  lockLeft.value = Math.max(0, Math.floor(seconds || 0))
  if (lockTimer) clearInterval(lockTimer)
  if (lockLeft.value <= 0) return
  lockTimer = setInterval(() => {
    lockLeft.value -= 1
    if (lockLeft.value <= 0) { clearInterval(lockTimer); lockTimer = null }
  }, 1000)
}

function onKey(ev) {
  if (typeof ev.getModifierState === 'function') capsLock.value = ev.getModifierState('CapsLock')
}

async function submit() {
  if (busy.value || lockLeft.value > 0) return
  busy.value = true; error.value = null
  try {
    const data = await post('/api/login', { username: username.value, password: password.value })
    setSession(data)
    const next = String(route.query.next || '/overview')
    // 只允许站内相对路径，避免开放重定向
    router.replace(next.startsWith('/') && !next.startsWith('//') ? next : '/overview')
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 0) error.value = { kind: 'network', message: err.message, hint: err.hint }
      else if (err.status === 401) error.value = { kind: 'auth', message: err.message, hint: err.hint }
      else if (err.status === 429) {
        error.value = { kind: 'locked', message: err.message, hint: err.hint }
        startLock(err.retryAfter || 900)
      } else error.value = { kind: 'server', message: err.message, hint: err.hint }
    } else error.value = { kind: 'network', message: '登录失败', hint: String(err) }
  } finally { busy.value = false }
}

onMounted(async () => {
  window.addEventListener('keyup', onKey)
  try {
    const health = await get('/api/health')
    firstRun.value = !!health.first_run
  } catch (e) { /* 不影响登录 */ }
  if (userInput.value) userInput.value.focus()
})
onUnmounted(() => {
  window.removeEventListener('keyup', onKey)
  if (lockTimer) clearInterval(lockTimer)
})
</script>
