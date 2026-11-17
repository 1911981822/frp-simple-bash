<template>
  <div class="page">
    <header class="page-head">
      <div class="tabs">
        <router-link class="tab" to="/logs">实时日志</router-link>
        <span class="tab active">连接诊断</span>
      </div>
      <div class="page-tools">
        <span v-if="updatedAt && !loading" class="muted small">更新于 {{ updatedAt }}</span>
        <button class="ghost" :disabled="loading" @click="run">
          {{ loading ? '诊断中…' : '重新诊断' }}
        </button>
      </div>
    </header>

    <ErrorState v-if="error" :error="error" :retry="run" @retry="run" />

    <p v-if="loading && !result" class="muted">正在执行诊断，请稍候…</p>

    <template v-if="result">
      <!-- 汇总行 -->
      <section class="band">
        <div class="band-left">
          <StatusBadge :label="summary.badge" :tone="summary.tone" />
          <span>{{ summary.text }}</span>
          <span v-if="loading" class="muted small">正在重新诊断…</span>
        </div>
        <div v-if="serviceBadge" class="band-left">
          <span class="muted small">服务状态</span>
          <StatusBadge :label="serviceBadge.label" :tone="serviceBadge.tone" />
        </div>
      </section>

      <!-- 诊断项：✓ 绿 / ✗ 红 -->
      <ul v-if="checks.length" class="diag-list">
        <li v-for="(item, i) in checks" :key="i" class="diag-item"
            :class="item.ok ? 'is-ok' : 'is-err'">
          <span class="diag-icon" aria-hidden="true">{{ item.ok ? '✓' : '✗' }}</span>
          <div class="diag-body">
            <div class="diag-head">
              <span class="diag-name">{{ item.label }}</span>
              <span class="diag-state">{{ item.ok ? '通过' : '异常' }}</span>
            </div>
            <p v-if="item.detail" class="diag-detail">{{ item.detail }}</p>
          </div>
        </li>
      </ul>

      <!-- 兜底：后端没有结构化诊断项时，用 raw 的文本行渲染 -->
      <div v-else class="card2 diag-raw">
        <h3>诊断明细</h3>
        <p v-if="!rawLines.length" class="muted">没有可显示的诊断项。</p>
        <p v-for="(line, i) in rawLines" :key="i" class="diag-raw-line">{{ line }}</p>
      </div>

      <p class="muted small footer">
        诊断在本机执行，不读取密码等敏感信息；如反复异常，请查看「日志与诊断」页面的服务日志。
      </p>
    </template>
  </div>
</template>
<script setup>
import { computed, onMounted, ref } from 'vue'
import { post, ApiError } from '../lib/api.js'
import { connectionView, serviceView } from '../lib/status.js'
import { nowText } from '../lib/format.js'
import StatusBadge from '../components/StatusBadge.vue'
import ErrorState from '../components/ErrorState.vue'

const result = ref(null)
const error = ref(null)
const loading = ref(false)
const updatedAt = ref('')

// 诊断项的 name 是后端英文标识，这里翻译成中文；未知项原样显示。
const CHECK_LABELS = {
  config: '配置校验',
  'server-port': '服务端端口',
  connection: '连接状态',
  'dashboard-port': 'Dashboard 端口',
  'dashboard-auth': 'Dashboard 认证',
}

function labelOf(name) {
  const key = String(name || '').trim()
  if (CHECK_LABELS[key]) return CHECK_LABELS[key]
  if (key.startsWith('firewall-')) return '防火墙检查（' + key.slice('firewall-'.length) + '）'
  return key || '检查项'
}

function detailOf(item) {
  const detail = item && item.detail
  if (detail === undefined || detail === null) return ''
  if (item.name === 'connection' && typeof detail === 'string') {
    const view = connectionView(detail)
    return view.label === detail ? detail : view.label + '（' + detail + '）'
  }
  return String(detail).trim()
}

// 后端 items 里每项为 {name, ok, detail}；这里只做展示层归一化。
const checks = computed(() => (result.value?.items || []).map((item) => {
  const ok = !!item && typeof item === 'object' && !!item.ok
  return { ok, label: labelOf(item?.name), detail: detailOf(item) }
}))

const serviceBadge = computed(() => {
  const raw = result.value?.raw
  const state = raw && typeof raw === 'object' ? String(raw.service_state || '') : ''
  return state ? serviceView(state) : null
})

