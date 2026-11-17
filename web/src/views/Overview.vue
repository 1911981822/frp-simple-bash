<template>
  <div class="page">
    <header class="page-head">
      <h2>概览</h2>
      <div class="page-tools">
        <span class="muted small">更新于 {{ updatedAt }}</span>
        <button class="ghost small" :disabled="loading" @click="load">刷新</button>
      </div>
    </header>

    <ErrorState v-if="error" :error="error" :retry="load" />

    <template v-if="data">
      <!-- 状态带 + 服务控制 -->
      <section class="band">
        <div class="band-left">
          <StatusBadge :label="svc.label" :tone="svc.tone" />
          <StatusBadge :label="conn.label" :tone="conn.tone" />
          <span class="band-profile" v-if="data.active_profile">
            {{ data.active_profile.name }}
            <span class="muted">（{{ data.active_profile.server_addr }}:{{ data.active_profile.server_port }}）</span>
          </span>
          <span v-else class="muted">未选择服务端</span>
        </div>
        <div class="band-actions">
          <button v-if="!isRunning" :disabled="busy" @click="service('start')">启动</button>
          <button v-else class="ghost" :disabled="busy" @click="askStop">停止</button>
          <button class="ghost" :disabled="busy" @click="askRestart">重启</button>
        </div>
      </section>

      <!-- 4 个指标 -->
      <section class="stats">
        <div class="stat"><div class="stat-num">{{ data.counts.total }}</div><div class="stat-label">映射总数</div></div>
        <div class="stat"><div class="stat-num">{{ data.counts.enabled }}</div><div class="stat-label">已启用</div></div>
        <div class="stat">
          <div class="stat-num" :class="{ muted: data.counts.online === null }">
            {{ data.counts.online === null ? '—' : data.counts.online }}
          </div>
          <div class="stat-label">在线{{ data.counts.online === null ? '（不可用）' : '' }}</div>
        </div>
        <div class="stat">
          <div class="stat-num" :class="data.pending ? 'warn' : ''">{{ data.pending ? '1' : '0' }}</div>
          <div class="stat-label">待重启</div>
        </div>
      </section>

      <!-- 需关注 -->
      <div v-for="(c, i) in data.concerns" :key="i" class="alert" :class="'tone-' + (c.level === 'err' ? 'err' : 'warn')">
        <div class="alert-head"><span>{{ c.text }}</span></div>
      </div>

      <div v-if="data.pending" class="alert tone-warn">
        <div class="alert-head">
          <span>有配置修改尚未生效，需要重启 frpc</span>
          <button class="ghost small" :disabled="busy" @click="askRestart">立即重启</button>
        </div>
      </div>

      <div v-if="data.mappings_error" class="alert tone-err">
        <div class="alert-head"><span>映射读取失败：{{ data.mappings_error }}</span>
          <router-link class="ghost small" to="/settings">去设置页恢复备份</router-link>
        </div>
      </div>

      <!-- 面板访问信息 -->
      <section class="card2">
        <h3>管理端地址</h3>
        <div class="kv">
          <span>当前地址</span>
          <code>{{ panelUrl }}</code>
          <button class="ghost small" @click="copy(panelUrl)">{{ copied === panelUrl ? '已复制 ✓' : '复制' }}</button>
        </div>
        <div class="kv" v-if="data.expose">
          <span>公网访问</span>
          <code>{{ data.expose.new }}</code>
          <span class="warn small">旧地址 {{ data.expose.old }} 将在 {{ data.expose.seconds_left }} 秒后关闭</span>
        </div>
        <p class="muted small">修改端口 / 绑定地址请在服务器终端执行 <code>sudo frpc web --set-port …</code></p>
      </section>

      <p class="muted small footer">
        frpc {{ data.version }} · 管理器 {{ data.manager_version }}
      </p>
    </template>

    <ConfirmDialog :open="dialog.open" :title="dialog.title" :message="dialog.message"
                   :items="dialog.items" :danger="dialog.danger" :busy="busy"
                   :confirm-label="dialog.confirmLabel" :error="dialog.error"
                   @confirm="dialog.onConfirm" @cancel="dialog.open = false" />
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { get, post, copyText, ApiError } from '../lib/api.js'
import { serviceView, connectionView } from '../lib/status.js'
import { nowText } from '../lib/format.js'
import StatusBadge from '../components/StatusBadge.vue'
import ErrorState from '../components/ErrorState.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const data = ref(null); const error = ref(null); const loading = ref(false)
const busy = ref(false); const updatedAt = ref(''); const copied = ref('')
const dialog = reactive({ open: false, title: '', message: '', items: [], danger: false,
                          confirmLabel: '确认', error: '', onConfirm: () => {} })
let timer = null

const svc = computed(() => data.value?.service_view || serviceView(data.value?.service_state))
const conn = computed(() => data.value?.connection_view || connectionView(data.value?.connection_state))
const isRunning = computed(() => ['running', 'active', 'started'].includes(String(data.value?.service_state).toLowerCase()))
const panelUrl = computed(() => location.origin + location.pathname)

async function load() {
  loading.value = true; error.value = null
  try {
    data.value = await get('/api/overview')
    updatedAt.value = nowText()
  } catch (err) {
    error.value = err instanceof ApiError ? err : new ApiError(0, { error: String(err) })
  } finally { loading.value = false }
}

async function service(action, mode) {
  busy.value = true; dialog.error = ''
  try {
    await post('/api/service/' + action, mode ? { mode } : {})
    dialog.open = false
    await load()
  } catch (err) {
    dialog.error = err.message + (err.hint ? '（' + err.hint + '）' : '')
  } finally { busy.value = false }
}

function askStop() {
  Object.assign(dialog, {
    open: true, title: '停止 frpc？', message: '停止后所有映射将立即失效，外部无法访问。',
    items: [], danger: true, confirmLabel: '停止', error: '',
    onConfirm: () => service('stop'),
  })
}

function askRestart() {
  const hasMappings = (data.value?.counts.enabled || 0) > 0
  Object.assign(dialog, {
    open: true, title: '重启 frpc？',
    message: hasMappings
      ? '重启会短暂中断所有映射。若你正通过映射端口 SSH 登录本机，请选择「后台延迟重启」。'
      : '重启会短暂中断服务。',
    items: [], danger: false, confirmLabel: '立即重启', error: '',
    onConfirm: () => service('restart', 'now'),
  })
}

async function copy(text) {
  if (await copyText(text)) {
    copied.value = text
    setTimeout(() => { if (copied.value === text) copied.value = '' }, 1500)
  }
}

onMounted(() => { load(); timer = setInterval(() => { if (document.visibilityState === 'visible') load() }, 10000) })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>
