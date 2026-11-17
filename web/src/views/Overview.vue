<template>
  <div class="page">
    <header class="page-head">
      <h2>概览</h2>
      <div class="page-tools">
        <span class="muted small">更新于 {{ updatedAt }}</span>
        <button class="ghost small" :disabled="loading" @click="load">刷新</button>
      </div>
    </header>

    <NoticeBar ref="notice" />

    <ErrorState v-if="error" :error="error" :retry="load" @retry="load" />

    <template v-if="data">
      <!-- 首次使用引导：无服务端时给出三步 -->
      <section v-if="!data.active_profile" class="card2 guide">
        <h3>还没有服务端设置，按这三步开始</h3>
        <ol class="steps">
          <li><b>① 新建服务端</b> — 填写你的 FRPS 服务器地址与 token
            <router-link class="ghost small" to="/profiles">去新建</router-link></li>
          <li class="muted"><b>② 新建端口映射</b> — 选择要穿透的本地端口（需先完成第 ① 步）</li>
          <li class="muted"><b>③ 启动 frpc</b> — 启动后在概览确认「已连接」</li>
        </ol>
      </section>
      <section v-else-if="!data.counts.total" class="card2 guide">
        <h3>已连接服务端「{{ data.active_profile.name }}」，还没有端口映射</h3>
        <p class="muted">添加映射后，外部就能通过「服务器地址:远程端口」访问你的本地服务。</p>
        <router-link class="ghost small" to="/mappings">新建第一条映射</router-link>
      </section>

      <!-- 状态带 + 服务控制 -->
      <section class="band">
        <div class="band-left">
          <StatusBadge :label="svc.label" :tone="svc.tone" />
          <StatusBadge :label="conn.label" :tone="conn.tone" />
          <span class="band-profile" v-if="data.active_profile">
            {{ data.active_profile.name }}
            <span class="muted">（{{ data.active_profile.server_addr }}:{{ data.active_profile.server_port }}）</span>
          </span>
        </div>
        <div class="band-actions">
          <button v-if="!isRunning" :disabled="busy || !data.active_profile"
                  :title="data.active_profile ? '' : '请先新建服务端'"
                  @click="runStart">启动</button>
          <button v-else class="warn-outline" :disabled="busy" @click="askStop">停止</button>
          <button class="warn-outline" :disabled="busy || !data.active_profile" @click="askRestart">重启</button>
        </div>
      </section>

      <!-- 4 个指标 -->
      <section class="stats">
        <div class="stat">
          <div class="stat-num" :class="{ muted: data.counts.total === null }">
            {{ data.counts.total === null ? '—' : data.counts.total }}
          </div>
          <div class="stat-label">映射总数</div>
        </div>
        <div class="stat"><div class="stat-num">{{ data.counts.enabled }}</div>
          <div class="stat-label">已启用</div></div>
        <div class="stat">
          <div class="stat-num" :class="onlineTone">{{ onlineText }}</div>
          <div class="stat-label">在线</div>
        </div>
        <div class="stat">
          <div class="stat-num" :class="data.pending ? 'warn' : ''">{{ data.pending ? '1' : '0' }}</div>
          <div class="stat-label">待重启</div>
        </div>
      </section>

      <!-- 需关注 -->
      <div v-for="(c, i) in data.concerns" :key="i" class="alert"
           :class="'tone-' + (c.level === 'err' ? 'err' : 'warn')">
        <div class="alert-head">
          <span>{{ c.text }}</span>
          <router-link v-if="c.level === 'err'" class="ghost small" to="/logs">查看日志</router-link>
        </div>
      </div>

      <div v-if="data.mappings_error" class="alert tone-err">
        <div class="alert-head">
          <span>映射读取失败：{{ data.mappings_error }}</span>
          <router-link class="ghost small" to="/settings">去恢复备份</router-link>
        </div>
      </div>

      <div v-if="data.pending" class="alert tone-warn">
        <div class="alert-head">
          <span>有配置修改尚未生效，需要重启 frpc</span>
          <button class="ghost small" :disabled="busy" @click="askRestart">立即重启</button>
        </div>
      </div>

      <!-- 公网访问（自映射） -->
      <section class="card2">
        <h3>公网访问管理端</h3>
        <div v-if="data.expose_mapping" class="kv">
          <StatusBadge label="已映射" tone="warn" />
          <code>{{ data.expose_mapping.external }}</code>
          <button class="ghost small" @click="copyExpose">复制</button>
          <button class="ghost small danger-text" :disabled="busy" @click="askUnExpose">取消映射</button>
        </div>
        <template v-else>
          <p class="muted small">默认只能在本机访问。映射到公网后，任何知道地址的人都能打开登录页。</p>
          <div class="row2">
            <label class="field">公网 TCP 端口
              <input v-model.number="expose.remotePort" type="number" min="1" max="65535"
                     placeholder="如 7501" />
            </label>
            <label class="field">或 HTTP 域名
              <input v-model.trim="expose.domain" placeholder="frpc.example.com" />
            </label>
          </div>
          <button class="ghost" :disabled="busy || (!expose.remotePort && !expose.domain)"
                  @click="askExpose">创建公网映射</button>
          <p class="muted small">暴露前请确认已设置强密码；面板以 root 运行，被突破等于服务器失守。</p>
        </template>
      </section>

      <p class="muted small footer">
        管理端地址：<code>{{ panelUrl }}</code>
        <button class="ghost small" @click="copy(panelUrl)">{{ copied === 'url' ? '已复制 ✓' : '复制' }}</button>
        · frpc {{ data.version }} · 管理器 {{ data.manager_version }}
      </p>
    </template>

    <ConfirmDialog :open="dialog.open" :title="dialog.title" :message="dialog.message"
                   :items="dialog.items" :danger="dialog.danger" :busy="dialog.busy"
                   :confirm-label="dialog.confirmLabel" :extra-label="dialog.extraLabel"
                   :require-text="dialog.requireText" :error="dialog.error"
                   @confirm="dialog.onConfirm" @extra="dialog.onExtra" @cancel="dialog.open = false" />
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { get, post, request, copyText, ApiError } from '../lib/api.js'
import { serviceView, connectionView } from '../lib/status.js'
import { nowText } from '../lib/format.js'
import StatusBadge from '../components/StatusBadge.vue'
import ErrorState from '../components/ErrorState.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import NoticeBar from '../components/NoticeBar.vue'

