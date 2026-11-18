#!/usr/bin/env python3
"""frpc Web 管理端后端。

设计原则：
1. 只用 Python 标准库（与项目"零依赖"定位一致）。
2. 业务逻辑全部复用 manager.py 的现有函数，绝不复制一份逻辑，
   否则 CLI 与 Web 会随时间漂移。
3. 默认只监听 127.0.0.1；改绑非回环地址会强警告。
4. 改端口/绑定/凭据时保留"逃生窗口"，详见 docs/Web管理端需求与方案.md §9。
"""
import base64
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import re
import secrets
import signal
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
import manager  # noqa: E402  （必须在 sys.path 调整之后）


def bind_manager(module):
    """由 manager.py 调用，注入"正在运行的那一份"manager 模块。

    manager.py 既可能以 __main__ 运行（CLI），也可能被 import。
    若这里再 import 一次，会得到第二份模块实例（缓存/异常类/全局状态各一份），
    调试时极难定位，所以由调用方把真实模块传进来。
    """
    global manager, ConfigCorruptError
    manager = module
    # manager 以 __main__ 运行时，这里必须同步换成"那一份"的异常类，
    # 否则 except ConfigCorruptError 永远匹配不上，损坏配置会被当成 400 用户输入错误
    ConfigCorruptError = module.ConfigCorruptError

WEB_CONFIG_NAME = 'web.json'
ConfigCorruptError = manager.ConfigCorruptError
SESSION_COOKIE = 'frpweb'
CSRF_HEADER = 'X-CSRF-Token'
ESCAPE_WINDOW_SECONDS = 300
MAX_BODY_BYTES = 1024 * 1024      # 单个请求体上限 1MB

DEFAULT_CONFIG = {
    'schema_version': 1,
    'bind': '127.0.0.1',
    'port': 7501,
    'username': 'admin',
    'password_hash': '',
    'idle_timeout_minutes': 30,
    'login_max_failures': 5,
    'login_lock_minutes': 15,
    'trusted_proxies': [],       # 仅这些来源的 X-Forwarded-For 才采信
}


# ---------------------------------------------------------------- 配置

def web_config_path():
    return manager.ROOT / WEB_CONFIG_NAME


def load_web_config():
    """读取 web.json，缺失字段填默认值（容错，不让坏配置炸掉启动）。

    配置**存在但无法解析**是严重状态：静默回落到默认值会让 password_hash 变成
    空，面板从此任何密码都登不进，且没有任何提示。这里标记 _corrupt，
    由 ensure_web_config 自动修复（重新生成一次性密码）并写审计。
    """
    path = web_config_path()
    data = {}
    corrupt = False
    if path.exists():
        try:
            raw = path.read_text(encoding='utf-8')
            data = json.loads(raw)
            if not isinstance(data, dict):
                data, corrupt = {}, True
        except (ValueError, OSError):
            data, corrupt = {}, True
    if not isinstance(data, dict):
        data = {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(data)                    # 保留未知键，避免 schema 演进时被静默丢弃
    try:
        cfg['port'] = int(cfg['port'])
    except (TypeError, ValueError):
        cfg['port'] = DEFAULT_CONFIG['port']
    if not 1 <= cfg['port'] <= 65535:
        cfg['port'] = DEFAULT_CONFIG['port']
        cfg['_port_invalid'] = True
    for key in ('idle_timeout_minutes', 'login_max_failures', 'login_lock_minutes'):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            cfg[key] = DEFAULT_CONFIG[key]
    # 历史版本写入的 session_secret 从未参与任何签名（会话是服务端内存表 +
    # 不透明随机 token），保留只会误导审计，这里顺手清掉
    cfg.pop('session_secret', None)
    if corrupt:
        cfg['_corrupt'] = True
    return cfg


def save_web_config(cfg):
    """原子写入 web.json（0600），并保留上一份为 .bak 作为回滚点。"""
    path = web_config_path()
    if path.exists():
        bak = path.parent / (WEB_CONFIG_NAME + '.bak')
        try:
            manager.atomic(bak, path.read_text(encoding='utf-8'))
            os.chmod(str(bak), 0o600)
        except (OSError, ValueError):
            pass
    # 不写内部键（_ 前缀）与已废弃的死字段，避免旧数据被反复写回磁盘
    payload = {k: v for k, v in cfg.items()
               if not str(k).startswith('_') and k != 'session_secret'}
    manager.atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def hash_password(password, iterations=200000):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    return 'pbkdf2_sha256$%d$%s$%s' % (iterations, salt, digest.hex())


def verify_password(password, stored):
    try:
        algo, iterations, salt, expected = str(stored).split('$', 3)
    except (ValueError, AttributeError):
        return False
    if algo != 'pbkdf2_sha256':
        return False
    try:
        iterations = int(iterations)
    except (TypeError, ValueError):
        return False
    if not 1000 <= iterations <= 2000000:
        return False
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    return hmac.compare_digest(digest.hex(), expected)


def ensure_web_config():
    """确保 web.json 存在；首次创建或损坏修复时生成随机密码并返回它。"""
    path = web_config_path()
    if path.exists():
        cfg = load_web_config()
        if not cfg.get('_corrupt') and cfg.get('password_hash'):
            return cfg, None
        # 配置损坏 / 凭据丢失：重新生成口令并落盘，否则面板永远登不进
        password = secrets.token_urlsafe(12)
        repaired = dict(DEFAULT_CONFIG)
        repaired['password_hash'] = hash_password(password)
        repaired['must_change_password'] = True
        repaired.pop('_corrupt', None)
        save_web_config(repaired)
        web_audit('config-repaired', result='warn',
                  detail='web.json 损坏或缺少凭据，已重建并生成新密码')
        return load_web_config(), password
    password = secrets.token_urlsafe(12)
    cfg = dict(DEFAULT_CONFIG)
    cfg['password_hash'] = hash_password(password)
    cfg['must_change_password'] = True   # 首次登录后应引导改密
    save_web_config(cfg)
    return cfg, password


# ---------------------------------------------------------------- 审计

def web_audit(action, client_ip='', username='', result='ok', detail=''):
    record = {'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'action': action,
              'ip': client_ip, 'user': username, 'result': result,
              'detail': str(detail)[:200]}
    try:
        path = manager.ROOT / 'logs' / 'web-audit.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        if os.name != 'nt':
            try: os.chmod(str(path), 0o600)
            except OSError: pass
    except OSError:
        pass


# ---------------------------------------------------------------- 会话与限流

class SessionStore(object):
    """内存会话表（进程重启即失效，符合"重启后以磁盘配置为准"的设计）。"""

    def __init__(self, idle_minutes=30):
        self._lock = threading.Lock()
        self._items = {}
        self._idle = max(1, int(idle_minutes)) * 60

    def create(self, username):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            # 顺手清理过期会话，避免长期运行内存无界增长
            for key in [k for k, v in self._items.items()
                        if now - v['seen'] > self._idle]:
                self._items.pop(key, None)
            self._items[token] = {'user': username, 'csrf': csrf, 'seen': now}
        return token, csrf

    def get(self, token):
        if not token: return None
        with self._lock:
            item = self._items.get(token)
            if not item: return None
            if time.time() - item['seen'] > self._idle:
                self._items.pop(token, None)
                return None
            item['seen'] = time.time()
            return item

    def drop(self, token):
        with self._lock:
            self._items.pop(token, None)

    def clear_except(self, keep_token):
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if k == keep_token}

    def remaining(self, token):
        """会话剩余秒数（前端显示倒计时用）。"""
        with self._lock:
            item = self._items.get(token)
            if not item: return 0
            return max(0, int(self._idle - (time.time() - item['seen'])))


class LoginLimiter(object):
    """按 IP 的登录失败计数与锁定。"""

    def __init__(self, max_failures=5, lock_minutes=15):
        self._lock = threading.Lock()
        self._state = {}
        self.max_failures = max(1, int(max_failures))
        self.lock_seconds = max(1, int(lock_minutes)) * 60

    def locked(self, ip):
        with self._lock:
            item = self._state.get(ip)
            if not item: return 0
            remaining = item.get('until', 0) - time.time()
            return int(remaining) if remaining > 0 else 0

    def fail(self, ip):
        with self._lock:
            now = time.time()
            # 只保留仍在锁定中或最近有失败的记录，防止大量不同 IP 撑爆内存
            if len(self._state) > 1024:
                for key in [k for k, v in self._state.items()
                            if v.get('until', 0) < now and not v.get('count')]:
                    self._state.pop(key, None)
            item = self._state.setdefault(ip, {'count': 0, 'until': 0})
            item['count'] += 1
            if item['count'] >= self.max_failures:
                item['until'] = time.time() + self.lock_seconds
                item['count'] = 0
            return item['count']

    def reset(self, ip):
        with self._lock:
            self._state.pop(ip, None)


# ---------------------------------------------------------------- 请求上下文

