<template>
  <div class="page">
    <header class="page-head">
      <h2>端口映射</h2>
      <div class="page-tools">
        <span class="muted small">更新于 {{ updatedAt }}</span>
        <button class="ghost small" :disabled="loading" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
        <button :disabled="!activeProfile" :title="activeProfile ? '' : '请先新建服务端'"
                @click="openCreate">新建映射</button>
      </div>
    </header>

    <!-- 当前服务端上下文：所有操作都作用在它上面，必须显式写出来 -->
    <div class="context" v-if="activeProfile">
      <span class="muted small">当前服务端</span>
      <strong>{{ activeProfile.name }}</strong>
      <code>{{ activeProfile.server_addr }}:{{ activeProfile.server_port }}</code>
      <router-link class="ghost small" to="/profiles">切换</router-link>
      <label class="inline-check">
        <input type="checkbox" v-model="showAll" @change="load" /> 显示全部服务端
      </label>
    </div>
    <div class="alert tone-warn" v-else>
      <div class="alert-head">
        <span>还没有选择服务端，无法管理映射</span>
        <router-link class="ghost small" to="/profiles">去新建服务端</router-link>
      </div>
    </div>

    <NoticeBar ref="notice" />

    <ErrorState v-if="error" :error="error" :retry="load" @retry="load" />

    <template v-if="!error">
      <div class="toolbar" v-if="items.length">
        <input v-model="keyword" ref="searchInput" aria-label="搜索映射"
               placeholder="搜索名称 / 本地端口 / 外部地址（按 / 聚焦）" />
        <select v-model="filter" aria-label="状态筛选">
          <option value="all">全部状态</option>
          <option value="online">在线</option>
          <option value="offline">离线</option>
          <option value="disabled">已停用</option>
        </select>
      </div>

      <!-- 读取失败时不能同时说"还没有映射"（否则用户会去点新建而不是修配置） -->
      <div v-for="errItem in errors" :key="errItem.profile" class="alert tone-err">
        <div class="alert-head">
          <span>「{{ errItem.profile }}」的映射读取失败：{{ errItem.error }}</span>
          <router-link class="ghost small" to="/settings">去恢复备份</router-link>
        </div>
      </div>

      <EmptyState v-if="!items.length && !errors.length" icon="🔌" title="还没有端口映射"
                  desc="添加映射后，外部就能通过「服务器地址:远程端口」访问你的本地服务">
        <button :disabled="!activeProfile" @click="openCreate">新建映射</button>
      </EmptyState>

      <EmptyState v-else-if="items.length && !filtered.length" icon="🔍" title="没有匹配的映射"
                  desc="试试清除搜索或切换状态筛选" />

      <div v-else-if="filtered.length" class="map-list">
        <div v-for="m in filtered" :key="m.profile_id + '/' + m.name" class="map-card"
             :class="{ disabled: !m.enabled, expose: isExpose(m) }">
          <div class="map-main">
            <div class="map-title">
              <strong>{{ m.name }}</strong>
              <span class="type-tag">{{ m.type.toUpperCase() }}</span>
              <StatusBadge v-if="isExpose(m)" label="管理端自映射" tone="warn" />
              <StatusBadge v-if="!m.enabled" label="已停用" tone="err" />
              <StatusBadge v-else-if="m.online === true" label="在线" tone="ok" />
              <StatusBadge v-else-if="m.online === false" label="离线" tone="err" />
              <StatusBadge v-else-if="m.enabled" label="状态未知" tone="muted" />
              <span v-if="showAll" class="muted small">· {{ m.profile_name }}</span>
            </div>
            <div class="map-route">
              <code>{{ m.local_ip }}:{{ m.local_port }}</code>
              <span class="arrow">→</span>
              <code v-if="m.type === 'tcp' || m.type === 'udp'">
                {{ extAddr(m) || ('远程端口 ' + m.remote_port) }}
              </code>
              <code v-else>{{ m.type }}://{{ m.domain }}</code>
              <button v-if="extAddr(m)" class="ghost small" @click="copy(m)">
                {{ copied === m.name ? '已复制 ✓' : '复制' }}
              </button>
            </div>
          </div>
          <div class="map-actions" v-if="m.is_active_profile">
            <button class="ghost small" :disabled="busy === m.name" @click="toggle(m)">
              {{ m.enabled ? '停用' : '启用' }}
            </button>
            <button class="ghost small" @click="openEdit(m)">编辑</button>
            <button class="ghost small danger-text" @click="askDelete(m)">删除</button>
          </div>
          <div class="map-actions" v-else>
            <span class="muted small">非当前服务端，只读</span>
          </div>
        </div>
      </div>
    </template>

    <!-- 新建 / 编辑弹层 -->
    <div v-if="form.open" class="mask" @click.self="closeForm">
      <div class="dialog form-dialog" role="dialog" aria-modal="true"
           aria-labelledby="map-form-title" tabindex="-1"
           @keydown.esc="closeForm">
        <h3 id="map-form-title">{{ form.editing ? '编辑映射' : '新建映射' }}</h3>
        <label class="field">名称
          <input ref="nameInput" v-model.trim="form.name" placeholder="如 ssh、web（字母/数字/-/_）"
                 autocomplete="off" :aria-invalid="!!form.fieldErrors.name" />
          <span v-if="form.fieldErrors.name" class="err small">{{ form.fieldErrors.name }}</span>
        </label>
        <label class="field">类型
          <select v-model="form.type">
            <option value="tcp">TCP</option>
            <option value="udp">UDP</option>
            <option value="http">HTTP</option>
            <option value="https">HTTPS</option>
          </select>
        </label>
        <div class="row2">
          <label class="field">本地地址
            <input v-model.trim="form.local_ip" placeholder="127.0.0.1"
                   :aria-invalid="!!form.fieldErrors.local_ip" />
            <span v-if="form.fieldErrors.local_ip" class="err small">{{ form.fieldErrors.local_ip }}</span>
          </label>
          <label class="field">本地端口
            <input v-model.number="form.local_port" type="number" min="1" max="65535"
                   inputmode="numeric" placeholder="如 22"
                   :aria-invalid="!!form.fieldErrors.local_port" />
            <span v-if="form.fieldErrors.local_port" class="err small">{{ form.fieldErrors.local_port }}</span>
          </label>
        </div>
        <label class="field" v-if="form.type === 'tcp' || form.type === 'udp'">远程端口
          <input v-model.number="form.remote_port" type="number" min="1" max="65535"
                 inputmode="numeric" placeholder="如 6000（服务端对外开放的端口）"
                 :aria-invalid="!!form.fieldErrors.remote_port" />
          <span v-if="form.fieldErrors.remote_port" class="err small">{{ form.fieldErrors.remote_port }}</span>
        </label>
        <label class="field" v-else>域名
          <input v-model.trim="form.domain" placeholder="example.com"
                 :aria-invalid="!!form.fieldErrors.domain" />
          <span v-if="form.fieldErrors.domain" class="err small">{{ form.fieldErrors.domain }}</span>
        </label>
        <label class="inline-check">
          <input type="checkbox" v-model="form.enabled" /> 启用此映射
        </label>
        <p v-if="form.error" class="err" role="alert">{{ form.error }}</p>
        <div class="dialog-actions">
          <button class="ghost" :disabled="form.busy" @click="closeForm">取消</button>
          <button :disabled="form.busy" @click="submitForm">
            {{ form.busy ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <ConfirmDialog :open="dialog.open" :title="dialog.title" :message="dialog.message"
                   :items="dialog.items" :danger="dialog.danger" :busy="dialog.busy"
                   :require-text="dialog.requireText" :error="dialog.error"
                   @confirm="dialog.onConfirm" @cancel="dialog.open = false" />
  </div>
</template>
<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { get, post, request, copyText, ApiError } from '../lib/api.js'
import { nowText } from '../lib/format.js'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import NoticeBar from '../components/NoticeBar.vue'

const NAME_RE = /^[A-Za-z0-9_-]{1,64}$/

const items = ref([]); const errors = ref([]); const error = ref(null)
const loading = ref(false); const busy = ref(''); const copied = ref('')
const activeProfile = ref(null); const updatedAt = ref('')
const showAll = ref(false); const keyword = ref(''); const filter = ref('all')
const searchInput = ref(null); const nameInput = ref(null); const notice = ref(null)
const form = reactive({ open: false, editing: false, originalName: '', name: '', type: 'tcp',
                        local_ip: '127.0.0.1', local_port: null, remote_port: null,
                        domain: '', enabled: true, busy: false, error: '', fieldErrors: {} })
const dialog = reactive({ open: false, title: '', message: '', items: [],
                          requireText: '', busy: false, error: '', danger: true,
                          onConfirm: () => {} })

function isExpose(m) {
  return String(m.name).startsWith('_webui-')
}

function extAddr(m) {
  // 非当前服务端的行不能拼当前服务端的地址，否则复制出来是错的主机
  if (!m.is_active_profile) return ''
  const host = (activeProfile.value && activeProfile.value.server_addr) || ''
  if (m.type === 'tcp' || m.type === 'udp') {
    return host && m.remote_port ? host + ':' + m.remote_port : ''
  }
  return m.domain || ''
}

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return items.value.filter(m => {
    if (filter.value === 'online' && m.online !== true) return false
    if (filter.value === 'offline' && m.online !== false) return false
    if (filter.value === 'disabled' && m.enabled) return false
    if (!kw) return true
    const hay = [m.name, m.local_port, m.remote_port, m.domain, extAddr(m)]
      .filter(Boolean).join(' ').toLowerCase()
    return hay.includes(kw)
  })
})

