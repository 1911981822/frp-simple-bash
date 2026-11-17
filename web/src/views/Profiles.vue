<template>
  <div class="page">
    <header class="page-head">
      <h2>服务端（FRPS）</h2>
      <div class="page-tools">
        <span class="muted small">更新于 {{ updatedAt || '—' }}</span>
        <button class="ghost small" :disabled="loading || !!busyId" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
        <button @click="openCreate">新建服务端</button>
      </div>
    </header>

    <!-- 首次加载失败：整块错误 + 重试 -->
    <ErrorState v-if="error" :error="error" :retry="load" @retry="load" />

    <template v-else>
      <!-- 操作结果提示：成功自动消失，失败保留并可关闭 -->
      <div v-if="notice" class="alert" :class="'tone-' + notice.tone"
           :role="notice.tone === 'ok' ? 'status' : 'alert'"
           :aria-live="notice.tone === 'ok' ? 'polite' : 'assertive'">
        <div class="alert-head">
          <span>{{ notice.text }}</span>
          <button class="ghost small" @click="dismissNotice">关闭</button>
        </div>
        <p v-if="notice.hint" class="alert-hint">{{ notice.hint }}</p>
      </div>

      <p v-if="!loaded" class="muted">正在加载…</p>

      <EmptyState v-else-if="!items.length" icon="🖥" title="还没有服务端"
                  desc="需要先添加 FRPS 服务器才能创建端口映射">
        <button @click="openCreate">新建服务端</button>
      </EmptyState>

      <div v-else class="profile-list">
        <article v-for="p in items" :key="p.id" class="profile-card"
                 :class="{ active: p.is_active }">
          <div class="profile-main">
            <div class="profile-title">
              <h3>{{ p.name }}</h3>
              <StatusBadge v-if="p.is_active" label="当前" tone="info" />
              <StatusBadge :label="p.tls_enable === false ? 'TLS 未启用' : 'TLS 已启用'"
                           :tone="p.tls_enable === false ? 'warn' : 'ok'" />
            </div>
            <div class="profile-meta">
              <code>{{ addrText(p) }}</code>
              <span v-if="p.mapping_count === null" class="warn small">端口映射读取失败</span>
              <span v-else class="muted small">端口映射 {{ p.mapping_count }} 条</span>
            </div>
          </div>
          <div class="profile-actions">
            <button class="small" :disabled="p.is_active || !!busyId" @click="makeActive(p)">
              {{ busyId === p.id ? '切换中…' : '设为当前' }}
            </button>
            <button class="ghost small" :disabled="!!busyId" @click="openEdit(p)">编辑</button>
            <button class="danger small" :disabled="!!busyId" @click="askDelete(p)">删除</button>
          </div>
        </article>
      </div>
    </template>

    <!-- 新建 / 编辑服务端 -->
    <div v-if="form.open" class="mask" @click.self="closeForm">
      <div class="dialog form-dialog" role="dialog" aria-modal="true"
           aria-labelledby="profile-dialog-title">
        <h3 id="profile-dialog-title">{{ isEditing ? '编辑服务端' : '新建服务端' }}</h3>
        <form class="profile-form" novalidate @submit.prevent="submitForm">
          <label for="pf-name">名称
            <span class="req" aria-hidden="true">*</span>
            <input id="pf-name" ref="nameInput" v-model="form.name"
                   placeholder="如 生产服务器" autocomplete="off"
                   :disabled="form.busy" :aria-invalid="!!form.fieldErrors.name"
                   @input="onFieldInput('name')" />
            <span v-if="form.fieldErrors.name" class="field-err">{{ form.fieldErrors.name }}</span>
          </label>

          <label for="pf-addr">服务器地址
            <span class="req" aria-hidden="true">*</span>
            <input id="pf-addr" ref="addrInput" v-model="form.server_addr"
                   placeholder="1.2.3.4" autocomplete="off" spellcheck="false"
                   :disabled="form.busy" :aria-invalid="!!form.fieldErrors.server_addr"
                   @input="onFieldInput('server_addr')" />
            <span v-if="form.fieldErrors.server_addr" class="field-err">{{ form.fieldErrors.server_addr }}</span>
          </label>

          <label for="pf-port">服务器端口
            <span class="req" aria-hidden="true">*</span>
            <input id="pf-port" ref="portInput" v-model.number="form.server_port"
                   type="number" min="1" max="65535" inputmode="numeric"
                   :disabled="form.busy" :aria-invalid="!!form.fieldErrors.server_port"
                   @input="onFieldInput('server_port')" />
            <span v-if="form.fieldErrors.server_port" class="field-err">{{ form.fieldErrors.server_port }}</span>
          </label>

          <label for="pf-token">Token
            <span v-if="!isEditing" class="req" aria-hidden="true">*</span>
            <span class="pw">
              <input id="pf-token" ref="tokenInput" v-model="form.token"
                     :type="form.showToken ? 'text' : 'password'"
                     :placeholder="isEditing ? '留空表示不修改' : '与 frps 配置中的 auth.token 一致'"
                     autocomplete="off" spellcheck="false"
                     :disabled="form.busy" :aria-invalid="!!form.fieldErrors.token"
                     @input="onFieldInput('token')" />
              <button type="button" class="ghost small pw-toggle"
                      @click="form.showToken = !form.showToken">{{ form.showToken ? '隐藏' : '显示' }}</button>
            </span>
            <span v-if="form.fieldErrors.token" class="field-err">{{ form.fieldErrors.token }}</span>
          </label>

          <label class="check">
            <input v-model="form.tls_enable" type="checkbox" :disabled="form.busy" />
            <span>启用 TLS（推荐）</span>
          </label>

          <p v-if="form.error" class="err" role="alert">{{ form.error }}</p>
          <p v-if="form.hint" class="muted small">{{ form.hint }}</p>

          <div class="dialog-actions">
            <button type="button" class="ghost" :disabled="form.busy" @click="closeForm">取消</button>
            <button type="submit" :disabled="form.busy">{{ form.busy ? '保存中…' : '保存' }}</button>
          </div>
        </form>
      </div>
    </div>

    <!-- 删除确认：必须输入服务端名称 -->
    <ConfirmDialog :open="deleteState.open" :title="deleteTitle"
                   message="删除后不可恢复。删除前会自动备份配置。"
                   :items="deleteItems" :danger="true" :busy="deleteState.busy"
                   :require-text="deleteState.target ? deleteState.target.name : ''"
                   :error="deleteState.error" confirm-label="删除服务端"
                   @confirm="doDelete" @cancel="closeDelete" />
  </div>