const data = ref(null); const error = ref(null); const loading = ref(false)
const busy = ref(false); const updatedAt = ref(''); const copied = ref('')
const notice = ref(null)
const expose = reactive({ remotePort: null, domain: '' })
const dialog = reactive({ open: false, title: '', message: '', items: [], danger: false,
                          busy: false, confirmLabel: '确认', extraLabel: '', error: '',
                          requireText: '', onConfirm: () => {}, onExtra: () => {} })
let timer = null

const svc = computed(() => data.value?.service_view || serviceView(data.value?.service_state))
const conn = computed(() => data.value?.connection_view || connectionView(data.value?.connection_state))
const isRunning = computed(() => ['running', 'active', 'started']
  .includes(String(data.value?.service_state).toLowerCase()))
const panelUrl = computed(() => location.origin + location.pathname)

// 在线数分档着色：全在线=绿，0 在线但有启用=红，部分=黄，未知=灰
const onlineText = computed(() => {
  const c = data.value?.counts
  if (!c || c.online === null || c.online === undefined) return '—'
  return c.online
})
const onlineTone = computed(() => {
  const c = data.value?.counts
  if (!c || c.online === null || c.online === undefined) return 'muted'
  if (!c.enabled) return ''
  if (c.online === 0) return 'err'
  if (c.online < c.enabled) return 'warn'
  return 'ok'
})

async function load() {
  loading.value = true; error.value = null
  try {
    data.value = await get('/api/overview')
    updatedAt.value = nowText()
  } catch (err) {
    error.value = err instanceof ApiError ? err : new ApiError(0, { error: String(err) })
  } finally { loading.value = false }
}

function flash() { return notice.value }

// 启动是"直接动作"，失败必须落在页面级提示上（原来写进未打开的弹层，用户看不到）
async function runStart() {
  busy.value = true
  try {
    await post('/api/service/start', {})
    flash()?.ok('已启动 frpc。')
    await load()
  } catch (err) { flash()?.err(err) }
  finally { busy.value = false }
}