async function load() {
  loading.value = true; error.value = null
  try {
    const data = await get('/api/mappings' + (showAll.value ? '?all=1' : ''))
    items.value = data.items || []
    errors.value = data.errors || []
    activeProfile.value = data.active_profile || null
    updatedAt.value = nowText()
  } catch (err) {
    error.value = err instanceof ApiError ? err : new ApiError(0, { error: String(err) })
  } finally { loading.value = false }
}

function validate() {
  const fe = {}
  if (!form.name) fe.name = '名称不能为空'
  else if (!NAME_RE.test(form.name)) fe.name = '只允许 1-64 个字母、数字、-、_（不支持中文和空格）'
  else if (!form.editing && existsName(form.name)) fe.name = '名称已存在'
  else if (form.editing && form.name !== form.originalName && existsName(form.name)) fe.name = '名称已存在'
  if (!form.local_ip) fe.local_ip = '不能为空'
  for (const [key, label] of [['local_port', '本地端口'], ['remote_port', '远程端口']]) {
    if (key === 'remote_port' && !(form.type === 'tcp' || form.type === 'udp')) continue
    const value = form[key]
    if (value === null || value === '' || value === undefined) fe[key] = label + '不能为空'
    else if (!Number.isInteger(Number(value)) || Number(value) < 1 || Number(value) > 65535)
      fe[key] = label + '必须是 1-65535 的整数'
  }
  if (form.type === 'tcp' || form.type === 'udp') {
    const dup = items.value.find(m => m.is_active_profile && m.name !== form.originalName
      && (m.type === 'tcp' || m.type === 'udp')
      && String(m.type) === form.type && Number(m.remote_port) === Number(form.remote_port))
    if (dup && !fe.remote_port) fe.remote_port = '与映射「' + dup.name + '」的远程端口重复'
  } else if (!form.domain) {
    fe.domain = 'HTTP/HTTPS 必须填写域名'
  }
  form.fieldErrors = fe
  return Object.keys(fe).length === 0
}

