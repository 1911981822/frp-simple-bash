<template>
  <div class="page">
    <header class="page-head">
      <h2>端口映射</h2>
      <div class="page-tools">
        <label class="inline-check">
          <input type="checkbox" v-model="showAll" @change="load" />
          显示全部服务端
        </label>
        <span class="muted small">更新于 {{ updatedAt }}</span>
        <button class="ghost small" :disabled="loading" @click="load">刷新</button>
        <button :disabled="!activeProfile" @click="openCreate">新增映射</button>
      </div>
    </header>

    <ErrorState v-if="error" :error="error" :retry="load" />

    <div v-if="!activeProfile && !showAll" class="alert tone-warn">
      <div class="alert-head">
        <span>还没有选择服务端，无法管理映射</span>
        <router-link class="ghost small" to="/profiles">去选择服务端</router-link>
      </div>
    </div>

    <template v-if="!error">
      <!-- 工具条 -->
      <div class="toolbar" v-if="items.length">
        <input v-model="keyword" placeholder="搜索名称 / 本地端口 / 外部地址（按 / 聚焦）"
               ref="searchInput" />
        <select v-model="filter">
          <option value="all">全部状态</option>
          <option value="online">在线</option>
          <option value="offline">离线</option>
          <option value="disabled">已停用</option>
        </select>
      </div>

      <div v-for="err in errors" :key="err.profile" class="alert tone-err">
        <div class="alert-head"><span>「{{ err.profile }}」的映射读取失败：{{ err.error }}</span></div>
      </div>

      <EmptyState v-if="!items.length" icon="🔌" title="还没有端口映射"
                  desc="添加映射后，外部就能通过「服务器地址:远程端口」访问你的本地服务">
        <button :disabled="!activeProfile" @click="openCreate">新增映射</button>
      </EmptyState>

      <EmptyState v-else-if="!filtered.length" icon="🔍" title="没有匹配的映射"
                  desc="试试清除搜索或切换状态筛选" />

      <div v-else class="map-list">
        <div v-for="m in filtered" :key="m.profile_id + '/' + m.name" class="map-card"
             :class="{ disabled: !m.enabled }">
          <div class="map-main">
            <div class="map-title">
              <strong>{{ m.name }}</strong>
              <span class="type-tag">{{ m.type.toUpperCase() }}</span>
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

    <!-- 新增 / 编辑弹层 -->
    <div v-if="form.open" class="mask" @click.self="form.open = false">
      <div class="dialog" role="dialog" aria-modal="true">
        <h3>{{ form.editing ? '编辑映射' : '新增映射' }}</h3>
        <label>名称
          <input v-model="form.name" placeholder="如 ssh、web" autocomplete="off" />
        </label>
        <label>类型
          <select v-model="form.type">
            <option value="tcp">TCP</option>
            <option value="udp">UDP</option>
            <option value="http">HTTP</option>
            <option value="https">HTTPS</option>
          </select>
        </label>
        <div class="row2">
          <label>本地地址
            <input v-model="form.local_ip" placeholder="127.0.0.1" />
          </label>
          <label>本地端口
            <input v-model.number="form.local_port" type="number" min="1" max="65535" />
          </label>
        </div>
        <label v-if="form.type === 'tcp' || form.type === 'udp'">远程端口
          <input v-model.number="form.remote_port" type="number" min="1" max="65535" />
        </label>
        <label v-else>域名
          <input v-model="form.domain" placeholder="example.com" />
        </label>
        <label class="inline-check">
          <input type="checkbox" v-model="form.enabled" /> 启用此映射
        </label>
        <p v-if="form.error" class="err">{{ form.error }}</p>
        <div class="dialog-actions">
          <button class="ghost" @click="form.open = false">取消</button>
          <button :disabled="form.busy" @click="submitForm">
            {{ form.busy ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <ConfirmDialog :open="dialog.open" :title="dialog.title" :message="dialog.message"
                   :items="dialog.items" :danger="true" :busy="dialog.busy"
                   :require-text="dialog.requireText" :error="dialog.error"
                   @confirm="dialog.onConfirm" @cancel="dialog.open = false" />
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { get, post, request, copyText, ApiError } from '../lib/api.js'
import { nowText } from '../lib/format.js'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const items = ref([]); const errors = ref([]); const error = ref(null)
const loading = ref(false); const busy = ref(''); const copied = ref('')
const activeProfile = ref(null); const updatedAt = ref('')
const showAll = ref(false); const keyword = ref(''); const filter = ref('all')
const searchInput = ref(null)
const form = reactive({ open: false, editing: false, name: '', type: 'tcp',
                        local_ip: '127.0.0.1', local_port: null, remote_port: null,
                        domain: '', enabled: true, busy: false, error: '' })
const dialog = reactive({ open: false, title: '', message: '', items: [],
                          requireText: '', busy: false, error: '', onConfirm: () => {} })

function extAddr(m) {
  const p = activeProfile.value || { server_addr: '' }
  if (m.type === 'tcp' || m.type === 'udp') {
    const host = p.server_addr || ''
    return host && m.remote_port ? host + ':' + m.remote_port : ''
  }
  return m.domain ? m.domain : ''
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

function openCreate() {
  Object.assign(form, { open: true, editing: false, name: '', type: 'tcp',
    local_ip: '127.0.0.1', local_port: null, remote_port: null, domain: '',
    enabled: true, busy: false, error: '' })
}

function openEdit(m) {
  Object.assign(form, { open: true, editing: true, name: m.name, type: m.type,
    local_ip: m.local_ip, local_port: m.local_port, remote_port: m.remote_port || null,
    domain: m.domain || '', enabled: !!m.enabled, busy: false, error: '' })
}

async function submitForm() {
  form.busy = true; form.error = ''
  const body = {
    name: form.name, type: form.type, local_ip: form.local_ip,
    local_port: form.local_port, enabled: form.enabled,
  }
  if (form.type === 'tcp' || form.type === 'udp') body.remote_port = form.remote_port
  else body.domain = form.domain
  try {
    if (form.editing) await request('/api/mappings/' + encodeURIComponent(form.name),
                                    { method: 'PUT', body })
    else await post('/api/mappings', body)
    form.open = false
    await load()
  } catch (err) {
    form.error = err.message + (err.hint ? '（' + err.hint + '）' : '')
  } finally { form.busy = false }
}

async function toggle(m) {
  busy.value = m.name
  try { await post('/api/mappings/' + encodeURIComponent(m.name) + '/toggle'); await load() }
  catch (err) { alertOnPage(err) }
  finally { busy.value = '' }
}

function askDelete(m) {
  Object.assign(dialog, {
    open: true, title: '删除映射「' + m.name + '」？',
    message: '删除后外部访问地址将立即失效，此操作不可撤销。',
    items: [extAddr(m) ? '当前外部地址：' + extAddr(m) : '该映射尚无外部地址'],
    requireText: m.name, busy: false, error: '',
    onConfirm: () => doDelete(m),
  })
}

async function doDelete(m) {
  dialog.busy = true; dialog.error = ''
  try {
    await request('/api/mappings/' + encodeURIComponent(m.name),
                  { method: 'DELETE', body: { confirm: m.name } })
    dialog.open = false
    await load()
  } catch (err) {
    dialog.error = err.message + (err.hint ? '（' + err.hint + '）' : '')
  } finally { dialog.busy = false }
}

let pageError = ref('')
function alertOnPage(err) { pageError.value = err.message }

async function copy(m) {
  if (await copyText(extAddr(m))) {
    copied.value = m.name
    setTimeout(() => { if (copied.value === m.name) copied.value = '' }, 1500)
  }
}

function onKey(ev) {
  if (ev.key === '/' && document.activeElement !== searchInput.value) {
    ev.preventDefault(); if (searchInput.value) searchInput.value.focus()
  }
}

onMounted(() => { load(); window.addEventListener('keydown', onKey) })
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>