</template>
<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { get, post, request, ApiError } from '../lib/api.js'
import { nowText } from '../lib/format.js'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const items = ref([])
const activeId = ref('')
const error = ref(null)
const loading = ref(false)
const loaded = ref(false)
const updatedAt = ref('')
const busyId = ref('')          // 正在切换的服务端 id
const notice = ref(null)        // { tone, text, hint }
let noticeTimer = null

const nameInput = ref(null)
const addrInput = ref(null)
const portInput = ref(null)
const tokenInput = ref(null)

const form = reactive({
  open: false, editingId: '', name: '', server_addr: '', server_port: 7000,
  token: '', tls_enable: true, showToken: false,
  busy: false, error: '', hint: '', fieldErrors: {},
})

const deleteState = reactive({ open: false, target: null, busy: false, error: '' })

const isEditing = computed(() => !!form.editingId)
const deleteTitle = computed(() => deleteState.target
  ? '删除服务端「' + deleteState.target.name + '」？' : '删除服务端？')

const deleteItems = computed(() => {
  const p = deleteState.target
  if (!p) return []
  const out = []
  if (p.mapping_count === null) {
    out.push('该服务端的端口映射数量读取失败，删除后其映射配置也会一并移除')
  } else {
    out.push('将同时删除 ' + p.mapping_count + ' 条端口映射')
  }
  if (p.is_active) out.push('这是当前服务端：删除后 frpc 将停止，所有映射立即失效')
  return out
})

const NAME_RE = /^[A-Za-z0-9_\-\u3400-\u9fff]+$/

function toApiError(err) {
  return err instanceof ApiError ? err : new ApiError(0, { error: String(err) })
}

// IPv6 加方括号，避免「地址:端口」产生歧义
function addrText(p) {
  const addr = String(p.server_addr || '')
  return (addr.indexOf(':') >= 0 ? '[' + addr + ']' : addr) + ':' + p.server_port
}