async function doAction(action, mode) {
  dialog.busy = true; dialog.error = ''
  try {
    const res = await post('/api/service/' + action, mode ? { mode } : {})
    dialog.open = false
    flash()?.ok(res.message || (action === 'stop' ? '已停止 frpc。' : '已重启 frpc。'))
    await load()
  } catch (err) {
    dialog.error = err.message + (err.hint ? '（' + err.hint + '）' : '')
  } finally { dialog.busy = false }
}

function askStop() {
  Object.assign(dialog, {
    open: true, title: '停止 frpc？', message: '停止后所有映射立即失效，外部无法访问。',
    items: [], danger: true, confirmLabel: '停止', extraLabel: '', requireText: '', error: '',
    onConfirm: () => doAction('stop'),
  })
}

function askRestart() {
  const hasMappings = (data.value?.counts.enabled || 0) > 0
  Object.assign(dialog, {
    open: true, title: '重启 frpc？',
    message: hasMappings
      ? '重启会短暂中断全部映射。若你正通过映射端口 SSH 登录本机，请选「后台延迟重启」——它会先断开响应再重启，不会切断你当前的操作。'
      : '重启会短暂中断服务。',
    items: [], danger: false, confirmLabel: '立即重启',
    extraLabel: hasMappings ? '后台延迟重启' : '', requireText: '', error: '',
    onConfirm: () => doAction('restart', 'now'),
    onExtra: () => doAction('restart', 'detached'),
  })
}

async function askExpose() {
  const isTcp = !!expose.remotePort
  const label = isTcp ? 'TCP 端口 ' + expose.remotePort : '域名 ' + expose.domain
  Object.assign(dialog, {
    open: true, title: '把管理端映射到公网？',
    message: '任何知道地址的人都能打开管理端登录页。面板以 root 运行，请确保密码足够强。',
    items: ['将通过 ' + label + ' 暴露'],
    danger: true, confirmLabel: '确认暴露', extraLabel: '', requireText: '暴露', error: '',
    onConfirm: async () => {
      dialog.busy = true; dialog.error = ''
      try {
        await post('/api/expose', isTcp ? { type: 'tcp', remote_port: expose.remotePort }
                                        : { type: 'http', domain: expose.domain })
        dialog.open = false
        flash()?.ok('已创建公网映射；重启 frpc 后生效。')
        await load()
      } catch (err) { dialog.error = err.message + (err.hint ? '（' + err.hint + '）' : '') }
      finally { dialog.busy = false }
    },
  })
}

function askUnExpose() {
  Object.assign(dialog, {
    open: true, title: '取消公网映射？', message: '取消后该公网地址将无法访问管理端。',
    items: [], danger: true, confirmLabel: '取消映射', extraLabel: '', requireText: '', error: '',
    onConfirm: async () => {
      dialog.busy = true; dialog.error = ''
      try {
        await request('/api/expose', { method: 'DELETE' })
        dialog.open = false
        flash()?.ok('已取消公网映射。')
        await load()
      } catch (err) { dialog.error = err.message + (err.hint ? '（' + err.hint + '）' : '') }
      finally { dialog.busy = false }
    },
  })
}

async function copy(text) {
  if (await copyText(text)) {
    copied.value = 'url'
    setTimeout(() => { if (copied.value === 'url') copied.value = '' }, 1500)
  }
}
async function copyExpose() {
  if (await copyText(data.value.expose_mapping.external)) {
    copied.value = 'expose'
    setTimeout(() => { if (copied.value === 'expose') copied.value = '' }, 1500)
  }
}

function onVisible() { if (document.visibilityState === 'visible') load() }
onMounted(() => {
  load()
  timer = setInterval(() => { if (document.visibilityState === 'visible') load() }, 10000)
  document.addEventListener('visibilitychange', onVisible)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisible)
})
</script>
<style scoped>
.guide h3 { margin: 0 0 8px; font-size: 15px; }
.steps { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 8px; }
.steps a { margin-left: 8px; }
.stat-num.ok { color: var(--ok-fg); }
.stat-num.err { color: var(--err-fg); }
.stat-num.warn { color: var(--warn-fg); }
.stat-num.muted { color: var(--muted-fg); }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--muted); }
</style>
