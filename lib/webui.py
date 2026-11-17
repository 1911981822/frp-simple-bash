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

WEB_CONFIG_NAME = 'web.json'
SESSION_COOKIE = 'frpweb'
CSRF_HEADER = 'X-CSRF-Token'
ESCAPE_WINDOW_SECONDS = 300

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
    cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    try:
        cfg['port'] = int(cfg['port'])
    except (TypeError, ValueError):
        cfg['port'] = DEFAULT_CONFIG['port']
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
        try:
            (path.parent / (WEB_CONFIG_NAME + '.bak')).write_bytes(path.read_bytes())
        except OSError:
            pass
    payload = {k: cfg.get(k, DEFAULT_CONFIG.get(k)) for k in DEFAULT_CONFIG}
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
        with self._lock:
            self._items[token] = {'user': username, 'csrf': csrf,
                                  'seen': time.time()}
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
    def __init__(self, status, message, code=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.code = code or status


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

    def require_auth(self, ctx):
        session = self.current_session(ctx)
        if not session:
            raise HttpError(401, '未登录或会话已过期', 'E401')
        if ctx.method not in ('GET', 'HEAD'):
            token = ctx.headers.get(CSRF_HEADER, '')
            if not token or not hmac.compare_digest(token, session['csrf']):
                raise HttpError(403, 'CSRF 校验失败，请刷新页面重试', 'E403')
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
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def ok(handler, data=None, **extra):
    payload = {'ok': True, 'code': 0, 'error': None}
    if data is not None: payload.update(data if isinstance(data, dict) else {'data': data})
    payload.update(extra)
    send_json(handler, payload)


def fail(handler, status, message, code=None):
    send_json(handler, {'ok': False, 'code': code or status, 'error': message}, status)


# ---------------------------------------------------------------- API 处理

def api_health(ctx):
    return {'status': 'ok', 'version': manager.installed_manager_version()}


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
        raise HttpError(429, '登录失败次数过多，请 %d 秒后再试' % locked, 'E429')
    if not username or not password:
        raise HttpError(400, '用户名和密码不能为空')
    cfg = app.config
    if username != cfg.get('username') or not verify_password(password, cfg.get('password_hash')):
        app.limiter.fail(ctx.client_ip)
        web_audit('login', ctx.client_ip, username, 'failed')
        raise HttpError(401, '用户名或密码错误', 'E401')
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
    return {'logged_out': True}


def api_status(ctx):
    ctx.app.require_auth(ctx)
    data = manager.status('frpc')
    return {
        'service_state': data.get('service_state'),
        'connection_state': (data.get('connection') or {}).get('state'),
        'version': data.get('version'),
        'pending': bool(data.get('pending')),
        'manager_version': manager.installed_manager_version(),
        'active_profile': _profile_brief(data.get('active_profile')),
        'profile_error': data.get('profile_error'),
    }


def _profile_brief(profile):
    if not isinstance(profile, dict): return None
    return {'id': profile.get('id'), 'name': profile.get('name'),
            'server_addr': profile.get('server_addr'),
            'server_port': profile.get('server_port')}


def api_overview(ctx):
    ctx.app.require_auth(ctx)
    data = manager.status('frpc')
    profile = data.get('active_profile')
    mappings, online = [], {}
    if isinstance(profile, dict):
        try:
            mappings = manager.mappings(profile['id'])
        except (ValueError, OSError, KeyError, TypeError):
            mappings = []
    enabled = [m for m in mappings if m.get('enabled', True)]
    return {
        'service_state': data.get('service_state'),
        'connection_state': (data.get('connection') or {}).get('state'),
        'version': data.get('version'),
        'manager_version': manager.installed_manager_version(),
        'pending': bool(data.get('pending')),
        'active_profile': _profile_brief(profile),
        'counts': {'total': len(mappings), 'enabled': len(enabled),
                   'online': sum(1 for m in enabled if online.get(m.get('name'))),
                   'offline': 0},
        'listener': {'bind': ctx.app.config.get('bind'), 'port': ctx.app.config.get('port')},
        'expose': ctx.app.escape_summary(),
    }


# ---------------------------------------------------------------- 路由

ROUTES = [
    ('GET',    r'^/api/health$',        api_health,   False),
    ('POST',   r'^/api/login$',         api_login,    False),
    ('POST',   r'^/api/logout$',        api_logout,   True),
    ('GET',    r'^/api/session$',       api_session,  True),
    ('GET',    r'^/api/status$',        api_status,   True),
    ('GET',    r'^/api/overview$',      api_overview, True),
]


def _match_route(method, path):
    for verb, pattern, handler, needs_auth in ROUTES:
        if verb != method: continue
        match = re.match(pattern, path)
        if match:
            return handler, match, needs_auth
    return None, None, False


# ---------------------------------------------------------------- HTTP 处理器

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'frp-simple-bash-web'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # 不把每个请求打到 stdout（避免噪声与潜在敏感 URL 泄漏）
        return

    def _dispatch(self, method):
        app = self.server.app
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        length = 0
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length > 0 else b''
        if path.startswith('/api/'):
            handler, _match, needs_auth = _match_route(method, path)
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
                fail(self, exc.status, exc.message, exc.code)
            except manager.QuitProgram:
                fail(self, 500, '操作被中止')
            except (ValueError, OSError, KeyError, TypeError) as exc:
                fail(self, 400, str(exc))
            except Exception as exc:                      # 兜底，避免连接直接断
                fail(self, 500, '服务器内部错误：%s' % exc)
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
            self._send_text(500, '前端资源缺失：%s 不存在（请重新安装或构建）' % dist)
            return
        relative = path.lstrip('/') or 'index.html'
        if relative.startswith('assets/'):
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
        cache = 'public, max-age=31536000, immutable' if relative.startswith('assets/') else 'no-cache'
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------- 服务器

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app = None


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


# 逃生窗口：Web 端改端口/绑定时，新监听起来后旧监听继续服务一段时间
def _escape_summary(self):
    with self.escape_lock:
        if not self.escape: return None
        remaining = int(self.escape.get('until', 0) - time.time())
        if remaining <= 0: return None
        return {'old': self.escape.get('old'), 'new': self.escape.get('new'),
                'seconds_left': remaining}


WebApp.escape_summary = _escape_summary


# ---------------------------------------------------------------- 入口

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
    if not foreground:
        # 常驻服务模式：主线程阻塞等待
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt:
            pass
    _shutdown_listener(app.listeners[0])


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
                try: cfg['port'] = int(argv[i + 1])
                except ValueError: raise ValueError('端口必须是数字')
                save_web_config(cfg)
                print('已保存端口：%s' % argv[i + 1])
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