function existsName(name) {
  return items.value.some(m => m.is_active_profile && m.name !== form.originalName
    && m.name === name)
}

function openCreate() {
  Object.assign(form, { open: true, editing: false, originalName: '', name: '', type: 'tcp',
    local_ip: '127.0.0.1', local_port: null, remote_port: null, domain: '',
    enabled: true, busy: false, error: '', fieldErrors: {} })
  nextTick(() => nameInput.value && nameInput.value.focus())
}

function openEdit(m) {
  Object.assign(form, { open: true, editing: true, originalName: m.name, name: m.name,
    type: m.type, local_ip: m.local_ip, local_port: m.local_port,
    remote_port: m.remote_port || null, domain: m.domain || '', enabled: !!m.enabled,
    busy: false, error: '', fieldErrors: {} })
  nextTick(() => nameInput.value && nameInput.value.focus())
}

function closeForm() {
  if (form.busy) return          // 保存中不允许关闭，否则失败原因会丢
  form.open = false
}

async function submitForm() {
  form.error = ''
  if (!validate()) return
  form.busy = true
  const body = { name: form.name, type: form.type, local_ip: form.local_ip,
                 local_port: form.local_port, enabled: form.enabled }
  if (form.type === 'tcp' || form.type === 'udp') body.remote_port = form.remote_port
  else body.domain = form.domain
  try {
    if (form.editing) {
      await request('/api/mappings/' + encodeURIComponent(form.originalName),
                    { method: 'PUT', body })
    } else {
      await post('/api/mappings', body)
    }
    form.open = false
    notice.value?.ok(form.editing ? '映射已更新。' : '映射已创建。',
                     '若 frpc 正在运行，需要重启后生效')
    await load()
  } catch (err) {
    // 后端可能返回字段级错误
    if (err && err.details && err.details.length) {
      const fe = {}
      err.details.forEach(d => { if (d.field) fe[d.field] = d.message })
      form.fieldErrors = fe
    }
    form.error = err.message + (err.hint ? '（' + err.hint + '）' : '')
  } finally { form.busy = false }
}