function showNotice(tone, text, hint = '') {
  notice.value = { tone, text, hint }
  if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null }
  // 成功提示自动消失；失败提示保留，由用户关闭，避免一闪而过错过原因
  if (tone === 'ok') {
    noticeTimer = setTimeout(() => { notice.value = null; noticeTimer = null }, 5000)
  }
}

function dismissNotice() {
  notice.value = null
  if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null }
}

async function load() {
  loading.value = true
  error.value = null
  try {
    const data = await get('/api/profiles')
    items.value = Array.isArray(data.items) ? data.items : []
    activeId.value = data.active_id || ''
    updatedAt.value = nowText()
    loaded.value = true
    return true
  } catch (err) {
    const apiErr = toApiError(err)
    // 首次加载失败用整块错误态；已有数据时只做行内提示，避免把列表吞掉
    if (!loaded.value) error.value = apiErr
    else showNotice('err', '刷新列表失败：' + apiErr.message, apiErr.hint)
    return false
  } finally {
    loading.value = false
  }
}

function resetForm() {
  Object.assign(form, {
    editingId: '', name: '', server_addr: '', server_port: 7000,
    token: '', tls_enable: true, showToken: false,
    busy: false, error: '', hint: '', fieldErrors: {},
  })
}

function openCreate() {
  resetForm()
  form.open = true
  nextTick(() => { if (nameInput.value) nameInput.value.focus() })
}

function openEdit(p) {
  resetForm()
  Object.assign(form, {
    open: true,
    editingId: p.id,
    name: p.name,
    server_addr: p.server_addr,
    server_port: p.server_port,
    tls_enable: p.tls_enable !== false,
  })
  nextTick(() => { if (nameInput.value) nameInput.value.focus() })
}

function closeForm() {
  if (form.busy) return
  form.open = false
}

function onFieldInput(field) {
  if (form.fieldErrors[field]) form.fieldErrors[field] = ''
  if (form.error) { form.error = ''; form.hint = '' }
}

function validate() {
  const errors = {}
  const name = form.name.trim()
  if (!name) errors.name = '请填写名称'
  else if (name.length > 64) errors.name = '名称最多 64 个字符'
  else if (!NAME_RE.test(name)) errors.name = '名称只允许中文、字母、数字、-、_，不能包含空格'

  const addr = form.server_addr.trim()
  if (!addr) errors.server_addr = '请填写服务器地址'
  else if (/\s/.test(addr)) errors.server_addr = '地址不能包含空格'
  else if (addr.indexOf('://') >= 0) errors.server_addr = '只填地址，不要带 http:// 前缀'

  const rawPort = form.server_port
  const port = Number(rawPort)
  if (rawPort === '' || rawPort === null || rawPort === undefined) {
    errors.server_port = '请填写服务器端口'
  } else if (!Number.isInteger(port) || port < 1 || port > 65535) {
    errors.server_port = '端口必须是 1-65535 的整数'
  }

  if (!isEditing.value && !form.token.trim()) errors.token = '新建服务端必须填写 token'

  form.fieldErrors = errors
  const first = Object.keys(errors)[0]
  if (first) {
    const refs = { name: nameInput, server_addr: addrInput, server_port: portInput, token: tokenInput }
    nextTick(() => {
      const el = refs[first]
      if (el && el.value) el.value.focus()
    })
  }
  return !first
}

async function submitForm() {
  if (form.busy) return
  form.error = ''; form.hint = ''
  if (!validate()) return

  const editing = isEditing.value
  const body = {
    name: form.name.trim(),
    server_addr: form.server_addr.trim(),
    server_port: Number(form.server_port),
    tls_enable: !!form.tls_enable,
  }
  // 编辑时 token 留空 = 不修改，因此不提交该字段
  if (!editing || form.token.trim()) body.token = form.token.trim()

  form.busy = true
  try {
    if (editing) {
      await request('/api/profiles/' + encodeURIComponent(form.editingId), { method: 'PUT', body })
    } else {
      await post('/api/profiles', body)
    }
    form.open = false
    const refreshed = await load()
    let hint = ''
    if (!refreshed) hint = '列表刷新失败，请点击「刷新」查看最新状态'
    else if (!editing && !activeId.value) hint = '该服务端还未设为当前，点击「设为当前」后 frpc 才会使用它'
    showNotice('ok', (editing ? '已保存服务端「' : '已创建服务端「') + body.name + '」', hint)
  } catch (err) {
    const apiErr = toApiError(err)
    form.error = apiErr.message || '保存失败'
    form.hint = apiErr.hint || ''
  } finally {
    form.busy = false
  }
}

