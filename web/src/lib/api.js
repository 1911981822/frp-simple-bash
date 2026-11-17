// 统一请求封装：CSRF、401 跳登录、超时、统一错误
import { reactive } from 'vue'

const state = reactive({ csrf: '', username: '', expiresIn: 0 })

export function setSession(data) {
  if (data.csrf) state.csrf = data.csrf
  if (data.username) state.username = data.username
  if (typeof data.expires_in === 'number') state.expiresIn = data.expires_in
}
export function session() { return state }
export function clearSession() { state.csrf = ''; state.username = ''; state.expiresIn = 0 }

export class ApiError extends Error {
  constructor(status, payload) {
    super(payload.error || ('HTTP ' + status))
    this.status = status
    this.code = payload.code || ''
    this.hint = payload.hint || ''
    this.retryAfter = payload.retry_after || 0
    this.requestId = payload.request_id || ''
    this.details = payload.details || []
  }
}

// 相对 base 请求（支持子路径部署：由 <base> 或当前目录决定）
function endpoint(path) {
  const base = document.baseURI.replace(/[^/]*$/, '')
  return new URL(path.replace(/^\//, ''), base).toString()
}

let onUnauthorized = () => {}
export function setUnauthorizedHandler(fn) { onUnauthorized = fn }

export async function request(path, { method = 'GET', body, timeout = 20000 } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET' && method !== 'HEAD' && state.csrf) {
    headers['X-CSRF-Token'] = state.csrf
  }
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  let res
  try {
    res = await fetch(endpoint(path), {
      method, headers, credentials: 'same-origin', signal: controller.signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (err) {
    throw new ApiError(0, { error: err.name === 'AbortError' ? '请求超时' : '无法连接管理端',
                            hint: '请确认服务已启动：sudo systemctl status frp-simple-bash-web' })
  } finally {
    clearTimeout(timer)
  }

  let payload = {}
  const text = await res.text()
  if (text) {
    try { payload = JSON.parse(text) } catch (e) {
      throw new ApiError(res.status, {
        error: '响应格式异常（HTTP ' + res.status + '）',
        hint: '可能不是本管理端的地址，或反向代理路径不匹配',
      })
    }
  }
  if (!res.ok || payload.ok === false) {
    const error = new ApiError(res.status, payload)
    if (res.status === 401) onUnauthorized(error)
    throw error
  }
  return payload
}

export const get = (path, opts) => request(path, { ...opts, method: 'GET' })
export const post = (path, body, opts) => request(path, { ...opts, method: 'POST', body })

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch (e) { /* 落到下面的兼容实现 */ }
  try {
    const area = document.createElement('textarea')
    area.value = text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch (e) { return false }
}