async function toggle(m) {
  busy.value = m.name
  try {
    await post('/api/mappings/' + encodeURIComponent(m.name) + '/toggle')
    notice.value?.ok(m.enabled ? '已停用「' + m.name + '」。' : '已启用「' + m.name + '」。')
    await load()
  } catch (err) { notice.value?.err(err) }
  finally { busy.value = '' }
}

function askDelete(m) {
  Object.assign(dialog, {
    open: true, title: '删除映射「' + m.name + '」？',
    message: '删除后外部访问地址将立即失效，此操作不可撤销。',
    items: [extAddr(m) ? '当前外部地址：' + extAddr(m) : '该映射尚无外部地址'],
    requireText: m.name, busy: false, error: '', danger: true,
    onConfirm: () => doDelete(m),
  })
}

async function doDelete(m) {
  dialog.busy = true; dialog.error = ''
  try {
    await request('/api/mappings/' + encodeURIComponent(m.name),
                  { method: 'DELETE', body: { confirm: m.name } })
    dialog.open = false
    notice.value?.ok('已删除映射「' + m.name + '」。')
    await load()
  } catch (err) {
    dialog.error = err.message + (err.hint ? '（' + err.hint + '）' : '')
  } finally { dialog.busy = false }
}

async function copy(m) {
  if (await copyText(extAddr(m))) {
    copied.value = m.name
    setTimeout(() => { if (copied.value === m.name) copied.value = '' }, 1500)
  }
}

function onKey(ev) {
  if (ev.key !== '/') return
  const tag = (ev.target && ev.target.tagName || '').toLowerCase()
  const typing = ['input', 'textarea', 'select'].includes(tag)
    || (ev.target && ev.target.isContentEditable)
  // 弹层打开或在输入框里打字时，不能抢焦点（否则域名里的 / 会被吞掉）
  if (typing || form.open || dialog.open) return
  ev.preventDefault()
  if (searchInput.value) searchInput.value.focus()
}

onMounted(() => { load(); window.addEventListener('keydown', onKey) })
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>
<style scoped>
.context { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 8px 12px; margin-bottom: 12px; }
.form-dialog { max-height: calc(100dvh - 32px); overflow: auto; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--muted); }
</style>
