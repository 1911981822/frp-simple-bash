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
    global manager
    manager = module

WEB_CONFIG_NAME = 'web.json'
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
    'session_secret': '',
    'idle_timeout_minutes': 30,
    'login_max_failures': 5,
    'login_lock_minutes': 15,
}


# ---------------------------------------------------------------- 配置

def web_config_path():
    return manager.ROOT / WEB_CONFIG_NAME


def load_web_config():
    """读取 web.json，缺失字段填默认值（容错，不让坏配置炸掉启动）。"""
    path = web_config_path()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            data = {}
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
    if not cfg.get('session_secret'):
        cfg['session_secret'] = secrets.token_hex(32)
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
    payload = dict(cfg)                 # 写回全部键（含扩展字段）
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
    """确保 web.json 存在；首次创建时生成随机密码并返回它（只返回一次）。"""
    path = web_config_path()
    if path.exists():
        return load_web_config(), None
    password = secrets.token_urlsafe(12)
    cfg = dict(DEFAULT_CONFIG)
    cfg['password_hash'] = hash_password(password)
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


class RequestContext(object):
    def __init__(self, app, handler, method, path, query, body, headers):
        self.app = app
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers
        self.client_ip = handler.client_address[0] if handler.client_address else ''
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

def _common_headers(handler, extra=None):
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.send_header('Cache-Control', 'no-store')
    for key, value in (extra or {}).items():
        handler.send_header(key, value)


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
    first_run = bool(cfg.get('must_change_password'))
    return {'status': 'ok', 'first_run': first_run}





def api_session(ctx):
    session = ctx.app.require_auth(ctx)
    return {'username': session['user'], 'csrf': session['csrf']}


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
    username_ok = (username == cfg.get('username'))
    if not (username_ok and password_ok):
        app.limiter.fail(ctx.client_ip)
        web_audit('login', ctx.client_ip, username[:64], 'failed')
        raise HttpError(401, '用户名或密码错误', 'E_AUTH_FAILED',
                        hint='连续失败 %d 次将锁定 %d 分钟'
                             % (cfg.get('login_max_failures', 5),
                                cfg.get('login_lock_minutes', 15)))
    app.limiter.reset(ctx.client_ip)
    token, csrf = app.sessions.create(username)
    web_audit('login', ctx.client_ip, username, 'ok')
    ctx.handler.send_response(200)
    ctx.handler.send_header('Content-Type', 'application/json; charset=utf-8')
    cookie = '%s=%s; Path=/; HttpOnly; SameSite=Strict' % (SESSION_COOKIE, token)
    ctx.handler.send_header('Set-Cookie', cookie)
    body = json.dumps({'ok': True, 'code': 0, 'error': None,
                       'username': username, 'csrf': csrf}).encode('utf-8')
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
        'profile_error': data.get('profile_error'),
        'concerns': manager.status_concerns('frpc', data),
        'session': {'user': session['user'],
                    'expires_in': ctx.app.sessions.remaining(ctx.session_token)},
    }


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
    }


def _sse_start(ctx):
    """给 SSE 响应写头。返回 False 表示已出错。"""
    handler = ctx.handler
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
    _sse_start(ctx)
    last = None
    try:
        while True:
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
    _sse_start(ctx)
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
        while True:
            if not path.exists():
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
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            text = ''
        for line in text.splitlines()[-limit:]:
            try: rows.append(json.loads(line))
            except ValueError: continue
    return {'items': rows, 'limit': limit}


def api_logs(ctx):
    """读取最近 N 行日志（非流式，供下载/首屏）。"""
    ctx.app.require_auth(ctx)
    try:
        lines = int(ctx.param('lines', '200'))
    except (TypeError, ValueError):
        lines = 200
    lines = max(1, min(2000, lines))
    path = manager.ROOT / 'logs' / 'frpc.log'
    if not path.exists():
        return {'lines': [], 'path': str(path), 'exists': False}
    content = path.read_text(encoding='utf-8', errors='replace').splitlines()
    return {'lines': content[-lines:], 'path': str(path), 'exists': True,
            'total': len(content)}


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
    ('GET',    r'^/api/audit$',         api_audit,    True),
    ('POST',   r'^/api/service/start$',   api_service_start,   True),
    ('POST',   r'^/api/service/stop$',    api_service_stop,    True),
    ('POST',   r'^/api/service/restart$', api_service_restart, True),
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
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            length = 0
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
                result = handler(ctx)
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
            except ValueError as exc:
                # 业务校验失败：文案本身是可读中文，直接回给用户
                fail(self, 400, str(exc), 'E_INVALID',
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
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        # 前端只加载同源资源，禁止内联脚本与外部域
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'")
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

MAX_CONCURRENT = 64          # 并发连接上限，防未认证的 slowloris 耗尽线程
_conn_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != 'nt'   # Windows 的 SO_REUSEADDR 会允许重复绑定
    app = None

    def process_request(self, request, client_address):
        # 超出上限时直接拒绝，避免线程无界增长
        if not _conn_slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            try: request.close()
            except OSError: pass
            return
        try:
            socketserver.ThreadingMixIn.process_request(self, request, client_address)
        except Exception:
            _conn_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            socketserver.ThreadingMixIn.process_request_thread(self, request, client_address)
        finally:
            _conn_slots.release()


def _make_server(app, bind, port):
    server = ThreadingHTTPServer((bind, port), Handler)
    server.app = app
    return server


def _serve_in_thread(app, server, label):
    thread = threading.Thread(target=server.serve_forever, name='web-' + label, daemon=True)
    thread.start()
    app.listeners.append({'server': server, 'thread': thread, 'label': label,
                          'bind': server.server_address[0], 'port': server.server_address[1]})
    return app.listeners[-1]


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


def run(bind=None, port=None, foreground=True, open_browser=False):
    """启动 Web 管理端（常驻服务与前台运行共用此入口）。"""
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

    print('frpc Web 管理端已启动')
    print('  访问地址：http://%s:%d/' % (bind, port))
    if bind not in ('127.0.0.1', '::1', 'localhost'):
        print('  ⚠ 已绑定非回环地址：管理端可从网络访问，请确保密码足够强。')
    if generated:
        print('  首次启动，已生成随机密码（请立即记录并登录后修改）：')
        print('    用户名：%s' % cfg.get('username'))
        print('    密码　：%s' % generated)
    web_audit('start', result='ok', detail='%s:%d' % (bind, port))
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
        if item in ('-h', '--help'):
            print('用法：frpc web [--bind 地址] [--port 端口] [--set-bind 地址] '
                  '[--set-port 端口] [--set-password]')
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