async function makeActive(p) {
  if (busyId.value) return
  busyId.value = p.id
  dismissNotice()
  try {
    // 切换包含可达性检测与重启，耗时可能超过默认 20 秒
    const data = await post('/api/profiles/' + encodeURIComponent(p.id) + '/switch',
                            undefined, { timeout: 90000 })
    await load()
    if (data && data.active_id === p.id) {
      showNotice('ok', '已切换到「' + p.name + '」')
    } else {
      // 后端检测到新服务端不可达时会自动回退，此时 active_id 仍是旧值
      showNotice('warn', '切换未生效：未能连接到「' + p.name + '」，已保留原服务端',
                 '请检查该服务端的地址 / 端口 / token 与防火墙后重试。')
    }
  } catch (err) {
    const apiErr = toApiError(err)
    showNotice('err', '切换到「' + p.name + '」失败：' + apiErr.message, apiErr.hint)
  } finally {
    busyId.value = ''
  }
}

function askDelete(p) {
  Object.assign(deleteState, { open: true, target: p, busy: false, error: '' })
}

function closeDelete() {
  if (deleteState.busy) return
  deleteState.open = false
  deleteState.target = null
  deleteState.error = ''
}

async function doDelete() {
  const p = deleteState.target
  if (!p || deleteState.busy) return
  deleteState.busy = true
  deleteState.error = ''
  try {
    await request('/api/profiles/' + encodeURIComponent(p.id),
                  { method: 'DELETE', body: { confirm: p.name } })
    deleteState.open = false
    deleteState.target = null
    const refreshed = await load()
    showNotice('ok', '已删除服务端「' + p.name + '」',
               refreshed ? '' : '列表刷新失败，请点击「刷新」确认最新状态')
  } catch (err) {
    const apiErr = toApiError(err)
    deleteState.error = apiErr.message + (apiErr.hint ? '（' + apiErr.hint + '）' : '')
  } finally {
    deleteState.busy = false
  }
}

function onKeydown(ev) {
  if (ev.key !== 'Escape') return
  if (form.open) { if (!form.busy) closeForm() }
  else if (deleteState.open && !deleteState.busy) closeDelete()
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  load()
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  if (noticeTimer) clearTimeout(noticeTimer)
})
</script>
<style scoped>
.profile-list { display:flex; flex-direction:column; gap:10px; }
.profile-card {
  background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; display:flex; align-items:center; justify-content:space-between;
  gap:12px; flex-wrap:wrap;
}
.profile-card.active { border-color:var(--info-line); }
.profile-main { min-width:0; flex:1; }
.profile-title { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:6px; }
.profile-title h3 { margin:0; font-size:15px; font-weight:600; }
.profile-meta { display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:13px; }
.profile-actions { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }

.form-dialog { max-height:calc(100dvh - 32px); overflow:auto; }
.profile-form { display:flex; flex-direction:column; gap:12px; }
.profile-form p { margin:0; }
.profile-form label { display:block; font-size:13px; color:var(--muted); }
.profile-form label > input,
.profile-form .pw { display:block; margin-top:6px; }
.profile-form .pw input { padding-right:64px; }
.profile-form .check { display:flex; align-items:center; gap:8px; }
.profile-form .check input { width:auto; min-height:auto; margin:0; accent-color:var(--accent); }
.profile-form .req { color:var(--err-fg); }
.profile-form .field-err { display:block; margin-top:4px; font-size:12.5px; color:var(--err-fg); }
.profile-form .dialog-actions { margin-top:8px; }
@media (max-width:820px) {
  .profile-card { flex-direction:column; align-items:stretch; }
  .profile-actions { width:100%; }
  .profile-actions button { flex:1; }
}
</style>