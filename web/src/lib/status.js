// 状态枚举 → 中文 + tone 的兜底字典。
// 正常情况下后端已下发 {label, tone}（status_view / connection_view），
// 这里只用于后端版本较旧或字段缺失时兜底。
const SERVICE = {
  running: ['运行中', 'ok'], active: ['运行中', 'ok'], started: ['运行中', 'ok'],
  stopped: ['已停止', 'err'], inactive: ['已停止', 'err'], dead: ['已停止', 'err'],
  failed: ['异常', 'err'],
  starting: ['启动中', 'warn'], activating: ['启动中', 'warn'],
  stopping: ['停止中', 'warn'], deactivating: ['停止中', 'warn'],
  'not-installed': ['未安装', 'muted'], unknown: ['未知', 'warn'],
}
const CONNECTION = {
  connected: ['已连接', 'ok'],
  connecting: ['连接中', 'warn'],
  'auth-failed': ['认证失败', 'err'],
  'tls-failed': ['TLS 失败', 'err'],
  'config-error': ['配置错误', 'err'],
  'proxy-error': ['映射异常', 'err'],
  unreachable: ['不可达', 'err'],
  disconnected: ['已断开', 'err'],
  timeout: ['连接超时', 'warn'],
  'not-configured': ['未配置', 'warn'],
  stopped: ['已停止', 'err'],
  unknown: ['待确认', 'warn'],
}

export function view(raw, dict) {
  const key = String(raw || '').toLowerCase()
  const hit = dict[key]
  if (hit) return { raw, label: hit[0], tone: hit[1] }
  return { raw, label: raw || '未知', tone: 'warn' }
}
export const serviceView = raw => view(raw, SERVICE)
export const connectionView = raw => view(raw, CONNECTION)
export const TONE_ORDER = ['ok', 'warn', 'err', 'info', 'muted']
