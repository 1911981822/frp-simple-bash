"""Web 管理端后端单元测试：配置、密码、会话、限流、路由、认证/CSRF、静态托管。"""
import importlib.util
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('webui_under_test', str(ROOT / 'lib' / 'webui.py'))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class ServerFixture(object):
    """在临时目录起一个真实的 HTTP 服务，端口由系统分配。"""

    def __init__(self, password='testpass123'):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            patch.object(w.manager, 'ROOT', self.tmp),
            patch.object(w.manager, 'TEST', True),
            patch.object(w.manager, 'installed_manager_version', return_value='v-test'),
            patch.object(w.manager, 'status', return_value={
                'service_state': 'running', 'connection': {'state': 'connected'},
                'version': '0.72.0', 'pending': False,
                'active_profile': {'id': 'a' * 24, 'name': 'srv',
                                   'server_addr': '1.2.3.4', 'server_port': 7000},
            }),
            patch.object(w.manager, 'mappings', return_value=[
                {'name': 'ssh', 'type': 'tcp', 'enabled': True},
                {'name': 'web', 'type': 'tcp', 'enabled': False},
            ]),
        ]
        for item in self.patches:
            item.start()
        cfg = dict(w.DEFAULT_CONFIG)
        cfg['password_hash'] = w.hash_password(password)
        w.save_web_config(cfg)
        self.app = w.WebApp(w.load_web_config())
        self.server = w._make_server(self.app, '127.0.0.1', 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return 'http://127.0.0.1:%d%s' % (self.port, path)

    def request(self, method, path, body=None, headers=None, cookie=None, csrf=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url(path), data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if cookie: req.add_header('Cookie', cookie)
        if csrf: req.add_header(w.CSRF_HEADER, csrf)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, raw, resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def json(self, method, path, **kwargs):
        status, raw, headers = self.request(method, path, **kwargs)
        try: payload = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError): payload = {}
        return status, payload, headers

    def login(self):
        status, payload, headers = self.json('POST', '/api/login',
                                             body={'username': 'admin', 'password': 'testpass123'})
        cookie = ''
        for header in headers.get_all('Set-Cookie') or []:
            cookie = header.split(';')[0]
        return status, payload, cookie

    def close(self):
        try: self.server.shutdown()
        except Exception: pass
        try: self.server.server_close()
        except Exception: pass
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class WebUiConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = patch.object(w.manager, 'ROOT', self.tmp)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_password_hash_and_verify(self):
        stored = w.hash_password('s3cret-pass')
        self.assertTrue(stored.startswith('pbkdf2_sha256$'))
        self.assertTrue(w.verify_password('s3cret-pass', stored))
        self.assertFalse(w.verify_password('wrong', stored))
        self.assertFalse(w.verify_password('x', 'garbage'))
        self.assertFalse(w.verify_password('x', ''))

    def test_password_unique_salt(self):
        self.assertNotEqual(w.hash_password('same'), w.hash_password('same'))

    def test_config_roundtrip_and_backup(self):
        cfg = w.load_web_config()
        cfg['port'] = 7600
        w.save_web_config(cfg)
        self.assertEqual(w.load_web_config()['port'], 7600)
        cfg['port'] = 7601
        w.save_web_config(cfg)          # 第二次写入应产生 .bak
        self.assertTrue((self.tmp / (w.WEB_CONFIG_NAME + '.bak')).exists())
        self.assertEqual((self.tmp / w.WEB_CONFIG_NAME).stat().st_mode & 0o777, 0o600)

    def test_config_tolerates_garbage(self):
        (self.tmp / w.WEB_CONFIG_NAME).write_text('not json {{{')
        cfg = w.load_web_config()
        self.assertEqual(cfg['port'], 7501)          # 回落到默认值
        self.assertTrue(cfg['session_secret'])

    def test_config_coerces_bad_port(self):
        (self.tmp / w.WEB_CONFIG_NAME).write_text(json.dumps({'port': 'abc'}))
        self.assertEqual(w.load_web_config()['port'], 7501)

    def test_ensure_web_config_generates_password_once(self):
        cfg, password = w.ensure_web_config()
        self.assertTrue(password and len(password) >= 8)
        self.assertTrue(w.verify_password(password, cfg['password_hash']))
        cfg2, password2 = w.ensure_web_config()
        self.assertIsNone(password2)                 # 第二次不再生成


class SessionAndLimiterTests(unittest.TestCase):
    def test_session_create_get_drop(self):
        store = w.SessionStore(idle_minutes=30)
        token, csrf = store.create('admin')
        item = store.get(token)
        self.assertEqual(item['user'], 'admin')
        self.assertEqual(item['csrf'], csrf)
        store.drop(token)
        self.assertIsNone(store.get(token))

    def test_session_idle_expiry(self):
        store = w.SessionStore(idle_minutes=1)
        token, _ = store.create('admin')
        with patch.object(w.time, 'time', return_value=time.time() + 120):
            self.assertIsNone(store.get(token))

    def test_limiter_locks_after_failures(self):
        limiter = w.LoginLimiter(max_failures=3, lock_minutes=1)
        for _ in range(3): limiter.fail('1.1.1.1')
        self.assertGreater(limiter.locked('1.1.1.1'), 0)
        limiter.reset('1.1.1.1')
        self.assertEqual(limiter.locked('1.1.1.1'), 0)


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.fx = ServerFixture()

    def tearDown(self):
        self.fx.close()

    def test_health_is_public(self):
        status, payload, _ = self.fx.json('GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])

    def test_protected_endpoints_require_login(self):
        for path in ('/api/session', '/api/status', '/api/overview'):
            status, payload, _ = self.fx.json('GET', path)
            self.assertEqual(status, 401, path)
            self.assertFalse(payload['ok'])

    def test_login_success_sets_cookie_and_csrf(self):
        status, payload, cookie = self.fx.login()
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertIn(w.SESSION_COOKIE + '=', cookie)
        self.assertTrue(payload.get('csrf'))
        self.assertNotIn('password', json.dumps(payload).lower())

    def test_login_wrong_password_is_401(self):
        status, payload, _ = self.fx.json('POST', '/api/login',
                                          body={'username': 'admin', 'password': 'nope'})
        self.assertEqual(status, 401)

    def test_login_rate_limited(self):
        cfg = self.fx.app.config
        cfg['login_max_failures'] = 3
        self.fx.app.limiter = w.LoginLimiter(3, 1)
        for _ in range(3):
            self.fx.json('POST', '/api/login', body={'username': 'admin', 'password': 'x'})
        status, payload, _ = self.fx.json('POST', '/api/login',
                                          body={'username': 'admin', 'password': 'testpass123'})
        self.assertEqual(status, 429)

    def test_csrf_required_for_writes(self):
        _, _, cookie = self.fx.login()
        status, _, _ = self.fx.json('POST', '/api/logout', cookie=cookie)
        self.assertEqual(status, 403)                # 缺 CSRF
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        status, payload, _ = self.fx.json('POST', '/api/logout', cookie=cookie,
                                          csrf=sess['csrf'])
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])

    def test_status_returns_expected_shape(self):
        _, _, cookie = self.fx.login()
        status, payload, _ = self.fx.json('GET', '/api/status', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload['service_state'], 'running')
        self.assertEqual(payload['connection_state'], 'connected')
        self.assertEqual(payload['manager_version'], 'v-test')
        self.assertNotIn('token', json.dumps(payload).lower())

    def test_overview_counts_mappings(self):
        _, _, cookie = self.fx.login()
        status, payload, _ = self.fx.json('GET', '/api/overview', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload['counts']['total'], 2)
        self.assertEqual(payload['counts']['enabled'], 1)
        self.assertEqual(payload['listener']['port'], self.fx.app.config['port'])

    def test_unknown_api_returns_404(self):
        _, _, cookie = self.fx.login()
        status, payload, _ = self.fx.json('GET', '/api/nope', cookie=cookie)
        self.assertEqual(status, 404)

    def test_static_index_served(self):
        status, raw, headers = self.fx.request('GET', '/')
        # dist 可能未构建；未构建时应返回 500 且提示缺失，构建后应 200 text/html
        self.assertIn(status, (200, 500))
        if status == 200:
            self.assertIn('text/html', headers.get('Content-Type', ''))

    def test_static_path_traversal_blocked(self):
        status, raw, _ = self.fx.request('GET', '/../webui.py')
        self.assertIn(status, (403, 404))

    def test_security_headers_present(self):
        status, _, headers = self.fx.request('GET', '/api/health')
        self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(headers.get('X-Frame-Options'), 'DENY')


if __name__ == '__main__':
    unittest.main()