const summary = computed(() => {
  if (!result.value) return { badge: '', tone: 'muted', text: '' }
  const elapsed = Math.max(0, Number(result.value.elapsed_ms) || 0)
  if (checks.value.length) {
    const pass = checks.value.filter((c) => c.ok).length
    const fail = checks.value.length - pass
    const ok = fail === 0 && result.value.passed !== false
    return {
      badge: ok ? '诊断通过' : '发现异常',
      tone: ok ? 'ok' : 'err',
      text: pass + ' 项通过 / ' + fail + ' 项异常，耗时 ' + elapsed + 'ms',
    }
  }
  const ok = !!result.value.passed
  return {
    badge: ok ? '诊断通过' : '诊断未通过',
    tone: ok ? 'ok' : 'err',
    text: '诊断' + (ok ? '通过' : '未通过') + '，耗时 ' + elapsed + 'ms',
  }
})

// 把 raw（对象）摊平成可读文本行，供 items 为空时兜底显示。
const RAW_LABELS = { component: '组件', service_state: '服务状态', config_path: '配置文件' }
const RAW_SKIP = new Set(['component', 'service_state', 'config_path', 'active_profile',
                          'checks', 'items', 'ok', 'raw'])

function flattenValue(value, prefix, depth, out) {
  if (value === null || value === undefined) return
  if (typeof value !== 'object') {
    out.push(prefix + '：' + value)
    return
  }
  if (depth >= 2) {
    try { out.push(prefix + '：' + JSON.stringify(value)) } catch (e) { out.push(prefix + '：—') }
    return
  }
  if (Array.isArray(value)) {
    value.forEach((item, i) => flattenValue(item, prefix + '[' + i + ']', depth + 1, out))
    return
  }
  Object.keys(value).forEach((key) => {
    flattenValue(value[key], prefix + '.' + key, depth + 1, out)
  })
}

const rawLines = computed(() => {
  if (checks.value.length) return []
  const raw = result.value?.raw
  if (raw === null || raw === undefined) return []
  if (typeof raw === 'string') return raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
  if (typeof raw !== 'object') return [String(raw)]
  const lines = []
  Object.keys(RAW_LABELS).forEach((key) => {
    if (raw[key] === undefined || raw[key] === null || raw[key] === '') return
    if (key === 'service_state') {
      const view = serviceView(raw[key])
      lines.push(RAW_LABELS[key] + '：' +
        (view.label === raw[key] ? raw[key] : view.label + '（' + raw[key] + '）'))
      return
    }
    lines.push(RAW_LABELS[key] + '：' + raw[key])
  })
  if (raw.active_profile && typeof raw.active_profile === 'object') {
    const name = raw.active_profile.name || raw.active_profile.id || ''
    const server = raw.active_profile.server || ''
    lines.push('当前服务端：' + (name || '—') + (server ? '（' + server + '）' : ''))
  }
  if (raw.ok !== undefined) lines.push('总体结果：' + (raw.ok ? '通过' : '未通过'))
  Object.keys(raw).forEach((key) => {
    if (RAW_SKIP.has(key)) return
    flattenValue(raw[key], key, 0, lines)
  })
  return lines
})

function toApiError(err) {
  if (err instanceof ApiError) return err
  return new ApiError(0, { error: String((err && err.message) || err) })
}

async function postDiagnose() {
  // 后端已把「诊断是否通过」放在 passed 字段，信封 ok 只表示请求成功，
  // 因此可以直接复用统一的 post()（自动带 CSRF、401 跳登录、超时处理）。
  return await post('/api/diagnose', {})
}

async function run() {
  if (loading.value) return
  loading.value = true
  error.value = null
  try {
    result.value = await postDiagnose()
    updatedAt.value = nowText()
  } catch (err) {
    result.value = null
    error.value = toApiError(err)
  } finally {
    loading.value = false
  }
}

onMounted(run)
</script>
<style scoped>
.diag-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.diag-item { display: flex; align-items: flex-start; gap: 10px; background: var(--panel);
  border: 1px solid var(--line); border-left: 3px solid var(--muted-line);
  border-radius: 10px; padding: 10px 12px; }
.diag-item.is-ok { border-left-color: var(--ok-fg); }
.diag-item.is-err { border-left-color: var(--err-fg); background: var(--err-bg); }
.diag-icon { flex: 0 0 20px; width: 20px; text-align: center; font-size: 16px;
  font-weight: 700; line-height: 1.5; }
.diag-item.is-ok .diag-icon,
.diag-item.is-ok .diag-name,
.diag-item.is-ok .diag-state { color: var(--ok-fg); }
.diag-item.is-err .diag-icon,
.diag-item.is-err .diag-name,
.diag-item.is-err .diag-state { color: var(--err-fg); }
.diag-body { flex: 1; min-width: 0; }
.diag-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.diag-name { font-weight: 600; }
.diag-state { font-size: 12px; }
.diag-detail { margin: 3px 0 0; font-size: 13px; color: var(--muted);
  white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
.diag-raw-line { margin: 4px 0; white-space: pre-wrap; overflow-wrap: anywhere;
  word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; }
</style>