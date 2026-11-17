export function hhmmss(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0))
  const m = Math.floor(s / 60)
  return String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0')
}

export function nowText() {
  const d = new Date()
  const p = n => String(n).padStart(2, '0')
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds())
}

// 外部访问地址：tcp/udp 用 服务器地址:远程端口，http/https 用域名
export function externalAddress(mapping, profile) {
  if (!mapping) return ''
  const type = String(mapping.type || '').toLowerCase()
  if (type === 'http' || type === 'https') {
    const domain = mapping.domain || ''
    return domain ? (type + '://' + domain) : ''
  }
  const host = (profile && profile.server_addr) || ''
  const port = mapping.remote_port
  return host && port ? (host + ':' + port) : ''
}