class HttpError(Exception):
    """带"原因 + 建议"的错误（与终端 result_error 的四要素保持一致）。"""

    def __init__(self, status, message, code=None, hint='', retry_after=None,
                 details=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        # code 统一为字符串，前端可以稳定 switch（避免 401 与 'E401' 混用）
        self.code = str(code) if code is not None else 'E%d' % status
        self.hint = hint
        self.retry_after = retry_after
        self.details = details or []


def _ip_in_trusted(ip, trusted):
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in trusted:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _resolve_client_ip(app, handler):
    """解析真实客户端 IP。

    只有**直连来源在 trusted_proxies 内**时才采信 X-Forwarded-For，
    否则任何人都能伪造 XFF 绕过限流。
    """
    peer = handler.client_address[0] if handler.client_address else ''
    trusted = app.config.get('trusted_proxies') or []
    if not trusted or not _ip_in_trusted(peer, trusted):
        return peer
    forwarded = handler.headers.get('X-Forwarded-For', '') or ''
    parts = [p.strip() for p in forwarded.split(',') if p.strip()]
    # 从右往左找第一个"非可信代理"的地址（最靠近真实客户端的那一跳）
    for candidate in reversed(parts):
        if not _ip_in_trusted(candidate, trusted):
            return candidate
    return parts[0] if parts else peer


class RequestContext(object):
    def __init__(self, app, handler, method, path, query, body, headers):
        self.app = app
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers
        self.client_ip = _resolve_client_ip(app, handler)
        self.session = None
        self.session_token = ''

    def param(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    def json_body(self):
        if not self.body: return {}
        try:
            data = json.loads(self.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, '请求体不是合法 JSON')
        if not isinstance(data, dict):
            raise HttpError(400, '请求体必须是 JSON 对象')
        return data


# ---------------------------------------------------------------- 应用

class WebApp(object):
    """持有配置、会话、限流器与路由表。"""

    def __init__(self, config):
        self.config = config
        self.sessions = SessionStore(config.get('idle_timeout_minutes', 30))
        self.limiter = LoginLimiter(config.get('login_max_failures', 5),
                                    config.get('login_lock_minutes', 15))
        self.listeners = []          # [(server, thread, label)]
        self.escape = None           # 逃生窗口状态
        self.current_listener = None # 当前对外提供服务的监听（切换时更新）
        self.escape_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.dist_dir = _LIB_DIR.parent / 'web' / 'dist'

    # -- 会话辅助 --

    def current_session(self, ctx):
        cookie = self._parse_cookie(ctx)
        token = cookie.get(SESSION_COOKIE, '')
        session = self.sessions.get(token)
        ctx.session = session
        ctx.session_token = token if session else ''
        return session

    @staticmethod
    def _parse_cookie(ctx):
        raw = ctx.headers.get('Cookie', '')
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return {}
        return {k: v.value for k, v in jar.items()}

    def escape_summary(self):
        """当前逃生窗口状态（无窗口时返回 None）。"""
        with self.escape_lock:
            if not self.escape: return None
            remaining = int(self.escape.get('until', 0) - time.time())
            if remaining <= 0: return None
            return {'old': self.escape.get('old'), 'new': self.escape.get('new'),
                    'seconds_left': remaining}

    def require_auth(self, ctx):
        session = self.current_session(ctx)
        if not session:
            raise HttpError(401, '未登录或会话已过期', 'E401')
        if ctx.method not in ('GET', 'HEAD'):
            token = ctx.headers.get(CSRF_HEADER, '')
            try:
                token_ok = bool(token) and token.isascii() \
                    and hmac.compare_digest(token, session['csrf'])
            except (TypeError, ValueError):
                token_ok = False
            if not token_ok:
                web_audit('csrf-failed', ctx.client_ip,
                          session['user'] if session else '', 'failed')
                raise HttpError(403, 'CSRF 校验失败，请刷新页面重试', 'E_CSRF',
                                hint='页面可能已过期，刷新后重试')
        return session


# ---------------------------------------------------------------- 响应辅助

_PATH_RE = re.compile(r'(/[^\s\'"\):]+){2,}')


def _sanitize(text):
    """把错误文本里的绝对路径换成占位符，避免泄露部署结构。"""
    s = str(text)
    for prefix in (str(manager.ROOT), str(manager.INSTALL_ROOT)):
        if prefix and prefix != '/':
            s = s.replace(prefix, '<配置目录>' if prefix == str(manager.ROOT) else '<安装目录>')
    s = _PATH_RE.sub('<路径>', s)
    return s[:300]


def _common_headers(handler, extra=None):
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.send_header('Cache-Control', 'no-store')
    try:
        if handler.connection.cipher():
            handler.send_header('Strict-Transport-Security', 'max-age=31536000')
    except (AttributeError, ValueError):
        pass
    for key, value in (extra or {}).items():
        handler.send_header(key, value)


def _security_headers(handler):
    """静态资源的安全响应头（200 与 304 都要带）。"""
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.send_header('Content-Security-Policy',
                        "default-src 'self'; img-src 'self' data:; "
                        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                        "connect-src 'self'; frame-ancestors 'none'")


def send_json(handler, payload, status=200, extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    _common_headers(handler, extra_headers)
    handler.end_headers()
    if getattr(handler, 'command', '') == 'HEAD':
        return                       # HEAD 不能带 body，否则污染后续响应
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def ok(handler, data=None, **extra):
    payload = {'ok': True, 'code': 0, 'error': None}
    if data is not None: payload.update(data if isinstance(data, dict) else {'data': data})
    payload.update(extra)
    send_json(handler, payload)


def fail(handler, status, message, code=None, hint='', retry_after=None,
         details=None, request_id=''):
    payload = {'ok': False, 'code': str(code) if code is not None else 'E%d' % status,
               'error': message}
    if hint: payload['hint'] = hint
    if retry_after: payload['retry_after'] = int(retry_after)
    if details: payload['details'] = details
    payload['request_id'] = request_id or secrets.token_hex(6)
    send_json(handler, payload, status, extra_headers={'X-Request-Id': payload['request_id']})


# ---------------------------------------------------------------- API 处理

_SERVICE_TONE = {
    'running': ('ok', ), 'active': ('ok', ), 'started': ('ok', ),
    'stopped': ('err', ), 'inactive': ('err', ), 'dead': ('err', ),
    'failed': ('err', ),
    'starting': ('warn', ), 'activating': ('warn', ),
    'stopping': ('warn', ), 'deactivating': ('warn', ),
    'not-installed': ('muted', ), 'unknown': ('warn', ),
}
_CONNECTION_TONE = {
    'connected': 'ok', 'connecting': 'warn',
    'auth-failed': 'err', 'tls-failed': 'err', 'config-error': 'err',
    'proxy-error': 'err', 'unreachable': 'err', 'disconnected': 'err',
    'timeout': 'warn', 'not-configured': 'warn', 'stopped': 'err',
    'unknown': 'warn',
}

def _state_view(raw, table):
    """把后台枚举转成 {raw, label, tone}，前端只负责展示。"""
    key = str(raw or '').lower()
    label = manager.SERVICE_STATES.get(key) or manager.CONNECTION_STATES.get(key) or raw or '未知'
    if isinstance(table.get(key), tuple):
        tone = table[key][0]
    else:
        tone = table.get(key, 'warn')
    return {'raw': raw, 'label': label, 'tone': tone}


def api_health(ctx):
    """公开接口：前端登录页据此判断是否首次使用。"""
    cfg = ctx.app.config
    # 首次运行或凭据丢失（password_hash 为空）都要提示，否则用户不知道密码从哪来
    first_run = bool(cfg.get('must_change_password')) or not cfg.get('password_hash')
    return {'status': 'ok', 'first_run': first_run,
            'config_corrupt': bool(cfg.get('_corrupt'))}





def api_session(ctx):
    session = ctx.app.require_auth(ctx)
    return {'username': session['user'], 'csrf': session['csrf'],
            'expires_in': ctx.app.sessions.remaining(ctx.session_token)}


def _is_https(ctx):
    """当前请求是否走 HTTPS（含反向代理终止 TLS 的情况）。"""
    proto = (ctx.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
    if proto:
        return proto == 'https'
    try:
        return bool(ctx.handler.connection.cipher())
    except (AttributeError, ValueError):
        return False


def api_login(ctx):
    data = ctx.json_body()
    username = str(data.get('username', ''))
    password = str(data.get('password', ''))
    app = ctx.app
    locked = app.limiter.locked(ctx.client_ip)
    if locked:
        raise HttpError(429, '登录失败次数过多，已临时锁定',
                        'E_AUTH_LOCKED', hint='等待倒计时结束，或重启 Web 服务立即解除',
                        retry_after=locked)
    if not username or not password:
        raise HttpError(400, '用户名和密码不能为空', 'E_INVALID',
                        details=[{'field': 'username' if not username else 'password',
                                  'message': '不能为空'}])
    cfg = app.config
    # 先算密码再比用户名：用户名错误时也付出同样的 PBKDF2 代价，
    # 避免通过响应时间判断"账号是否存在"
    password_ok = verify_password(password, cfg.get('password_hash'))
    if not password_ok:
        # 改密后的宽限期：旧密码在 5 分钟内仍可用（防止记错新密码被锁死）
        old_hash = cfg.get('password_hash_old') or ''
        expires = cfg.get('password_expires_at') or 0
        if old_hash and time.time() < float(expires or 0):
            password_ok = verify_password(password, old_hash)
    username_ok = (username == cfg.get('username'))
    if not (username_ok and password_ok):
        app.limiter.fail(ctx.client_ip)
        web_audit('login', ctx.client_ip, username[:64], 'failed')
        # 本次失败刚好触发锁定 → 直接返回 429，不要让用户再试一次才发现被锁
        newly_locked = app.limiter.locked(ctx.client_ip)
        if newly_locked:
            raise HttpError(429, '登录失败次数过多，已临时锁定', 'E_AUTH_LOCKED',
                            hint='等待倒计时结束，或重启 Web 服务立即解除',
                            retry_after=newly_locked)
        raise HttpError(401, '用户名或密码错误', 'E_AUTH_FAILED',
                        hint='连续失败 %d 次将锁定 %d 分钟'
                             % (cfg.get('login_max_failures', 5),
                                cfg.get('login_lock_minutes', 15)))
    app.limiter.reset(ctx.client_ip)
    token, csrf = app.sessions.create(username)
    web_audit('login', ctx.client_ip, username, 'ok')
    ctx.handler.send_response(200)
    ctx.handler.send_header('Content-Type', 'application/json; charset=utf-8')
    secure = _is_https(ctx) or (ctx.app.config.get('cookie_secure') is True)
    cookie = '%s=%s; Path=/; HttpOnly; SameSite=Strict%s' % (
        SESSION_COOKIE, token, '; Secure' if secure else '')
    ctx.handler.send_header('Set-Cookie', cookie)
    body = json.dumps({'ok': True, 'code': 0, 'error': None,
                       'username': username, 'csrf': csrf,
                       'expires_in': app.sessions.remaining(token)}).encode('utf-8')
    ctx.handler.send_header('Content-Length', str(len(body)))
    _common_headers(ctx.handler)
    ctx.handler.end_headers()
    ctx.handler.wfile.write(body)


def api_logout(ctx):
    token = ctx.session_token
    if token:
        ctx.app.sessions.drop(token)
        web_audit('logout', ctx.client_ip, ctx.session['user'] if ctx.session else '')
    # 同时让浏览器丢弃 Cookie，否则残留的无效 Cookie 会一直带着
    ctx.handler.send_response(200)
    ctx.handler.send_header('Content-Type', 'application/json; charset=utf-8')
    ctx.handler.send_header(
        'Set-Cookie', '%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict' % SESSION_COOKIE)
    body = json.dumps({'ok': True, 'code': 0, 'error': None,
                       'logged_out': True}).encode('utf-8')
    ctx.handler.send_header('Content-Length', str(len(body)))
    _common_headers(ctx.handler)
    ctx.handler.end_headers()
    ctx.handler.wfile.write(body)


def api_status(ctx):
    session = ctx.app.require_auth(ctx)
    data = manager.status('frpc')
    conn_raw = (data.get('connection') or {}).get('state')
    return {
        'service_state': data.get('service_state'),
        'service_view': _state_view(data.get('service_state'), _SERVICE_TONE),
        'connection_state': conn_raw,
        'connection_view': _state_view(conn_raw, _CONNECTION_TONE),
        'version': data.get('version'),
        'pending': bool(data.get('pending')),
        'manager_version': manager.installed_manager_version(),
        'active_profile': _profile_brief(data.get('active_profile')),
        'profile_error': (_sanitize(data.get('profile_error'))
                          if data.get('profile_error') else None),
        'concerns': manager.status_concerns('frpc', data),
        'session': {'user': session['user'],
                    'expires_in': ctx.app.sessions.remaining(ctx.session_token)},
    }


def _find_expose_mapping(profile, items):
    """找出指向本管理端的自映射（_webui-tcp / _webui-http）。"""
    if not isinstance(profile, dict): return None
    try:
        cfg = load_web_config()
        port = int(cfg.get('port') or 7501)
    except (TypeError, ValueError, ValueError):
        port = 7501
    for item in items or []:
        if str(item.get('name')) not in (EXPOSE_TCP_NAME, EXPOSE_HTTP_NAME): continue
        try:
            if int(item.get('local_port') or 0) != port: continue
        except (TypeError, ValueError):
            continue
        return {'name': item.get('name'), 'type': item.get('type'),
                'remote_port': item.get('remote_port'), 'domain': item.get('domain'),
                'server_addr': profile.get('server_addr'),
                'external': ('%s:%s' % (profile.get('server_addr'), item.get('remote_port'))
                             if item.get('type') in ('tcp', 'udp')
                             else '%s://%s' % (item.get('type'), item.get('domain')))}
    return None


def _profile_brief(profile):
    if not isinstance(profile, dict): return None
    return {'id': profile.get('id'), 'name': profile.get('name'),
            'server_addr': profile.get('server_addr'),
            'server_port': profile.get('server_port')}


def api_service(ctx, action):
    """服务控制：start / stop / restart。

    restart 支持 {mode:'detached'} —— 用户在映射隧道里操作时，
    立即重启会切断自己的连接，改由脱离会话的进程延迟重启。
    """
    ctx.app.require_auth(ctx)
    if action == 'start':
        manager.require_active()
    if action == 'restart':
        data = ctx.json_body() if ctx.body else {}
        mode = str(data.get('mode', 'now')).lower()
        if mode == 'detached':
            manager.schedule_detached_restart('frpc', delay=2)
            web_audit('service-restart', ctx.client_ip, ctx.session['user'], 'scheduled')
            return {'action': 'restart', 'mode': 'detached',
                    'message': '已在后台计划重启，当前连接若经映射会短暂断开'}
    with ctx.app.write_lock:
        manager.service('frpc', action)
    web_audit('service-' + action, ctx.client_ip, ctx.session['user'], 'ok')
    state = manager.status('frpc')
    conn = (state.get('connection') or {}).get('state')
    return {'action': action,
            'service_state': state.get('service_state'),
            'service_view': _state_view(state.get('service_state'), _SERVICE_TONE),
            'connection_view': _state_view(conn, _CONNECTION_TONE)}


def api_service_start(ctx): return api_service(ctx, 'start')
def api_service_stop(ctx): return api_service(ctx, 'stop')
def api_service_restart(ctx): return api_service(ctx, 'restart')


def api_overview(ctx):
    ctx.app.require_auth(ctx)
    data = manager.status('frpc')
    profile = data.get('active_profile')
    mappings, mappings_error = [], None
    if isinstance(profile, dict):
        try:
            mappings = manager.mappings(profile['id'])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # 读取失败绝不能伪装成"没有映射"，否则用户会去点"新增"而不是"修权限"
            mappings_error = str(exc)
    enabled = [m for m in mappings if m.get('enabled', True)]
    # 在线状态来自 frpc 本地 admin API（读不到时整体为 None，前端显示"未知"而非假 0）
    # _proxy_status_map 返回 None 表示"读不到"（frpc 未运行），此时应显示未知而非 0
    online_map = None
    if isinstance(profile, dict):
        try:
            online_map = manager.mapping_online_status(profile['id'])
        except (ValueError, OSError, KeyError, TypeError):
            online_map = None
    online_count = (sum(1 for m in enabled if online_map.get(str(m.get('name'))))
                    if online_map is not None else None)
    conn_raw = (data.get('connection') or {}).get('state')
    return {
        'service_state': data.get('service_state'),
        'service_view': _state_view(data.get('service_state'), _SERVICE_TONE),
        'connection_state': conn_raw,
        'connection_view': _state_view(conn_raw, _CONNECTION_TONE),
        'version': data.get('version'),
        'manager_version': manager.installed_manager_version(),
        'pending': bool(data.get('pending')),
        'concerns': manager.status_concerns('frpc', data),
        'mappings_error': mappings_error,
        'active_profile': _profile_brief(profile),
        'counts': {'total': len(mappings), 'enabled': len(enabled),
                   'online': online_count,
                   'offline': (len(enabled) - online_count) if online_count is not None else None},
        'listener': {'bind': ctx.app.config.get('bind'), 'port': ctx.app.config.get('port')},
        'expose': ctx.app.escape_summary(),
        'expose_mapping': _find_expose_mapping(profile, mappings),
    }


def _sse_start(ctx):
    """给 SSE 响应写头。返回 False 表示不应继续（如 HEAD 请求）。"""
    handler = ctx.handler
    if handler.command == 'HEAD':
        # RFC 9110：HEAD 不得有 body，更不能启动永不结束的流
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Connection', 'close')
        _common_headers(handler)
        handler.end_headers()
        handler.close_connection = True
        return False
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('Connection', 'close')
    handler.send_header('X-Accel-Buffering', 'no')     # Nginx 反代下禁用缓冲
    handler.close_connection = True                    # 流式响应不复用连接
    handler.end_headers()
    return True


def _sse_write(handler, event, data):
    payload = 'event: %s\ndata: %s\n\n' % (event, json.dumps(data, ensure_ascii=False))
    handler.wfile.write(payload.encode('utf-8'))
    handler.wfile.flush()


def api_events(ctx):
    """SSE：状态变化推送（每 3 秒一次快照，值不变时不推）。"""
    ctx.app.require_auth(ctx)
    if not _sse_start(ctx):
        return None
    last = None
    rounds = 0
    try:
        while True:
            rounds += 1
            # 每 10 轮复查会话：改密吊销会话后，已连上的流必须失效
            if rounds % 10 == 0 and not ctx.app.sessions.get(ctx.session_token):
                _sse_write(ctx.handler, 'expired', {})
                return None
            try:
                data = manager.status('frpc')
                conn = (data.get('connection') or {}).get('state')
                snapshot = {
                    'service_state': data.get('service_state'),
                    'service_view': _state_view(data.get('service_state'), _SERVICE_TONE),
                    'connection_state': conn,
                    'connection_view': _state_view(conn, _CONNECTION_TONE),
                    'pending': bool(data.get('pending')),
                }
            except (ValueError, OSError) as exc:
                snapshot = {'error': str(exc)}
            if snapshot != last:
                _sse_write(ctx.handler, 'status', snapshot)
                last = snapshot
            else:
                ctx.handler.wfile.write(b': ping\n\n')
                ctx.handler.wfile.flush()
            time.sleep(3)
    except (BrokenPipeError, ConnectionResetError, OSError):
        return None


def api_logs_stream(ctx):
    """SSE：日志尾随（按行增量推送）。"""
    ctx.app.require_auth(ctx)
    path = manager.ROOT / 'logs' / 'frpc.log'
    if not _sse_start(ctx):
        return None
    offset = 0
    try:
        # 首次推送最近 100 行，之后增量
        if path.exists():
            size = path.stat().st_size
            with path.open('rb') as handle:
                handle.seek(max(0, size - 256 * 1024))
                tail = handle.read().decode('utf-8', errors='replace').splitlines()[-100:]
            for line in tail:
                _sse_write(ctx.handler, 'log', {'line': line})
            offset = size
        rounds = 0
        while True:
            rounds += 1
            if rounds % 10 == 0 and not ctx.app.sessions.get(ctx.session_token):
                _sse_write(ctx.handler, 'expired', {})
                return None
            if not path.exists():
                _sse_write(ctx.handler, 'reset', {})
                time.sleep(1); continue
            size = path.stat().st_size
            if size < offset:            # 日志被轮转
                offset = 0
                _sse_write(ctx.handler, 'reset', {})
            if size > offset:
                with path.open('rb') as handle:
                    handle.seek(offset)
                    chunk = handle.read(size - offset).decode('utf-8', errors='replace')
                offset = size
                for line in chunk.splitlines():
                    _sse_write(ctx.handler, 'log', {'line': line})
            else:
                ctx.handler.wfile.write(b': ping\n\n')
                ctx.handler.wfile.flush()
                time.sleep(1)
    except (BrokenPipeError, ConnectionResetError, OSError):
        return None


def api_logs_download(ctx):
    """下载完整日志文件（text/plain + Content-Disposition）。"""
    ctx.app.require_auth(ctx)
    path = manager.ROOT / 'logs' / 'frpc.log'
    if not path.exists():
        raise HttpError(404, '日志文件不存在', 'E_NOT_FOUND',
                        hint='服务可能从未启动，或日志已轮转')
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise HttpError(500, '读取日志失败', 'E_IO', hint=str(exc)[:120])
    handler = ctx.handler
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/plain; charset=utf-8')
    handler.send_header('Content-Length', str(len(content)))
    handler.send_header('Content-Disposition', 'attachment; filename="frpc.log"')
    _common_headers(handler)
    handler.end_headers()
    if handler.command != 'HEAD':
        try: handler.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError): pass
    return None


def api_audit(ctx):
    """读取 Web 审计日志（最近 N 条，可按结果过滤）。"""
    ctx.app.require_auth(ctx)
    try:
        limit = int(ctx.param('limit', '50'))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(500, limit))
    path = manager.ROOT / 'logs' / 'web-audit.jsonl'
    rows = []
    if path.exists():
        for line in _tail_lines(path, limit):
            try: rows.append(json.loads(line))
            except ValueError: continue
    return {'items': rows, 'limit': limit}


def _tail_lines(path, count):
    """只读文件尾部（最多 512KB）取最后 N 行，避免大日志整文件读入内存。"""
    try:
        size = path.stat().st_size
        with path.open('rb') as handle:
            if size > 512 * 1024:
                handle.seek(size - 512 * 1024)
                handle.readline()            # 丢掉被截断的首行
            data = handle.read()
    except OSError:
        return []
    return data.decode('utf-8', errors='replace').splitlines()[-count:]


def api_logs(ctx):
    """读取最近 N 行日志（非流式，供下载/首屏）。"""
    ctx.app.require_auth(ctx)
    try:
        lines = int(ctx.param('lines', '200'))
    except (TypeError, ValueError):
        lines = 200
    lines = max(1, min(2000, lines))
    path = manager.ROOT / 'logs' / 'frpc.log'
    shown = 'logs/frpc.log'
    if not path.exists():
        return {'lines': [], 'path': shown, 'exists': False}
    content = _tail_lines(path, lines)
    return {'lines': content, 'path': shown, 'exists': True, 'total': len(content)}


# ---------------------------------------------------------------- 映射 CRUD

MAPPING_FIELDS = ('name', 'type', 'local_ip', 'local_port', 'remote_port',
                  'domain', 'enabled')

def _strict_bool(value, field, default):
    """布尔字段严格解析：绝不把字符串 "false" 当成 true。

    历史行为 `bool("false") is True` 会把"本意禁用"的映射真的启用并推到公网。
    """
    if value is None: return default
    if isinstance(value, bool): return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ('true', '1', 'yes', 'on'): return True
        if low in ('false', '0', 'no', 'off', ''): return False
    raise HttpError(400, '%s 必须是布尔值（true/false）' % field, 'E_INVALID',
                    details=[{'field': field, 'message': '必须是 true 或 false'}])


def _mapping_from_body(body, existing=None):
    """把请求体规整成合法 mapping dict（缺省值取现有值或默认值）。"""
    base = dict(existing or {})
    item = {
        'name': str(body.get('name', base.get('name', ''))).strip(),
        'type': str(body.get('type', base.get('type', 'tcp'))).lower(),
        'local_ip': str(body.get('local_ip', base.get('local_ip', '127.0.0.1'))).strip(),
        'local_port': _to_int(body.get('local_port', base.get('local_port')), '本地端口'),
        'enabled': _strict_bool(body.get('enabled'), 'enabled',
                                base.get('enabled', True)),
    }
    if item['type'] in ('tcp', 'udp'):
        item['remote_port'] = _to_int(body.get('remote_port', base.get('remote_port')),
                                      '远程端口')
    else:
        item['domain'] = str(body.get('domain', base.get('domain', ''))).strip()
    return item


def _to_int(value, field):
    """端口字段归一为 int（避免 API 里 str/number 混用）。"""
    if value is None: return value
    if isinstance(value, bool): raise HttpError(400, '%s 必须是数字' % field, 'E_INVALID')
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise HttpError(400, '%s 必须是数字' % field, 'E_INVALID',
                        details=[{'field': field, 'message': '必须是 1-65535 的整数'}])


def _active_pid():
    pid = manager.active()
    if not pid:
        raise HttpError(400, '还没有选择服务端', 'E_NO_PROFILE',
                        hint='请先到「服务端」页新建或选择一个 FRPS 服务端')
    manager.load_profile(pid)
    return pid


def api_mappings(ctx):
    """列出映射（默认当前服务端；all=1 时列出全部服务端）。"""
    ctx.app.require_auth(ctx)
    want_all = str(ctx.param('all', '0')) in ('1', 'true', 'yes')
    active_id = manager.active()
    rows = []
    errors = []
    targets = manager.profiles() if want_all else None
    if targets is None:
        pid = _active_pid()
        targets = [manager.load_profile(pid)]
    online = manager.mapping_online_status(active_id) if active_id else None
    for profile in targets:
        try:
            items = manager.mappings(profile['id'])
        except (ValueError, OSError) as exc:
            errors.append({'profile': profile.get('name'), 'error': _sanitize(exc)})
            continue
        for item in items:
            row = dict(item)
            row['profile_id'] = profile['id']
            row['profile_name'] = profile.get('name')
            row['is_active_profile'] = profile['id'] == active_id
            if profile['id'] == active_id and online is not None:
                row['online'] = bool(online.get(str(item.get('name'))))
            else:
                row['online'] = None
            rows.append(row)
    return {'items': rows, 'errors': errors,
            'active_profile': _profile_brief(manager.load_profile(active_id)) if active_id else None}


def _save_mappings_quiet(pid, items):
    """Web 端保存映射：静默（不往 journal 打结果框、不读 stdin）+ 返回是否待重启。"""
    with manager.non_interactive():
        manager.save_mappings(pid, items)
    return bool(manager.state().get('pending'))


def _locate(items, name):
    for index, item in enumerate(items):
        if str(item.get('name')) == name:
            return index, item
    raise HttpError(404, '映射不存在：' + name, 'E_NOT_FOUND')


def api_mapping_create(ctx):
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    pid = _active_pid()
    item = _mapping_from_body(body)
    # 读-改-写必须在同一把锁内完成，否则并发请求会各自基于旧快照写回，
    # 后写者覆盖先写者（实测 10 个并发只用 1 个落盘）。
    with ctx.app.write_lock:
        items = manager.mappings(pid)
        if any(str(m.get('name')) == item['name'] for m in items):
            raise HttpError(400, '映射名称已存在：' + item['name'], 'E_DUPLICATE_NAME')
        items.append(item)
        manager.validate_mappings(items)
        pending = _save_mappings_quiet(pid, items)
    web_audit('mapping-create', ctx.client_ip, ctx.session['user'], 'ok', item['name'])
    return {'item': item, 'pending': pending}


def _require_profile(pid):
    """确认 profile 存在；不存在返回 404（而不是把 FileNotFoundError 当磁盘故障）。"""
    if not re.fullmatch(r'[a-f0-9]{24}', str(pid)):
        raise HttpError(404, '服务端不存在：' + str(pid), 'E_NOT_FOUND')
    try:
        return manager.load_profile(pid)
    except FileNotFoundError:
        raise HttpError(404, '服务端不存在：' + str(pid), 'E_NOT_FOUND')
    except manager.ConfigCorruptError as exc:
        raise HttpError(500, '服务端配置已损坏：' + Path(exc.path).name, 'E_CONFIG_CORRUPT',
                        hint='可从「设置 → 备份与恢复」还原，或重新创建服务端')



def api_mapping_update(ctx, name):
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    pid = _active_pid()
    with ctx.app.write_lock:
        items = manager.mappings(pid)          # 只读一次，index 与列表必然一致
        index, current = _locate(items, name)
        item = _mapping_from_body(body, current)
        if item['name'] != name and any(
                str(m.get('name')) == item['name']
                for i, m in enumerate(items) if i != index):
            raise HttpError(400, '映射名称已存在：' + item['name'], 'E_DUPLICATE_NAME')
        items[index] = item
        manager.validate_mappings(items)
        pending = _save_mappings_quiet(pid, items)
    web_audit('mapping-update', ctx.client_ip, ctx.session['user'], 'ok', name)
    return {'item': item, 'pending': pending}


def api_mapping_toggle(ctx, name):
    ctx.app.require_auth(ctx)
    pid = _active_pid()
    with ctx.app.write_lock:
        items = manager.mappings(pid)
        index, current = _locate(items, name)
        items[index] = dict(current, enabled=not current.get('enabled', True))
        manager.validate_mappings(items)
        pending = _save_mappings_quiet(pid, items)
    web_audit('mapping-toggle', ctx.client_ip, ctx.session['user'], 'ok', name)
    return {'item': items[index], 'pending': pending}


def api_mapping_delete(ctx, name):
    ctx.app.require_auth(ctx)
    body = ctx.json_body() if ctx.body else {}
    if str(body.get('confirm', '')) != name:
        raise HttpError(400, '请确认删除：需要提交与映射名相同的 confirm 字段',
                        'E_CONFIRM_REQUIRED')
    pid = _active_pid()
    with ctx.app.write_lock:
        items = manager.mappings(pid)
        index, _current = _locate(items, name)
        items.pop(index)
        pending = _save_mappings_quiet(pid, items)
    web_audit('mapping-delete', ctx.client_ip, ctx.session['user'], 'ok', name)
    return {'deleted': name, 'pending': pending}


# ---------------------------------------------------------------- 服务端 CRUD

def api_profiles(ctx):
    ctx.app.require_auth(ctx)
    active_id = manager.active()
    rows = []
    for profile in manager.profiles():
        row = dict(profile)
        row.pop('token', None)                    # 凭据绝不外发
        try:
            count = len(manager.mappings(profile['id']))
        except (ValueError, OSError):
            count = None
        row['mapping_count'] = count
        row['is_active'] = profile['id'] == active_id
        rows.append(row)
    return {'items': rows, 'active_id': active_id}


PROFILE_FIELDS = ('name', 'server_addr', 'server_port', 'tls_enable',
                  'transport_protocol', 'log_level', 'admin_port',
                  'dashboard_port', 'dashboard_addr', 'dashboard_scheme')
TRANSPORT_PROTOCOLS = ('tcp', 'kcp', 'quic', 'websocket', 'wss')
LOG_LEVELS = ('trace', 'debug', 'info', 'warn', 'error')

def _profile_from_body(body, existing=None):
    base = dict(existing or {})
    item = {}
    for key in PROFILE_FIELDS:
        if key in body:
            # 显式 null 视为"未提供"，否则会把 None 写进 profile.toml 让该配置永久不可用
            if body[key] is None: continue
            item[key] = body[key]
        elif key in base:
            item[key] = base[key]
    for key in ('server_port', 'admin_port', 'dashboard_port'):
        if key in item:
            item[key] = _to_int(item[key], key)
    if 'tls_enable' in item:
        item['tls_enable'] = _strict_bool(item['tls_enable'], 'tls_enable',
                                          bool(base.get('tls_enable', True)))
    if 'transport_protocol' in item:
        value = str(item['transport_protocol']).lower()
        if value not in TRANSPORT_PROTOCOLS:
            raise HttpError(400, '传输协议只支持 ' + '/'.join(TRANSPORT_PROTOCOLS),
                            'E_INVALID', details=[{'field': 'transport_protocol'}])
        item['transport_protocol'] = value
    if 'log_level' in item:
        value = str(item['log_level']).lower()
        if value not in LOG_LEVELS:
            raise HttpError(400, '日志级别只支持 ' + '/'.join(LOG_LEVELS),
                            'E_INVALID', details=[{'field': 'log_level'}])
        item['log_level'] = value
    if 'dashboard_scheme' in item:
        value = str(item['dashboard_scheme']).lower()
        if value not in ('http', 'https'):
            raise HttpError(400, 'Dashboard 协议只支持 http/https', 'E_INVALID')
        item['dashboard_scheme'] = value
    return item


def api_profile_create(ctx):
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    profile = _profile_from_body(body)
    token = str(body.get('token', '') or '').strip()
    if not token:
        raise HttpError(400, 'token 不能为空', 'E_INVALID',
                        details=[{'field': 'token', 'message': '必填'}])
    # render()/本地 admin API 依赖 admin_password，必须与 CLI 向导一致地生成，
    # 否则新建的服务端在「设为当前」或启动 frpc 时会 KeyError。
    secret = {'token': token,
              'admin_password': secrets.token_urlsafe(32)}
    profile['name'] = str(profile.get('name', '')).strip()
    if not profile['name']:
        raise HttpError(400, '名称不能为空', 'E_INVALID')
    profile.setdefault('server_addr', '')
    profile.setdefault('server_port', 7000)
    profile.setdefault('tls_enable', True)
    profile.setdefault('admin_port', 7400)
    with ctx.app.write_lock:
        with manager.non_interactive():
            pid = manager.create_profile(profile, secret)
            activated = False
            # 第一个服务端直接设为当前，省掉"新建完还要再点一次切换"
            if not manager.active():
                manager.switch(pid)
                activated = True
    web_audit('profile-create', ctx.client_ip, ctx.session['user'], 'ok', profile['name'])
    return {'id': pid, 'activated': activated}


def api_profile_update(ctx, pid):
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    current = _require_profile(pid)
    profile = dict(current)
    profile.update(_profile_from_body(body, current))
    secret = manager.secrets_for(pid)
    if 'token' in body and str(body['token']).strip():
        secret['token'] = str(body['token']).strip()
    if not secret.get('admin_password'):
        secret['admin_password'] = secrets.token_urlsafe(32)
    current.setdefault('admin_port', 7400)
    with ctx.app.write_lock:
        with manager.non_interactive():
            manager.update_profile(pid, profile, secret)
    web_audit('profile-update', ctx.client_ip, ctx.session['user'], 'ok', profile.get('name', ''))
    return {'id': pid}


def api_profile_switch(ctx, pid):
    ctx.app.require_auth(ctx)
    profile = _require_profile(pid)
    body = ctx.json_body() if ctx.body else {}
    # 先探测可达性：不可达时先返回"需要确认"，避免把 frpc 切到连不上的地址
    if not body.get('force'):
        try:
            reachable = manager.port_reachable(profile['server_addr'],
                                               int(profile['server_port']))
        except (ValueError, OSError):
            reachable = True
        if not reachable:
            return {'switched': False, 'reachable': False,
                    'message': '新服务端 %s:%s 当前不可达（地址/端口错误或被防火墙拦截）'
                               % (profile['server_addr'], profile['server_port']),
                    'hint': '确认仍要切换请提交 force=true'}
    with ctx.app.write_lock:
        with manager.non_interactive():
            manager.switch(pid)
    web_audit('profile-switch', ctx.client_ip, ctx.session['user'], 'ok', pid)
    return {'switched': True, 'reachable': True, 'active_id': manager.active()}


def api_profile_delete(ctx, pid):
    ctx.app.require_auth(ctx)
    body = ctx.json_body() if ctx.body else {}
    profile = _require_profile(pid)
    if str(body.get('confirm', '')) != str(profile.get('name', '')):
        raise HttpError(400, '请确认删除：需要提交与服务端名相同的 confirm 字段',
                        'E_CONFIRM_REQUIRED')
    with ctx.app.write_lock:
        with manager.non_interactive():
            manager.delete_profile_id(pid)
    web_audit('profile-delete', ctx.client_ip, ctx.session['user'], 'ok', profile.get('name', ''))
    return {'deleted': pid, 'active_id': manager.active()}


# ---------------------------------------------------------------- 诊断 / 备份

def api_diagnose(ctx):
    ctx.app.require_auth(ctx)
    started = time.time()
    try:
        report = manager.diagnostics_report('frpc')
    except (ValueError, OSError) as exc:
        raise HttpError(500, '诊断执行失败：' + _sanitize(exc), 'E_DIAG',
                        hint='查看服务日志获取详情')
    items = report.get('checks') if isinstance(report, dict) else None
    # 注意：这里不能用 'ok' 表示"检查是否全部通过"——外层信封的 ok 表示
    # "请求是否成功"，两者同名会让前端把"诊断发现问题"误判成接口失败。
    return {'elapsed_ms': int((time.time() - started) * 1000),
            'passed': bool(report.get('ok')) if isinstance(report, dict) else False,
            'items': items or [], 'raw': report}


def api_backups(ctx):
    ctx.app.require_auth(ctx)
    rows = []
    for item in manager.list_backups():
        path = item.get('path')
        rows.append({'name': Path(path).name if path else '',
                     'time': item.get('time'),
                     'reason': item.get('reason'),
                     'files': item.get('files') or [],
                     'path': ('backups/' + Path(path).name) if path else ''})
    return {'items': rows}


def api_backup_create(ctx):
    ctx.app.require_auth(ctx)
    with ctx.app.write_lock:
        path = manager.backup(manager.managed_paths(), 'manual')
    web_audit('backup-create', ctx.client_ip, ctx.session['user'], 'ok', str(path))
    return {'path': 'backups/' + Path(path).name, 'name': Path(path).name}


def api_restore(ctx):
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    name = str(body.get('name', '')).strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]+', name) or name in ('.', '..'):
        raise HttpError(400, '备份名称不合法', 'E_INVALID')
    if str(body.get('confirm', '')) != name:
        raise HttpError(400, '请确认恢复：需要提交与备份名相同的 confirm 字段',
                        'E_CONFIRM_REQUIRED')
    directory = manager.ROOT / 'backups' / name
    if not directory.is_dir():
        raise HttpError(404, '备份不存在：' + name, 'E_NOT_FOUND')
    with ctx.app.write_lock:
        with manager.non_interactive():
            try: manager.backup(manager.managed_paths(), 'pre-restore')
            except (ValueError, OSError): pass
            manifest = manager.read_json(directory / 'manifest.json', {})
            if not isinstance(manifest, dict):
                raise HttpError(400, '备份清单格式非法', 'E_INVALID')
            reason = manifest.get('reason', '')
            if reason == 'upgrade':
                restored = manager.restore_upgrade_snapshot(directory)
            else:
                restored = manager.restore_backup(directory)
    # 备份里可能包含 web.json：恢复后必须重载内存配置，否则磁盘口令与内存口令不一致
    try:
        ctx.app.config = load_web_config()
    except (ValueError, OSError):
        pass
    web_audit('restore', ctx.client_ip, ctx.session['user'], 'ok', name)
    return {'restored': restored, 'name': name}




# ---------------------------------------------------------------- 设置

def _masked_panel():
    cfg = ctx_cfg = None
    cfg = load_web_config()
    return {
        'bind': cfg.get('bind'),
        'port': cfg.get('port'),
        'username': cfg.get('username'),
        'password_set': bool(cfg.get('password_hash')),
        'idle_timeout_minutes': cfg.get('idle_timeout_minutes'),
        'login_max_failures': cfg.get('login_max_failures'),
        'login_lock_minutes': cfg.get('login_lock_minutes'),
        'first_run': bool(cfg.get('must_change_password')),
        'escape': None,
    }


def api_settings(ctx):
    ctx.app.require_auth(ctx)
    data = _masked_panel()
    data['escape'] = ctx.app.escape_summary()
    data['autostart'] = _autostart_state()
    return data


def _autostart_state():
    try:
        return manager.autostart_enabled('frpc')
    except (ValueError, OSError, AttributeError):
        return None


def api_autostart(ctx):
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    enabled = _strict_bool(body.get('enabled'), 'enabled', None)
    if enabled is None:
        raise HttpError(400, '缺少 enabled 参数', 'E_INVALID')
    with ctx.app.write_lock:
        with manager.non_interactive():
            manager.set_autostart('frpc', enabled)
    web_audit('autostart', ctx.client_ip, ctx.session['user'], 'ok', str(enabled))
    return {'enabled': _autostart_state()}


# ---------------------------------------------------------------- 设置：改端口/绑定/凭据（带逃生窗口）

def _validate_bind(value):
    import ipaddress
    try:
        ipaddress.ip_address(str(value))
    except ValueError:
        raise HttpError(400, '绑定地址必须是合法 IP（如 127.0.0.1 或 0.0.0.0）', 'E_INVALID')


def api_settings_apply(ctx):
    """修改绑定/端口。成功则新监听立即生效，旧监听保留 5 分钟逃生窗口。

    状态机：
      未切换中 → 拒绝并发切换（避免监听泄漏）
      切换中   → 新监听到位，旧监听继续服务，窗口到点或用户确认后关闭
    """
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    with ctx.app.write_lock:
        return _settings_apply_locked(ctx, body)

def _settings_apply_locked(ctx, body):
    cfg = load_web_config()
    old_bind, old_port = str(cfg.get('bind')), int(cfg.get('port'))

    with ctx.app.escape_lock:
        if ctx.app.escape:
            raise HttpError(409, '上一次切换还未确认', 'E_ESCAPE_PENDING',
                            hint='请先在页面顶部确认新地址可用，或点「回滚到旧配置」')
    new_bind = str(body.get('bind', old_bind)).strip() or old_bind
    try:
        new_port = int(body.get('port', old_port))
    except (TypeError, ValueError):
        raise HttpError(400, '端口必须是数字', 'E_INVALID')
    _validate_bind(new_bind)
    if not 1 <= new_port <= 65535:
        raise HttpError(400, '端口必须在 1-65535 之间', 'E_INVALID')
    # 绑定非回环地址 = 把 root 控制面暴露到网络：必须先设密码
    if new_bind not in ('127.0.0.1', '::1') and not cfg.get('password_hash'):
        raise HttpError(400, '绑定非回环地址前必须先设置管理端密码', 'E_NEED_PASSWORD',
                        hint='到「设置 → 账号安全」设置密码后再改绑定地址')

    if new_bind == old_bind and new_port == old_port:
        return {'changed': False, 'escape': None}
    if not _port_available(new_bind, new_port):
        raise HttpError(409, '端口 %d 已被占用' % new_port, 'E_PORT_IN_USE',
                        hint='换一个端口，或先停止占用该端口的程序')

    cfg['bind'] = new_bind
    cfg['port'] = new_port
    save_web_config(cfg)
    ctx.app.config = cfg

    # 启动第二个监听；失败则回滚配置，旧监听从未中断
    try:
        server = _make_server(ctx.app, new_bind, new_port)
    except OSError as exc:
        cfg['bind'], cfg['port'] = old_bind, old_port
        save_web_config(cfg)
        ctx.app.config = cfg
        raise HttpError(500, '新地址监听失败，配置已回滚：' + str(exc), 'E_BIND_FAILED',
                        hint='旧地址仍然可用，请检查防火墙或端口权限')

    previous = ctx.app.current_listener
    entry = _serve_in_thread(ctx.app, server, 'new')
    ctx.app.current_listener = entry
    with ctx.app.escape_lock:
        ctx.app.escape = {'old': 'http://%s:%d/' % (old_bind, old_port),
                          'new': 'http://%s:%d/' % (new_bind, new_port),
                          'until': time.time() + ESCAPE_WINDOW_SECONDS,
                          'old_entry': previous,
                          'old_cfg': {'bind': old_bind, 'port': old_port}}
    web_audit('settings-apply', ctx.client_ip, ctx.session['user'], 'ok',
              '%s:%d -> %s:%d' % (old_bind, old_port, new_bind, new_port))
    return {'changed': True, 'escape': ctx.app.escape_summary()}


def _close_listener(app, entry):
    if not entry: return
    _shutdown_listener(entry)
    try: app.listeners.remove(entry)
    except ValueError: pass


def api_settings_confirm(ctx):
    """确认新地址可用：立即关闭旧监听。"""
    ctx.app.require_auth(ctx)
    with ctx.app.write_lock:
        return _settings_confirm_locked(ctx)


def _settings_confirm_locked(ctx):
    with ctx.app.escape_lock:
        escape = ctx.app.escape
        if not escape:
            return {'escape': None, 'message': '当前没有进行中的切换'}
        old_entry = escape.get('old_entry')
        ctx.app.escape = None
    _close_listener(ctx.app, old_entry)
    web_audit('escape-confirm', ctx.client_ip, ctx.session['user'], 'ok')
    return {'escape': None, 'message': '旧监听已关闭'}


def api_settings_rollback(ctx):
    """回滚到旧配置：关闭新监听，恢复旧配置（旧监听仍在服务）。"""
    ctx.app.require_auth(ctx)
    with ctx.app.write_lock:
        return _settings_rollback_locked(ctx)


def _settings_rollback_locked(ctx):
    with ctx.app.escape_lock:
        escape = ctx.app.escape
        if not escape:
            return {'escape': None, 'message': '当前没有可回滚的切换'}
        old_cfg = escape.get('old_cfg') or {}
        old_entry = escape.get('old_entry')
        current = ctx.app.current_listener
        ctx.app.escape = None
    # 恢复配置
    cfg = load_web_config()
    if old_cfg.get('bind'): cfg['bind'] = old_cfg['bind']
    if old_cfg.get('port'): cfg['port'] = int(old_cfg['port'])
    save_web_config(cfg)
    ctx.app.config = cfg
    # 关掉新监听，把当前监听指回旧监听
    if current is not None and current is not old_entry:
        _close_listener(ctx.app, current)
    ctx.app.current_listener = old_entry
    web_audit('escape-rollback', ctx.client_ip, ctx.session['user'], 'ok')
    return {'escape': None, 'message': '已回滚到旧配置（旧监听仍在服务）'}


def api_settings_password(ctx):
    """修改用户名/密码：新旧凭据同时有效 5 分钟，避免记错密码被锁死。"""
    session = ctx.app.require_auth(ctx)
    body = ctx.json_body()
    with ctx.app.write_lock:
        return _settings_password_locked(ctx, body, session)


def _settings_password_locked(ctx, body, session):
    cfg = load_web_config()
    # 已登录不等于有权改密：必须提供当前密码，否则会话被盗即可改密锁死管理员
    current = str(body.get('current_password', ''))
    if not current or not verify_password(current, cfg.get('password_hash')):
        if cfg.get('password_hash'):
            raise HttpError(403, '当前密码不正确', 'E_CURRENT_PASSWORD',
                            hint='请输入当前密码后再修改')
    new_user = str(body.get('username', cfg.get('username'))).strip()
    new_password = str(body.get('password', ''))
    if new_user and len(new_user) < 2:
        raise HttpError(400, '用户名至少 2 个字符', 'E_INVALID')
    changed = False
    if new_password:
        if len(new_password) < 8:
            raise HttpError(400, '密码至少 8 位', 'E_INVALID')
        # 旧哈希保留 5 分钟：密码记错时还能用旧密码进来改回去
        cfg['password_hash_old'] = cfg.get('password_hash', '')
        cfg['password_expires_at'] = time.time() + ESCAPE_WINDOW_SECONDS
        cfg['password_hash'] = hash_password(new_password)
        cfg['must_change_password'] = False
        # 注意：绝不能在这里清掉 password_hash_old —— 它正是"旧密码 5 分钟宽限期"
        # 的唯一依据。清掉会让界面承诺的逃生通道立即失效。
        changed = True
    if new_user:
        cfg['username'] = new_user
    save_web_config(cfg)
    ctx.app.config = cfg
    if changed:
        # 吊销除当前会话外的全部会话：改密应当能把可疑的旧会话踢下线
        ctx.app.sessions.clear_except(ctx.session_token)
    web_audit('settings-password', ctx.client_ip, session['user'], 'ok')
    return {'username': cfg.get('username'), 'password_changed': changed,
            'old_password_valid_for': ESCAPE_WINDOW_SECONDS if changed else 0,
            'sessions_revoked': changed}


# ---------------------------------------------------------------- 危险操作

def api_uninstall(ctx):
    body = ctx.json_body()
    if str(body.get('confirm', '')) != 'frpc':
        raise HttpError(400, '请确认卸载：需要提交 confirm=frpc', 'E_CONFIRM_REQUIRED')
    purge = _strict_bool(body.get('purge'), 'purge', False)
    # 卸载会删掉本进程所在的文件；先回响应，再脱离会话执行
    web_audit('uninstall', ctx.client_ip, ctx.session['user'], 'ok', 'purge=%s' % purge)
    threading.Thread(target=_run_uninstall_detached, args=(purge,), daemon=True).start()
    return {'message': '卸载已开始，管理端将在数秒内停止', 'purge': purge}


def _run_uninstall_detached(purge=False):
    time.sleep(1.0)
    try:
        script = (manager.INSTALL_ROOT /
                  ('uninstall-windows.ps1' if os.name == 'nt' else 'uninstall.sh'))
        if not script.exists():
            raise ValueError('卸载脚本不存在：' + str(script))
        if os.name == 'nt':
            argv = ['powershell', '-File', str(script), '-Component', 'frpc', '-Yes']
        else:
            argv = ['bash', str(script), 'frpc', '--yes']
        if purge:
            argv.append('--purge')
        # 关键：必须在服务 cgroup 之外执行。否则脚本里"停止 web 服务"这一步
        # 会把执行卸载的进程自己杀掉，导致卸载只做了一半。
        ok, message = manager.run_detached(argv)
        web_audit('uninstall', result='ok' if ok else 'error', detail=message)
    except Exception as exc:            # 失败必须留痕
        web_audit('uninstall', result='error',
                  detail='%s: %s' % (type(exc).__name__, exc))



def api_upgrade(ctx):
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    target = str(body.get('target', '') or '').strip() or None
    if target and not re.fullmatch(r'v[0-9A-Za-z._-]+', target):
        raise HttpError(400, '版本号不合法', 'E_INVALID')
    web_audit('upgrade', ctx.client_ip, ctx.session['user'], 'ok', target or 'latest')
    thread = threading.Thread(target=_run_upgrade_detached, args=(target,), daemon=True)
    thread.start()
    if manager.web_service_installed():
        message = '升级已开始；完成后会自动重启管理端服务'
    else:
        message = '升级已开始；当前未安装常驻服务，请稍后手动重启 frpc web'
    return {'message': message}


def _run_upgrade_detached(target):
    time.sleep(0.5)
    try:
        with manager.non_interactive():
            manager.upgrade('frpc', target=target, yes=True)
        web_audit('upgrade', result='ok', detail='target=%s' % (target or 'latest'))
        # 升级只替换磁盘文件；不重启的话服务还在跑旧代码（旧界面/旧接口）
        if manager.web_service_installed():
            ok, message = manager.web_service_restart_detached()
            if not ok:
                web_audit('upgrade-restart', result='error', detail=message or '')
    except Exception as exc:
        web_audit('upgrade', result='error',
                  detail='%s: %s' % (type(exc).__name__, exc))


# ---------------------------------------------------------------- 自映射（面板暴露到公网）

EXPOSE_TCP_NAME = '_webui-tcp'
EXPOSE_HTTP_NAME = '_webui-http'


def _panel_port():
    try:
        return int(load_web_config().get('port') or 7501)
    except (TypeError, ValueError):
        return 7501


def _is_ours_expose(item, panel_port):
    """判断某条映射是不是本工具创建的自映射。

    判据：名字是保留名 + 本地地址是 127.0.0.1 + 本地端口是面板端口。
    改过面板端口后旧自映射的 local_port 是历史端口，用 `_looks_expose` 兜底识别。
    """
    if str(item.get('name')) not in (EXPOSE_TCP_NAME, EXPOSE_HTTP_NAME):
        return False
    if str(item.get('local_ip') or '') != '127.0.0.1':
        return False
    return True


def api_expose_create(ctx):
    """把管理端本身映射到公网：TCP 端口 或 HTTP 域名。"""
    ctx.app.require_auth(ctx)
    body = ctx.json_body()
    cfg = load_web_config()
    # 暴露前必须已设密码：面板以 root 运行
    if not cfg.get('password_hash'):
        raise HttpError(400, '暴露到公网前必须先设置面板密码', 'E_NEED_PASSWORD',
                        hint='到「设置 → 账号安全」设置密码')
    kind = str(body.get('type', 'tcp')).lower()
    pid = _active_pid()
    panel_port = _panel_port()
    if kind == 'tcp':
        remote = body.get('remote_port')
        if not remote:
            raise HttpError(400, '请填写公网端口', 'E_INVALID')
        item = {'name': EXPOSE_TCP_NAME, 'type': 'tcp', 'local_ip': '127.0.0.1',
                'local_port': panel_port, 'remote_port': _to_int(remote, '公网端口'),
                'enabled': True}
    elif kind == 'http':
        domain = str(body.get('domain', '')).strip()
        if not domain:
            raise HttpError(400, '请填写域名', 'E_INVALID')
        item = {'name': EXPOSE_HTTP_NAME, 'type': 'http', 'local_ip': '127.0.0.1',
                'local_port': panel_port, 'domain': domain, 'enabled': True}
    else:
        raise HttpError(400, '仅支持 tcp 或 http', 'E_INVALID')

    # 读-改-写必须在同一把锁内，否则并发会丢掉用户刚建的映射
    with ctx.app.write_lock:
        items = manager.mappings(pid)
        others = [m for m in items if not _is_ours_expose(m, panel_port)]
        # 旧自映射（可能指向改过的旧端口）原位替换，避免"名称重复"卡死
        same_kind = [m for m in items if _is_ours_expose(m, panel_port)
                     and str(m.get('type')) == item['type']]
        if same_kind:
            items = others
        else:
            items = [m for m in others
                     if str(m.get('name')) != item['name']]
        items.append(item)
        manager.validate_mappings(items)
        manager.save_mappings(pid, items)
    web_audit('expose-create', ctx.client_ip, ctx.session['user'], 'ok', kind)
    return {'item': item, 'pending': bool(manager.state().get('pending'))}


def api_expose_delete(ctx):
    ctx.app.require_auth(ctx)
    pid = _active_pid()
    panel_port = _panel_port()
    with ctx.app.write_lock:
        items = manager.mappings(pid)
        ours = [m for m in items if _is_ours_expose(m, panel_port)]
        if not ours:
            return {'deleted': False}
        kept = [m for m in items if not _is_ours_expose(m, panel_port)]
        manager.save_mappings(pid, kept)
    web_audit('expose-delete', ctx.client_ip, ctx.session['user'], 'ok')
    return {'deleted': True, 'removed': len(ours),
            'pending': bool(manager.state().get('pending'))}


# ---------------------------------------------------------------- 危险操作

# ---------------------------------------------------------------- 自映射（面板暴露到公网）

EXPOSE_TCP_NAME = '_webui-tcp'
EXPOSE_HTTP_NAME = '_webui-http'


# ---------------------------------------------------------------- 路由

ROUTES = [
    ('GET',    r'^/api/health$',        api_health,   False),
    ('POST',   r'^/api/login$',         api_login,    False),
    ('POST',   r'^/api/logout$',        api_logout,   True),
    ('GET',    r'^/api/session$',       api_session,  True),
    ('GET',    r'^/api/status$',        api_status,   True),
    ('GET',    r'^/api/overview$',      api_overview, True),
    ('GET',    r'^/api/events$',        api_events,   True),
    ('GET',    r'^/api/logs$',          api_logs,     True),
    ('GET',    r'^/api/logs/stream$',   api_logs_stream, True),
    ('GET',    r'^/api/logs/download$', api_logs_download, True),
    ('GET',    r'^/api/audit$',         api_audit,    True),
    ('POST',   r'^/api/service/start$',   api_service_start,   True),
    ('POST',   r'^/api/service/stop$',    api_service_stop,    True),
    ('POST',   r'^/api/service/restart$', api_service_restart, True),
    # 映射 CRUD
    ('GET',    r'^/api/mappings$',                     api_mappings,         True),
    ('POST',   r'^/api/mappings$',                     api_mapping_create,   True),
    ('PUT',    r'^/api/mappings/(?P<name>[A-Za-z0-9_-]{1,64})$', api_mapping_update, True),
    ('POST',   r'^/api/mappings/(?P<name>[A-Za-z0-9_-]{1,64})/toggle$', api_mapping_toggle, True),
    ('DELETE', r'^/api/mappings/(?P<name>[A-Za-z0-9_-]{1,64})$', api_mapping_delete, True),
    # 服务端 CRUD
    ('GET',    r'^/api/profiles$',                    api_profiles,         True),
    ('POST',   r'^/api/profiles$',                    api_profile_create,   True),
    ('PUT',    r'^/api/profiles/(?P<pid>[a-f0-9]{24})$', api_profile_update, True),
    ('POST',   r'^/api/profiles/(?P<pid>[a-f0-9]{24})/switch$', api_profile_switch, True),
    ('DELETE', r'^/api/profiles/(?P<pid>[a-f0-9]{24})$', api_profile_delete, True),
    # 诊断 / 备份
    ('POST',   r'^/api/diagnose$',     api_diagnose,      True),
    ('GET',    r'^/api/backups$',      api_backups,       True),
    ('POST',   r'^/api/backups$',      api_backup_create, True),
    ('POST',   r'^/api/restore$',      api_restore,       True),
    # 设置
    ('GET',    r'^/api/settings$',            api_settings,          True),
    ('POST',   r'^/api/settings/apply$',      api_settings_apply,    True),
    ('POST',   r'^/api/settings/confirm$',    api_settings_confirm,  True),
    ('POST',   r'^/api/settings/rollback$',   api_settings_rollback, True),
    ('POST',   r'^/api/settings/password$',   api_settings_password, True),
    ('POST',   r'^/api/autostart$',           api_autostart,         True),
    # 自映射
    ('POST',   r'^/api/expose$',      api_expose_create, True),
    ('DELETE', r'^/api/expose$',      api_expose_delete, True),
    # 危险操作
    ('POST',   r'^/api/uninstall$',   api_uninstall,     True),
    ('POST',   r'^/api/upgrade$',     api_upgrade,       True),
]


def _match_route(method, path):
    """按 path 匹配路由，再校验 method。

    返回 (handler, match, needs_auth, allowed_methods)：
      - 命中 → handler 非空
      - path 存在但方法不对 → handler 为 None 且 allowed 非空（调用方回 405）
      - 完全不存在 → 全为 None
    HEAD 自动复用 GET 处理器（RFC 7231）。
    """
    allowed = []
    for verb, pattern, handler, needs_auth in ROUTES:
        match = re.match(pattern, path)
        if not match: continue
        if verb == method or (method == 'HEAD' and verb == 'GET'):
            return handler, match, needs_auth, None
        allowed.append(verb)
    if method == 'HEAD' and 'GET' in allowed:
        return None, None, False, None
    if allowed:
        if 'GET' in allowed and 'HEAD' not in allowed:
            allowed.append('HEAD')
        return None, None, False, sorted(set(allowed))
    return None, None, False, None


# ---------------------------------------------------------------- HTTP 处理器

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'frp-simple-bash-web'
    sys_version = ''                    # 不暴露 Python 版本
    protocol_version = 'HTTP/1.1'
    timeout = 15                        # 单个请求的 socket 超时，防 slowloris

    def log_message(self, fmt, *args):
        # 不把每个请求打到 stdout（避免噪声与潜在敏感 URL 泄漏）
        return

    def _dispatch(self, method):
        app = self.server.app
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query, max_num_fields=50)
        except (ValueError, UnicodeError):
            self.close_connection = True
            fail(self, 400, '请求地址不合法', 'E_BAD_REQUEST')
            return
        length = 0
        raw_lengths = self.headers.get_all('Content-Length') or []
        if len(set(raw_lengths)) > 1:
            # RFC 7230：重复且不一致的 Content-Length 必须拒绝，否则可被用于请求走私
            self.close_connection = True
            fail(self, 400, 'Content-Length 不合法（重复且不一致）', 'E_BAD_REQUEST')
            return
        if raw_lengths:
            try:
                length = int(raw_lengths[0])
            except (TypeError, ValueError):
                self.close_connection = True
                fail(self, 400, 'Content-Length 不合法', 'E_BAD_REQUEST')
                return
        if length > MAX_BODY_BYTES or length < 0:
            # 未读取的 body 会残留到下一次请求，必须关连接
            self.close_connection = True
            fail(self, 413, '请求体过大（上限 %d 字节）' % MAX_BODY_BYTES, 'E413')
            return
        body = self.rfile.read(length) if length > 0 else b''
        transfer = (self.headers.get('Transfer-Encoding') or '').lower()
        if transfer and transfer != 'identity':
            self.close_connection = True
            fail(self, 411, '不支持 Transfer-Encoding，请改用 Content-Length', 'E_LENGTH')
            return
        if path == '/api' or path.startswith('/api/'):
            handler, _match, needs_auth, allowed = _match_route(method, path)
            if handler is None and allowed:
                send_json(self, {'ok': False, 'code': 'E_METHOD',
                                 'error': '该接口不支持 %s 方法' % method,
                                 'request_id': secrets.token_hex(6)},
                          405, extra_headers={'Allow': ', '.join(allowed)})
                return
            if handler is None:
                fail(self, 404, '接口不存在：%s %s' % (method, path), 'E404')
                return
            ctx = RequestContext(app, self, method, path, query, body, self.headers)
            try:
                if needs_auth:
                    # 统一在此处校验登录态 + CSRF，避免每个 handler 各写一遍漏掉
                    app.require_auth(ctx)
                kwargs = _match.groupdict() if _match else {}
                result = handler(ctx, **kwargs) if kwargs else handler(ctx)
                if result is not None:
                    ok(self, result if isinstance(result, dict) else {'data': result})
            except HttpError as exc:
                request_id = secrets.token_hex(6)
                if exc.status >= 500:
                    web_audit('http-error', ctx.client_ip, result='error',
                              detail='%s: %s' % (exc.code, exc.message))
                fail(self, exc.status, exc.message, exc.code, exc.hint,
                     exc.retry_after, exc.details, request_id)
            except manager.QuitProgram:
                fail(self, 500, '操作被中止', 'E_ABORTED',
                     hint='请重试；若反复出现请查看服务日志')
            except manager.ConfigCorruptError as exc:
                # 配置文件损坏属于服务端故障，不是用户输错
                web_audit('config-corrupt', ctx.client_ip, result='error', detail=exc.path)
                fail(self, 500, '配置文件已损坏：' + Path(exc.path).name,
                     'E_CONFIG_CORRUPT',
                     hint='可从「设置 → 备份与恢复」还原；路径见服务日志')
            except ValueError as exc:
                # 业务校验失败：文案本身是可读中文，直接回给用户（路径脱敏）
                fail(self, 400, _sanitize(exc), 'E_INVALID',
                     hint='请按提示修正后重试')
            except OSError as exc:
                # 磁盘/权限类问题属于服务端故障，不是用户输入错误
                web_audit('io-error', ctx.client_ip, result='error',
                          detail='%s: %s' % (type(exc).__name__, exc))
                fail(self, 500, '写入配置失败（磁盘或权限问题）', 'E_IO',
                     hint='检查 /etc/frp 目录权限与磁盘空间，详见服务日志')
            except (KeyError, TypeError) as exc:
                web_audit('internal-error', ctx.client_ip, result='error',
                          detail='%s: %s' % (type(exc).__name__, exc))
                fail(self, 500, '服务内部错误', 'E_INTERNAL',
                     hint='请重试；若反复出现请查看审计日志')
            except Exception as exc:                      # 兜底，避免连接直接断
                web_audit('internal-error', ctx.client_ip, result='error',
                          detail='%s: %s' % (type(exc).__name__, exc))
                fail(self, 500, '服务器内部错误', 'E_INTERNAL',
                     hint='请查看服务日志或审计日志')
            return
        if method in ('GET', 'HEAD'):
            self._serve_static(path)
        else:
            fail(self, 405, '方法不允许')

    def do_GET(self): self._dispatch('GET')
    def do_HEAD(self): self._dispatch('HEAD')
    def do_POST(self): self._dispatch('POST')
    def do_PUT(self): self._dispatch('PUT')
    def do_DELETE(self): self._dispatch('DELETE')
    def do_PATCH(self): self._dispatch('PATCH')
    def do_OPTIONS(self): self._dispatch('OPTIONS')
    def do_TRACE(self): self._dispatch('TRACE')

    def send_error(self, code, message=None, explain=None):
        """把 stdlib 的 HTML 错误页替换成 JSON 信封（414/431 等也会走到这里）。"""
        try:
            body = json.dumps({'ok': False, 'code': 'E%d' % code,
                               'error': message or '请求无法处理',
                               'request_id': secrets.token_hex(6)},
                              ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.close_connection = True
            _common_headers(self)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -- 静态文件 --

    def _serve_static(self, path):
        app = self.server.app
        dist = app.dist_dir
        if not dist.is_dir():
            web_audit('static-missing', result='error', detail=str(dist))
            self._send_text(500, '前端资源缺失，请重新安装 frp-simple-bash（详见服务日志）')
            return
        relative = path.lstrip('/') or 'index.html'
        in_assets = relative.startswith('assets/')
        if in_assets:
            target = (dist / relative)
        elif relative == 'index.html' or '.' not in Path(relative).name:
            target = dist / 'index.html'          # SPA fallback
        else:
            target = dist / relative
        try:
            resolved = target.resolve()
            if dist.resolve() not in resolved.parents and resolved != dist.resolve():
                self._send_text(403, '禁止访问')
                return
        except OSError:
            self._send_text(403, '禁止访问')
            return
        if not resolved.is_file():
            self._send_text(404, 'Not Found')
            return
        content = resolved.read_bytes()
        ctype = self._guess_type(resolved.suffix)
        etag = '"%s"' % hashlib.sha256(content).hexdigest()[:16]
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            _common_headers(self)
            _security_headers(self)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('ETag', etag)
        # 只有真实位于 dist/assets 下的文件才可长缓存
        try:
            real_assets = (dist / 'assets').resolve()
            immutable = (resolved.parent == real_assets)
        except OSError:
            immutable = False
        cache = 'public, max-age=31536000, immutable' if immutable else 'no-cache'
        self.send_header('Cache-Control', cache)
        _security_headers(self)
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    @staticmethod
    def _guess_type(suffix):
        return {'.html': 'text/html; charset=utf-8',
                '.js': 'application/javascript; charset=utf-8',
                '.css': 'text/css; charset=utf-8',
                '.json': 'application/json; charset=utf-8',
                '.svg': 'image/svg+xml',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.ico': 'image/x-icon',
                '.woff2': 'font/woff2'}.get(suffix.lower(), 'application/octet-stream')

    def _send_text(self, status, text):
        body = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        _common_headers(self)
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------- 服务器

MAX_CONCURRENT = 128         # 全局并发连接上限
MAX_PER_IP = 16              # 单 IP 并发连接上限（防单点占满全局池）
_conn_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_conn_per_ip = {}
_conn_per_ip_lock = threading.Lock()


def _acquire_ip_slot(ip):
    with _conn_per_ip_lock:
        count = _conn_per_ip.get(ip, 0)
        if count >= MAX_PER_IP:
            return False
        _conn_per_ip[ip] = count + 1
        return True


def _release_ip_slot(ip):
    with _conn_per_ip_lock:
        count = _conn_per_ip.get(ip, 0) - 1
        if count <= 0:
            _conn_per_ip.pop(ip, None)
        else:
            _conn_per_ip[ip] = count


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != 'nt'   # Windows 的 SO_REUSEADDR 会允许重复绑定
    app = None

    def process_request(self, request, client_address):
        """获取配额后启动处理线程。

        配额在**同一处**获取、在**同一处**释放（process_request_thread 的 finally），
        避免重复定义导致"获取一边、释放另一边"的双重释放。
        """
        ip = client_address[0] if client_address else ''
        if not _acquire_ip_slot(ip):
            self._reject(request, b'429 Too Many Requests')
            return
        if not _conn_slots.acquire(blocking=False):
            _release_ip_slot(ip)
            self._reject(request, b'503 Service Unavailable')
            return
        try:
            socketserver.ThreadingMixIn.process_request(self, request, client_address)
        except Exception:
            _conn_slots.release()
            _release_ip_slot(ip)
            raise

    @staticmethod
    def _reject(request, status):
        try:
            request.sendall(b'HTTP/1.1 ' + status +
                            b'\r\nContent-Type: text/plain\r\n'
                            b'Content-Length: 0\r\nConnection: close\r\n'
                            b'X-Content-Type-Options: nosniff\r\n\r\n')
        except OSError:
            pass
        try: request.close()
        except OSError: pass

    def process_request_thread(self, request, client_address):
        try:
            socketserver.ThreadingMixIn.process_request_thread(self, request, client_address)
        finally:
            _conn_slots.release()
            _release_ip_slot(client_address[0] if client_address else '')


def _make_server(app, bind, port):
    server = ThreadingHTTPServer((bind, port), Handler)
    server.app = app
    return server


def _serve_in_thread(app, server, label):
    thread = threading.Thread(target=server.serve_forever, name='web-' + label, daemon=True)
    thread.start()
    app.listeners.append({'server': server, 'thread': thread, 'label': label,
                          'bind': server.server_address[0], 'port': server.server_address[1]})
    entry = app.listeners[-1]
    # 第一个监听即"当前对外服务"的监听；后续切换时由 apply/confirm/rollback 维护
    if app.current_listener is None:
        app.current_listener = entry
    return entry


def _shutdown_listener(entry):
    try: entry['server'].shutdown()
    except Exception: pass
    try: entry['server'].server_close()
    except Exception: pass





# ---------------------------------------------------------------- 入口

def _port_available(bind, port):
    """端口能否绑定（仅检测，不占用）。"""
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((str(bind), int(port)))
        return True
    except OSError:
        return False
    finally:
        try: probe.close()
        except OSError: pass


def announce_startup(bind, port, cfg, generated_password):
    """打印启动信息并**立即 flush**。

    常驻服务下 stdout 是管道（块缓冲）：不 flush 的话，用户按文档执行
    `journalctl -u frp-simple-bash-web | grep 密码` 会一直看不到首次密码，
    直到进程退出或缓冲区写满。
    """
    print('frpc Web 管理端已启动')
    print('  访问地址：http://%s:%d/' % (bind, port))
    if bind not in ('127.0.0.1', '::1', 'localhost'):
        print('  ⚠ 已绑定非回环地址：管理端可从网络访问，请确保密码足够强。')
    if generated_password:
        print('  首次启动，已生成随机密码（请立即记录并登录后修改）：')
        print('    用户名：%s' % cfg.get('username'))
        print('    密码　：%s' % generated_password)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass


def run(bind=None, port=None, foreground=True, open_browser=False):
    """启动 Web 管理端（常驻服务与前台运行共用此入口）。"""
    # 确保 profiles/logs/backups 存在并执行日志轮转；只靠 Web 管理的主机
    # 否则永远不会有轮转，frpc.log 会无限增长
    try:
        manager.initialize()
    except (ValueError, OSError):
        pass
    cfg, generated = ensure_web_config()
    if bind: cfg['bind'] = bind
    if port: cfg['port'] = int(port)
    bind = str(cfg.get('bind') or '127.0.0.1')
    try:
        port = int(cfg.get('port') or 7501)
    except (TypeError, ValueError):
        port = 7501
    app = WebApp(cfg)
    try:
        server = _make_server(app, bind, port)
    except OSError as exc:
        raise ValueError('无法监听 %s:%d — %s' % (bind, port, exc))
    _serve_in_thread(app, server, 'main')

    announce_startup(bind, port, cfg, generated)
    web_audit('start', result='ok', detail='%s:%d' % (bind, port))
    def _escape_watchdog():
        """逃生窗口超时后自动关闭旧监听。"""
        while True:
            time.sleep(2)
            with app.escape_lock:
                escape = app.escape
                if not escape: continue
                if time.time() < escape.get('until', 0): continue
                old_entry = escape.get('old_entry')
                app.escape = None
            if old_entry:
                _close_listener(app, old_entry)
                web_audit('escape-timeout', result='ok')

    threading.Thread(target=_escape_watchdog, name='escape-watchdog', daemon=True).start()

    # 前台与常驻服务共用同一阻塞逻辑：服务线程已在后台跑，主线程等待退出信号。
    # systemd/launchd 停止服务发的是 SIGTERM，必须接住否则直接硬杀（请求被切断）。
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        stop_event.set()

    for signame in ('SIGTERM', 'SIGINT'):
        signum = getattr(signal, signame, None)
        if signum is not None:
            try: signal.signal(signum, _on_signal)
            except (ValueError, OSError): pass
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        web_audit('stop', result='ok')
        for entry in list(app.listeners):
            _shutdown_listener(entry)


def main(argv=None):
    """CLI 入口：frpc web [--bind A] [--port N] [--set-password] [--set-bind A] [--set-port N]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    bind = port = None
    set_password = False
    i = 0
    while i < len(argv):
        item = argv[i]
        if item in ('--bind', '--set-bind') and i + 1 < len(argv):
            if item == '--set-bind':
                cfg = load_web_config(); cfg['bind'] = argv[i + 1]; save_web_config(cfg)
                print('已保存绑定地址：%s' % argv[i + 1])
            else:
                bind = argv[i + 1]
            i += 2; continue
        if item in ('--port', '--set-port') and i + 1 < len(argv):
            if item == '--set-port':
                cfg = load_web_config()
                try: new_port = int(argv[i + 1])
                except ValueError: raise ValueError('端口必须是数字')
                if not 1 <= new_port <= 65535:
                    raise ValueError('端口必须在 1-65535 之间')
                if not _port_available(cfg.get('bind', '127.0.0.1'), new_port):
                    raise ValueError('端口 %d 已被占用，请换一个' % new_port)
                cfg['port'] = new_port
                save_web_config(cfg)
                print('已保存端口：%d（重启 Web 服务后生效）' % new_port)
            else:
                port = int(argv[i + 1])
            i += 2; continue
        if item == '--set-password':
            set_password = True; i += 1; continue
        if item == '--disable':
            ok, message = manager.web_service_disable()
            print(('已停止并禁用 Web 服务。' if ok else '停用失败：') + (message or ''))
            if ok:
                print('  恢复方法：frpc web --enable')
            return
        if item == '--enable':
            ok, message = manager.web_service_enable()
            print(('已启用并启动 Web 服务。' if ok else '启用失败：') + (message or ''))
            if ok:
                cfg = load_web_config()
                print('  访问地址：http://%s:%s/' % (cfg.get('bind'), cfg.get('port')))
            return
        if item == '--install-service':
            ok, message = manager.web_service_install()
            print(('已安装并启动 Web 服务：' if ok else '安装失败：') + (message or '完成'))
            if ok:
                cfg = load_web_config()
                print('  访问地址：http://%s:%s/' % (cfg.get('bind'), cfg.get('port')))
            return
        if item == '--uninstall-service':
            removed = manager.web_service_uninstall()
            print('已停止并移除 Web 服务。' if removed else '当前没有可移除的 Web 服务。')
            return
        if item == '--status':
            print('Web 服务状态：' + manager.web_service_status())
            cfg = load_web_config()
            print('  监听地址：http://%s:%s/' % (cfg.get('bind'), cfg.get('port')))
            print('  配置路径：' + str(web_config_path()))
            return
        if item in ('-h', '--help'):
            print('用法：frpc web [--bind 地址] [--port 端口] [--set-bind 地址] '
                  '[--set-port 端口] [--set-password] [--install-service] '
                  '[--uninstall-service] [--disable] [--enable] [--status]')
            return
        raise ValueError('未知参数：' + item)
    if set_password:
        import getpass
        try:
            first = getpass.getpass('新密码：')
            second = getpass.getpass('再输入一次：')
        except EOFError:
            raise ValueError('已取消')
        if not first or first != second:
            raise ValueError('两次输入不一致或为空')
        if len(first) < 8:
            raise ValueError('密码至少 8 位')
        cfg = load_web_config()
        cfg['password_hash'] = hash_password(first)
        save_web_config(cfg)
        print('密码已更新（请重启 Web 服务生效）')
        return
    run(bind=bind, port=port)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        sys.exit(1)
