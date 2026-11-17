<template>
  <div class="page">
    <!-- 逃生窗口：固定横幅 -->
    <div v-if="escape" class="alert tone-warn escape-banner" role="region"
         aria-label="地址切换逃生窗口">
      <p class="escape-line">
        <strong>新地址已生效：</strong><code>{{ escape.new }}</code>；
        旧地址 <code>{{ escape.old }}</code> 将在 <strong>{{ countdownText }}</strong> 后关闭
      </p>
      <div class="escape-actions">
        <button class="small" :disabled="escapeBusy !== ''" @click="confirmEscape">
          {{ escapeBusy === 'confirm' ? '处理中…' : '已确认新地址可用，立即关闭旧地址' }}
        </button>
        <button class="ghost small" :disabled="escapeBusy !== ''" @click="rollbackEscape">
          {{ escapeBusy === 'rollback' ? '处理中…' : '回滚到旧配置' }}
        </button>
      </div>
      <p class="escape-hint">
        {{ onNewAddr
          ? '当前页面已通过新地址访问，确认后旧地址会立即关闭。'
          : '当前页面仍通过旧地址（或反向代理）访问。建议先在新地址打开并登录本页面，再点确认；否则当前页面会立即失联。' }}
      </p>
      <p v-if="escapeError" class="err">{{ escapeError }}</p>
    </div>

    <header class="page-head">
      <h2>设置</h2>
      <div class="page-tools">
        <span v-if="updatedAt && !settingsLoading" class="muted small">更新于 {{ updatedAt }}</span>
        <button class="ghost small" :disabled="settingsLoading" @click="loadSettings()">
          {{ settingsLoading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </header>

    <ErrorState v-if="loadError" :error="loadError" :retry="loadSettings"
                 @retry="loadSettings" />
    <p v-else-if="settingsLoading && !settings" class="muted">正在读取设置…</p>

    <template v-if="settings">
      <!-- 1. 面板访问 -->
      <section class="card2">
        <div class="card-head">
          <h3>面板访问</h3>
          <StatusBadge :label="escape ? '正在切换地址' : '监听正常'" :tone="escape ? 'warn' : 'ok'" />
        </div>
        <div class="kv">
          <span>当前绑定</span>
          <code>{{ settings.bind }}:{{ settings.port }}</code>
        </div>
        <div class="kv">
          <span>页面地址</span>
          <code>{{ pageUrl }}</code>
        </div>

        <form class="form" @submit.prevent="applyPanel">
          <label class="field">绑定地址
            <input v-model.trim="panel.bind" placeholder="127.0.0.1 或 0.0.0.0"
                   autocomplete="off" autocapitalize="none" spellcheck="false" />
          </label>
          <label class="field">端口
            <input v-model.number="panel.port" type="number" inputmode="numeric"
                   min="1" max="65535" placeholder="7501" />
          </label>
          <div class="form-actions">
            <button type="submit" :disabled="panelBusy || !!escape">
              {{ panelBusy ? '保存中…' : '保存并生效' }}
            </button>
          </div>
        </form>
        <p class="small muted">
          保存后新地址立即生效，旧地址保留 5 分钟逃生窗口；期间两个地址都能访问，无需重新登录。
        </p>
        <p v-if="escape" class="warn small">当前有未确认的地址切换，请先在上方横幅确认或回滚，再修改地址。</p>
        <p v-if="panelError" class="err">{{ panelError }}</p>
        <p v-if="panelWarn" class="warn">{{ panelWarn }}</p>
        <p v-if="panelOk" class="ok">{{ panelOk }}</p>
      </section>

      <!-- 2. 账号安全 -->
      <section class="card2">
        <div class="card-head">
          <h3>账号安全</h3>
          <StatusBadge :label="settings.password_set ? '已设置密码' : '未设置密码'"
                       :tone="settings.password_set ? 'ok' : 'warn'" />
        </div>
        <p v-if="settings.first_run" class="warn small">当前仍使用初始密码，建议立即修改。</p>

        <form class="form" @submit.prevent="changePassword">
          <label class="field full">用户名
            <input v-model.trim="pwd.username" autocomplete="username"
                   autocapitalize="none" spellcheck="false" />
          </label>
          <label class="field">新密码
            <input v-model="pwd.password" type="password" autocomplete="new-password"
                   placeholder="至少 8 位" />
          </label>
          <label class="field">确认新密码
            <input v-model="pwd.confirm" type="password" autocomplete="new-password"
                   placeholder="再输入一次" />
          </label>
          <div class="form-actions">
            <button type="submit" :disabled="pwdBusy">{{ pwdBusy ? '修改中…' : '修改' }}</button>
          </div>
        </form>
        <p class="small muted">修改后旧密码在 5 分钟内仍然有效，避免记错密码被锁在门外。</p>
        <p v-if="pwdError" class="err">{{ pwdError }}</p>
        <p v-if="pwdOk" class="ok">{{ pwdOk }}</p>
        <p class="small muted footer">
          会话闲置 {{ settings.idle_timeout_minutes }} 分钟后自动退出；登录连续失败
          {{ settings.login_max_failures }} 次将锁定 {{ settings.login_lock_minutes }} 分钟。
        </p>
      </section>

      <!-- 3. 开机自启动 -->
      <section class="card2">
        <div class="card-head"><h3>开机自启动</h3></div>
        <div class="kv">
          <span>当前状态</span>
          <StatusBadge :label="autostartView.label" :tone="autostartView.tone" />
          <button class="small" :disabled="autostartBusy || settings.autostart === null"
                  @click="toggleAutostart">
            {{ autostartBusy
              ? '设置中…'
              : (settings.autostart === null ? '不可用' : (settings.autostart ? '取消' : '启用')) }}
          </button>
        </div>
        <p class="small muted">启用后，服务器重启时 frpc 会自动随系统启动；取消后需手动启动。</p>
        <p v-if="autostartError" class="err">{{ autostartError }}</p>
        <p v-if="autostartOk" class="ok">{{ autostartOk }}</p>
      </section>
    </template>

    <!-- 4. 备份与恢复（独立加载：即使设置读取失败也能用备份救配置） -->
    <section class="card2">
      <div class="card-head">
        <h3>备份与恢复</h3>
        <button class="small" :disabled="backupBusy" @click="createBackup">
          {{ backupBusy ? '备份中…' : '立即备份' }}
        </button>
      </div>
      <p v-if="backupNotice" class="ok">{{ backupNotice }}</p>
      <p v-if="backupActionError" class="err">{{ backupActionError }}</p>

      <ErrorState v-if="backupsError" :error="backupsError" :retry="loadBackups"
                 @retry="loadBackups" />
      <p v-else-if="backupsLoading && !backups.length" class="muted">正在读取备份…</p>
      <EmptyState v-else-if="!backups.length" icon="📦" title="暂无备份"
                  desc="备份会保存当前配置文件；恢复时可以回到这个时间点。">
        <button :disabled="backupBusy" @click="createBackup">
          {{ backupBusy ? '备份中…' : '立即备份' }}
        </button>
      </EmptyState>
      <ul v-else class="backup-list">
        <li v-for="b in backups" :key="b.name" class="backup-row">
          <div class="backup-info">
            <div class="backup-time">{{ backupTime(b.time) }}</div>
            <div class="backup-meta">
              <span>{{ backupReason(b.reason) }}</span>
              <span class="muted">· {{ fileCount(b) }} 个文件</span>
              <code class="backup-name">{{ b.name }}</code>
            </div>
          </div>
          <button class="ghost small" :disabled="restoreBusy === b.name" @click="askRestore(b)">
            {{ restoreBusy === b.name ? '恢复中…' : '恢复' }}
          </button>
        </li>
      </ul>
      <p class="small muted footer">恢复会覆盖当前配置；恢复前会自动创建一份「恢复配置前」备份。</p>
    </section>

    <!-- 5. 危险操作 -->
    <section class="card2 danger-card">
      <div class="card-head"><h3>危险操作</h3></div>
      <div class="danger-item">
        <div>
          <strong>卸载 frpc</strong>
          <p class="muted small">
            将停止并移除管理端本身，本页面会立即失联；卸载后需要重新运行安装脚本才能恢复。
          </p>
        </div>
        <button class="danger small" :disabled="dangerStarted" @click="askUninstall">卸载 frpc</button>
      </div>
      <div class="danger-item">
        <div>
          <strong>升级 frpc</strong>
          <p class="muted small">将下载并安装新版本；升级期间管理端会重启，本页面会短暂失联。</p>
        </div>
        <button class="danger small" :disabled="dangerStarted" @click="askUpgrade">升级 frpc</button>
      </div>
      <p v-if="dangerMsg" class="warn">{{ dangerMsg }}</p>
      <p class="small muted footer">以上操作不可撤销，请确认已在终端保留恢复手段。</p>
    </section>

    <ConfirmDialog :open="dialog.open" :title="dialog.title" :message="dialog.message"
                   :items="dialog.items" :danger="dialog.danger" :busy="dialog.busy"
                   :require-text="dialog.requireText" :confirm-label="dialog.confirmLabel"
                   :error="dialog.error" @confirm="dialog.onConfirm" @cancel="closeDialog" />
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { get, post, setSession, ApiError } from '../lib/api.js'
import { hhmmss, nowText } from '../lib/format.js'
import StatusBadge from '../components/StatusBadge.vue'
import ErrorState from '../components/ErrorState.vue'
import EmptyState from '../components/EmptyState.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const settings = ref(null)
const settingsLoading = ref(false)
const loadError = ref(null)
const updatedAt = ref('')

// 面板访问
const panel = reactive({ bind: '', port: null })
const panelBusy = ref(false)
const panelError = ref('')
const panelWarn = ref('')
const panelOk = ref('')

// 逃生窗口
const escape = ref(null)
const secondsLeft = ref(0)
const escapeBusy = ref('')
const escapeError = ref('')
let escapeTimer = null

// 账号安全
const pwd = reactive({ username: '', password: '', confirm: '' })
const pwdBusy = ref(false)
const pwdError = ref('')
const pwdOk = ref('')

// 开机自启动
const autostartBusy = ref(false)
const autostartError = ref('')
const autostartOk = ref('')

// 备份与恢复
const backups = ref([])
const backupsLoading = ref(false)
const backupsError = ref(null)
const backupBusy = ref(false)
const backupNotice = ref('')
const backupActionError = ref('')
const restoreBusy = ref('')

// 危险操作
const dangerStarted = ref(false)
const dangerMsg = ref('')

const dialog = reactive({
  open: false, title: '', message: '', items: [], danger: false,
  requireText: '', confirmLabel: '确认', busy: false, error: '',
  onConfirm: () => {},
})

const pageUrl = computed(() => location.origin + location.pathname)
const countdownText = computed(() => hhmmss(secondsLeft.value))

const autostartView = computed(() => {
  const value = settings.value ? settings.value.autostart : null
  if (value === null || value === undefined) return { label: '当前平台不支持', tone: 'muted' }
  return value ? { label: '已启用', tone: 'ok' } : { label: '未启用', tone: 'warn' }
})

const WILDCARD_HOSTS = new Set(['0.0.0.0', '::', '[::]'])

// 当前页面是否已经通过「新地址」访问：绑定地址是通配符时，主机名不同也算同一监听。
const onNewAddr = computed(() => {
  if (!escape.value || !escape.value.new) return false
  try {
    const target = new URL(escape.value.new)
    const samePort = (target.port || '') === (location.port || '')
    const sameHost = target.hostname === location.hostname || WILDCARD_HOSTS.has(target.hostname)
    return samePort && sameHost
  } catch (e) { return false }
})

// 0.0.0.0 / :: 不能直接点开，换成当前访问的主机名，给用户一个可用的地址。
function displayAddr(raw) {
  try {
    const target = new URL(raw)
    if (WILDCARD_HOSTS.has(target.hostname)) {
      target.hostname = location.hostname
      return target.toString()
    }
  } catch (e) { /* 保持原样 */ }
  return raw
}

function toApiError(err) {
  if (err instanceof ApiError) return err
  return new ApiError(0, { error: String((err && err.message) || err) })
}

function errText(err) {
  if (!err) return '操作失败'
  return (err.message || String(err)) + (err.hint ? '（' + err.hint + '）' : '')
}

// ---------------------------------------------------------------- 逃生窗口倒计时

function stopCountdown() {
  if (escapeTimer) { clearInterval(escapeTimer); escapeTimer = null }
}

function startCountdown(seconds) {
  stopCountdown()
  secondsLeft.value = Math.max(0, Math.floor(Number(seconds) || 0))
  if (secondsLeft.value <= 0) { escape.value = null; return }
  escapeTimer = setInterval(() => {
    secondsLeft.value = Math.max(0, secondsLeft.value - 1)
    if (secondsLeft.value === 0) {
      stopCountdown()
      escape.value = null          // 倒计时归零：自动隐藏横幅
    }
  }, 1000)
}

function syncEscape(info) {
  if (!info || !info.new) {
    escape.value = null
    stopCountdown()
    return
  }
  escape.value = { old: info.old || '', new: info.new || '' }
  startCountdown(info.seconds_left)
}

// ---------------------------------------------------------------- 设置读取

async function loadSettings(options = {}) {
  const silent = options && options.silent === true
  if (!silent) { settingsLoading.value = true; loadError.value = null }
  try {
    const data = await get('/api/settings')
    settings.value = data
    panel.bind = data.bind || ''
    panel.port = data.port
    pwd.username = data.username || ''
    syncEscape(data.escape)
    updatedAt.value = nowText()
  } catch (err) {
    if (!silent) loadError.value = toApiError(err)
  } finally {
    if (!silent) settingsLoading.value = false
  }
}

// ---------------------------------------------------------------- 1. 面板访问

function validatePanel() {
  const bind = String(panel.bind || '').trim()
  if (!bind) return '请填写绑定地址（如 127.0.0.1 或 0.0.0.0）'
  if (!/^[0-9a-fA-F:.]+$/.test(bind)) return '绑定地址必须是合法 IP（如 127.0.0.1 或 0.0.0.0）'
  if (panel.port === '' || panel.port === null || panel.port === undefined) return '请填写端口'
  const port = Number(panel.port)
  if (!Number.isInteger(port) || port < 1 || port > 65535) return '端口必须是 1-65535 之间的整数'
  return ''
}

async function applyPanel() {
  if (panelBusy.value) return
  panelError.value = ''
  panelWarn.value = ''
  panelOk.value = ''
  const message = validatePanel()
  if (message) { panelError.value = message; return }
  panelBusy.value = true
  try {
    const data = await post('/api/settings/apply', {
      bind: String(panel.bind).trim(), port: Number(panel.port),
    })
    syncEscape(data.escape)
    if (data.changed === false) {
      panelOk.value = '绑定地址和端口没有变化，无需切换。'
    } else if (data.escape) {
      panelOk.value = '新地址已生效，请在新地址确认可用后关闭旧地址。'
    } else {
      panelOk.value = '设置已保存并重新监听。'
    }
    await loadSettings({ silent: true })
  } catch (err) {
    panelError.value = errText(err)
  } finally {
    panelBusy.value = false
  }
}

async function confirmEscape() {
  if (escapeBusy.value) return
  const wasOnNew = onNewAddr.value
  const newAddr = escape.value ? escape.value.new : ''
  escapeBusy.value = 'confirm'
  escapeError.value = ''
  try {
    const data = await post('/api/settings/confirm', {})
    stopCountdown()
    escape.value = null
    panelOk.value = data.message || '旧监听已关闭'
    if (!wasOnNew && newAddr) {
      panelWarn.value = '旧地址已关闭；当前页面可能已失联，请改用新地址 ' + displayAddr(newAddr) + ' 重新打开管理端。'
    }
    await loadSettings({ silent: true })
  } catch (err) {
    escapeError.value = errText(err)
  } finally {
    escapeBusy.value = ''
  }
}

async function rollbackEscape() {
  if (escapeBusy.value) return
  const wasOnNew = onNewAddr.value
  const oldAddr = escape.value ? escape.value.old : ''
  escapeBusy.value = 'rollback'
  escapeError.value = ''
  try {
    const data = await post('/api/settings/rollback', {})
    stopCountdown()
    escape.value = null
    panelOk.value = data.message || '已回滚到旧配置（旧监听仍在服务）'
    if (wasOnNew && oldAddr) {
      panelWarn.value = '新监听已关闭；当前页面可能已失联，请改用旧地址 ' + displayAddr(oldAddr) + ' 重新打开管理端。'
    }
    await loadSettings({ silent: true })
  } catch (err) {
    escapeError.value = errText(err)
  } finally {
    escapeBusy.value = ''
  }
}

// ---------------------------------------------------------------- 2. 账号安全

async function changePassword() {
  if (pwdBusy.value) return
  pwdError.value = ''
  pwdOk.value = ''
  const username = String(pwd.username || '').trim()
  const password = String(pwd.password || '')
  const confirm = String(pwd.confirm || '')
  if (username.length < 2) { pwdError.value = '用户名至少 2 个字符'; return }
  if (password.length < 8) { pwdError.value = '新密码至少 8 位'; return }
  if (password !== confirm) { pwdError.value = '两次输入的新密码不一致'; return }
  pwdBusy.value = true
  try {
    const data = await post('/api/settings/password', { username, password })
    const name = data.username || username
    if (name) {
      if (settings.value) settings.value.username = name
      setSession({ username: name })
    }
    if (settings.value) {
      settings.value.password_set = true
      settings.value.first_run = false
    }
    pwd.password = ''
    pwd.confirm = ''
    if (data.password_changed) {
      const minutes = Math.max(1, Math.round((Number(data.old_password_valid_for) || 300) / 60))
      pwdOk.value = '已修改；旧密码在 ' + minutes + ' 分钟内仍然有效'
    } else {
      pwdOk.value = '已修改'
    }
  } catch (err) {
    pwdError.value = errText(err)
  } finally {
    pwdBusy.value = false
  }
}

// ---------------------------------------------------------------- 3. 开机自启动

async function toggleAutostart() {
  if (autostartBusy.value || !settings.value || settings.value.autostart === null) return
  autostartBusy.value = true
  autostartError.value = ''
  autostartOk.value = ''
  const want = !settings.value.autostart
  try {
    const data = await post('/api/autostart', { enabled: want })
    const value = data.enabled === undefined ? want : data.enabled
    settings.value.autostart = value === null ? null : !!value
    autostartOk.value = settings.value.autostart ? '已启用开机自启动' : '已取消开机自启动'
  } catch (err) {
    autostartError.value = errText(err)
  } finally {
    autostartBusy.value = false
  }
}

// ---------------------------------------------------------------- 4. 备份与恢复

const BACKUP_REASONS = {
  manual: '手动备份',
  change: '自动保存前快照',
  'frpc-config': '写入客户端配置前',
  'frps-change': '改动服务端配置前',
  'pre-restore': '恢复配置前',
  'pre-delete': '删除服务端前',
  upgrade: '升级前快照',
  unknown: '未知原因',
}

function backupReason(reason) {
  const key = String(reason || '').toLowerCase()
  return BACKUP_REASONS[key] || String(reason || '未知原因')
}

// 后端 time 形如 20261115-230102
function backupTime(raw) {
  const text = String(raw || '')
  if (/^\d{8}-\d{6}$/.test(text)) {
    return text.slice(0, 4) + '-' + text.slice(4, 6) + '-' + text.slice(6, 8) +
      ' ' + text.slice(9, 11) + ':' + text.slice(11, 13)
  }
  return text || '—'
}

function fileCount(backup) {
  if (!backup) return 0
  if (Array.isArray(backup.files)) return backup.files.length
  return Number(backup.files) || 0
}

async function loadBackups() {
  backupsLoading.value = true
  backupsError.value = null
  try {
    const data = await get('/api/backups')
    const items = Array.isArray(data.items) ? data.items : []
    // 备份名以时间戳开头，按名称倒序即为最新在前
    backups.value = items.slice().sort((a, b) =>
      String(b.name || '').localeCompare(String(a.name || '')))
  } catch (err) {
    backupsError.value = toApiError(err)
  } finally {
    backupsLoading.value = false
  }
}

async function createBackup() {
  if (backupBusy.value) return
  backupBusy.value = true
  backupNotice.value = ''
  backupActionError.value = ''
  try {
    const data = await post('/api/backups', {})
    backupNotice.value = '已创建备份 ' + (data.name || '')
    await loadBackups()
  } catch (err) {
    backupActionError.value = errText(err)
  } finally {
    backupBusy.value = false
  }
}

function askRestore(backup) {
  Object.assign(dialog, {
    open: true, title: '恢复备份？', danger: true,
    message: '恢复会用该备份中的文件覆盖当前配置；恢复前会自动创建一份「恢复配置前」备份。恢复完成后，如 frpc 正在运行，需要重启才会加载。',
    items: [
      '备份：' + backup.name,
      '时间：' + backupTime(backup.time) + ' · ' + backupReason(backup.reason),
      '文件数：' + fileCount(backup) + ' 个',
    ],
    requireText: backup.name, confirmLabel: '恢复备份', busy: false, error: '',
    onConfirm: () => doRestore(backup),
  })
}

async function doRestore(backup) {
  dialog.busy = true
  dialog.error = ''
  restoreBusy.value = backup.name
  try {
    const data = await post('/api/restore', { name: backup.name, confirm: backup.name })
    dialog.open = false
    const count = Array.isArray(data.restored) ? data.restored.length : 0
    backupNotice.value = '已恢复备份 ' + (data.name || backup.name) +
      (count ? '（' + count + ' 个文件）' : '') + '；如 frpc 正在运行，请重启后生效。'
    await loadBackups()
  } catch (err) {
    dialog.error = errText(err)
  } finally {
    dialog.busy = false
    restoreBusy.value = ''
  }
}

// ---------------------------------------------------------------- 5. 危险操作

function askUninstall() {
  Object.assign(dialog, {
    open: true, title: '卸载 frpc？', danger: true, requireText: 'frpc', confirmLabel: '卸载',
    message: '将停止并移除管理端本身，本页面会立即失联。卸载后如需继续使用，必须重新运行安装脚本。',
    items: [
      '停止并移除 frpc 与管理端服务',
      '删除程序文件，本页面会立即失联',
      '此操作不可撤销',
    ],
    busy: false, error: '',
    onConfirm: doUninstall,
  })
}

async function doUninstall() {
  dialog.busy = true
  dialog.error = ''
  try {
    const data = await post('/api/uninstall', { confirm: 'frpc' })
    dialog.open = false
    dangerStarted.value = true
    dangerMsg.value = (data.message || '卸载已开始') + '；本页面会立即失联。'
  } catch (err) {
    dialog.error = errText(err)
  } finally {
    dialog.busy = false
  }
}

function askUpgrade() {
  Object.assign(dialog, {
    open: true, title: '升级 frpc？', danger: true, requireText: 'frpc', confirmLabel: '升级',
    message: '将下载并安装最新版本，升级过程中管理端会自动重启，本页面会短暂失联。',
    items: [
      '下载新版本并校验完整性',
      '升级前自动创建快照备份',
      '完成后管理端自动重启，稍后刷新本页面',
    ],
    busy: false, error: '',
    onConfirm: doUpgrade,
  })
}

async function doUpgrade() {
  dialog.busy = true
  dialog.error = ''
  try {
    const data = await post('/api/upgrade', {})
    dialog.open = false
    dangerStarted.value = true
    dangerMsg.value = data.message
      ? data.message + '；请稍后刷新本页面。'
      : '升级已开始；完成后管理端会自动重启，请稍后刷新本页面。'
  } catch (err) {
    dialog.error = errText(err)
  } finally {
    dialog.busy = false
  }
}

function closeDialog() {
  if (!dialog.busy) dialog.open = false
}

onMounted(() => {
  loadSettings()
  loadBackups()
})

onUnmounted(stopCountdown)
</script>
<style scoped>
/* 逃生窗口横幅：粘在页面顶部，滚动时仍可见 */
.escape-banner { position: sticky; top: 8px; z-index: 20; margin-bottom: 14px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, .35); }
.escape-line { margin: 0; font-size: 14px; }
.escape-line code { word-break: break-all; }
.escape-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.escape-hint { margin: 6px 0 0; font-size: 12px; opacity: .9; }
.escape-banner .err { margin: 6px 0 0; }

.card-head { display: flex; align-items: center; justify-content: space-between;
  gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.card-head h3 { margin: 0; }

.form { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; margin-top: 6px; }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 13px;
  color: var(--muted); margin: 6px 0; }
.field.full { grid-column: 1 / -1; }
.form-actions { grid-column: 1 / -1; display: flex; gap: 8px; margin-top: 4px; }

.backup-list { list-style: none; margin: 0; padding: 0; display: flex;
  flex-direction: column; gap: 8px; }
.backup-row { display: flex; align-items: center; justify-content: space-between;
  gap: 10px; flex-wrap: wrap; border: 1px solid var(--line); border-radius: 8px;
  padding: 8px 10px; background: var(--bg); }
.backup-info { min-width: 0; }
.backup-time { font-weight: 600; }
.backup-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 13px; }
.backup-name { font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }

.danger-card { border-left: 4px solid var(--err-fg); margin-top: 20px; }
.danger-item { display: flex; justify-content: space-between; align-items: flex-start;
  gap: 12px; flex-wrap: wrap; padding: 10px 0; border-top: 1px solid var(--line); }
.danger-item:first-of-type { border-top: 0; }
.danger-item p { margin: 2px 0 0; }
.danger-item button { flex: 0 0 auto; }

@media (max-width: 720px) {
  .form { grid-template-columns: 1fr; }
  .escape-actions button { width: 100%; }
  .backup-row { align-items: flex-start; }
}
</style>