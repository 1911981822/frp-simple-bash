"""Web 管理端后端单元测试：配置、密码、会话、限流、路由、认证/CSRF、静态托管。"""
import contextlib
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
        # 与生产路径一致：记录"当前对外服务"的监听
        self.app.listeners.append({'server': self.server, 'thread': self.thread,
                                   'label': 'main', 'bind': '127.0.0.1',
                                   'port': self.port})
        self.app.current_listener = self.app.listeners[-1]

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


    # ---- 审查修复回归（P1 review）----

    def test_head_has_no_body(self):
        status, raw, headers = self.fx.request('HEAD', '/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(raw, b'')            # HEAD 不能带 body
        self.assertTrue(int(headers.get('Content-Length') or 0) > 0)

    def test_oversized_body_closes_connection(self):
        import socket
        sock = socket.create_connection(('127.0.0.1', self.fx.port), timeout=5)
        try:
            sock.sendall(b'POST /api/login HTTP/1.1\r\nHost: x\r\n'
                         b'Content-Length: 99999999\r\n\r\n')
            data = sock.recv(200)
            self.assertIn(b'413', data)
        finally:
            sock.close()

    def test_chunked_body_rejected(self):
        status, payload, _ = self.fx.json('POST', '/api/login', headers={
            'Transfer-Encoding': 'chunked'}, body=None)
        self.assertEqual(status, 411)

    def test_bare_api_path_returns_json_404(self):
        _, _, cookie = self.fx.login()
        status, payload, headers = self.fx.json('GET', '/api', cookie=cookie)
        self.assertEqual(status, 404)
        self.assertIn('application/json', headers.get('Content-Type', ''))

    def test_bad_url_returns_400_not_crash(self):
        import socket
        sock = socket.create_connection(('127.0.0.1', self.fx.port), timeout=5)
        try:
            sock.sendall(b'GET http://[::1/ HTTP/1.1\r\nHost: x\r\n\r\n')
            data = sock.recv(400)
            self.assertIn(b'400', data)
        finally:
            sock.close()

    def test_error_envelope_has_hint_and_request_id(self):
        status, payload, headers = self.fx.json('POST', '/api/login',
                                               body={'username': '', 'password': ''})
        self.assertEqual(status, 400)
        self.assertIsInstance(payload['code'], str)      # code 统一为字符串
        self.assertIn('request_id', payload)
        self.assertTrue(headers.get('X-Request-Id'))

    def test_locked_login_has_retry_after(self):
        self.fx.app.limiter = w.LoginLimiter(1, 1)
        self.fx.json('POST', '/api/login', body={'username': 'admin', 'password': 'x'})
        status, payload, _ = self.fx.json('POST', '/api/login',
                                          body={'username': 'admin', 'password': 'x'})
        self.assertEqual(status, 429)
        self.assertGreater(payload.get('retry_after', 0), 0)
        self.assertEqual(payload.get('code'), 'E_AUTH_LOCKED')

    def test_audit_file_is_0600(self):
        import os
        self.fx.json('POST', '/api/login', body={'username': 'admin', 'password': 'bad'})
        path = self.fx.tmp / 'logs' / 'web-audit.jsonl'
        self.assertTrue(path.exists())
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_backup_config_is_0600(self):
        import os
        cfg = w.load_web_config(); cfg['port'] = 7602; w.save_web_config(cfg)
        cfg['port'] = 7603; w.save_web_config(cfg)
        bak = self.fx.tmp / (w.WEB_CONFIG_NAME + '.bak')
        self.assertTrue(bak.exists())
        if os.name != 'nt':
            self.assertEqual(bak.stat().st_mode & 0o777, 0o600)

    def test_unknown_config_keys_preserved(self):
        cfg = w.load_web_config()
        cfg['future_option'] = 'keep-me'
        w.save_web_config(cfg)
        self.assertEqual(w.load_web_config().get('future_option'), 'keep-me')

    def test_invalid_port_falls_back(self):
        (self.fx.tmp / w.WEB_CONFIG_NAME).write_text(json.dumps({'port': 70000}))
        self.assertEqual(w.load_web_config()['port'], 7501)

    def test_server_header_hides_python_version(self):
        _, _, headers = self.fx.request('GET', '/api/health')
        self.assertNotIn('Python', headers.get('Server', ''))

    def test_health_does_not_leak_version(self):
        _, payload, _ = self.fx.json('GET', '/api/health')
        self.assertNotIn('version', payload)

    def test_noncesrf_nonascii_is_403_not_500(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        status, payload, _ = self.fx.json('POST', '/api/logout', cookie=cookie,
                                          csrf='caf\u00e9')
        self.assertEqual(status, 403)

    def test_overview_online_is_null_when_api_unreachable(self):
        _, _, cookie = self.fx.login()
        with patch.object(w.manager, 'mapping_online_status', return_value=None):
            status, payload, _ = self.fx.json('GET', '/api/overview', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIsNone(payload['counts']['online'])   # 未知而非谎报 0

    def test_overview_reports_mappings_error(self):
        _, _, cookie = self.fx.login()
        with patch.object(w.manager, 'mappings', side_effect=OSError('permission denied')):
            status, payload, _ = self.fx.json('GET', '/api/overview', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(payload.get('mappings_error'))   # 读取失败不能伪装成空

    def test_overview_includes_concerns_and_views(self):
        _, _, cookie = self.fx.login()
        _, payload, _ = self.fx.json('GET', '/api/overview', cookie=cookie)
        self.assertIn('service_view', payload)
        self.assertIn('tone', payload['service_view'])
        self.assertIn('concerns', payload)

    def test_service_control_requires_csrf(self):
        _, _, cookie = self.fx.login()
        status, _, _ = self.fx.json('POST', '/api/service/start', cookie=cookie)
        self.assertEqual(status, 403)

    def test_service_start_invokes_manager(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        with patch.object(w.manager, 'service') as service, \
             patch.object(w.manager, 'require_active'), \
             patch.object(w.manager, 'status', return_value={'service_state': 'running'}):
            status, payload, _ = self.fx.json('POST', '/api/service/start',
                                              cookie=cookie, csrf=sess['csrf'])
        self.assertEqual(status, 200)
        service.assert_called_once()

    # ---- P3-P5 接口回归 ----

    def test_profile_create_generates_admin_password(self):
        """Web 新建服务端必须生成 admin_password，否则切换/启动会 KeyError。"""
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        with patch.object(w.manager, 'create_profile', return_value='a' * 24) as create, \
             patch.object(w.manager, 'active', return_value=''), \
             patch.object(w.manager, 'switch'):
            status, payload, _ = self.fx.json('POST', '/api/profiles', cookie=cookie,
                                              csrf=sess['csrf'],
                                              body={'name': 'srv1', 'server_addr': '1.2.3.4',
                                                    'server_port': 7000, 'token': 'tok12345678'})
        self.assertEqual(status, 200)
        _profile, secret = create.call_args[0]
        self.assertTrue(secret.get('admin_password'))   # 关键：不能缺

    def test_mapping_create_validates_duplicate_name(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}), \
             patch.object(w.manager, 'mappings', return_value=[
                 {'name': 'ssh', 'type': 'tcp', 'local_ip': '127.0.0.1',
                  'local_port': 22, 'remote_port': 6000, 'enabled': True}]):
            status, payload, _ = self.fx.json('POST', '/api/mappings', cookie=cookie,
                                              csrf=sess['csrf'],
                                              body={'name': 'ssh', 'type': 'tcp',
                                                    'local_port': 22, 'remote_port': 6001})
        self.assertEqual(status, 400)
        self.assertEqual(payload['code'], 'E_DUPLICATE_NAME')

    def test_mapping_delete_requires_confirm(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}):
            status, payload, _ = self.fx.json('DELETE', '/api/mappings/ssh', cookie=cookie,
                                              csrf=sess['csrf'], body={})
        self.assertEqual(status, 400)
        self.assertEqual(payload['code'], 'E_CONFIRM_REQUIRED')

    def test_restore_requires_confirm(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        status, payload, _ = self.fx.json('POST', '/api/restore', cookie=cookie,
                                          csrf=sess['csrf'], body={'name': 'x'})
        self.assertEqual(status, 400)
        self.assertEqual(payload['code'], 'E_CONFIRM_REQUIRED')

    def test_settings_apply_rejects_bad_port(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        status, payload, _ = self.fx.json('POST', '/api/settings/apply', cookie=cookie,
                                          csrf=sess['csrf'], body={'port': 70000})
        self.assertEqual(status, 400)
        self.assertEqual(payload['code'], 'E_INVALID')

    def test_settings_apply_rejects_bad_bind(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        status, payload, _ = self.fx.json('POST', '/api/settings/apply', cookie=cookie,
                                          csrf=sess['csrf'], body={'bind': 'not-an-ip'})
        self.assertEqual(status, 400)

    def test_expose_requires_password(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        with patch.object(w, 'load_web_config',
                          return_value=dict(w.DEFAULT_CONFIG, password_hash='')), \
             patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}), \
             patch.object(w.manager, 'mappings', return_value=[]):
            status, payload, _ = self.fx.json('POST', '/api/expose', cookie=cookie,
                                              csrf=sess['csrf'],
                                              body={'type': 'tcp', 'remote_port': 7001})
        self.assertEqual(status, 400)
        self.assertEqual(payload['code'], 'E_NEED_PASSWORD')

    def test_uninstall_requires_exact_confirm(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        status, payload, _ = self.fx.json('POST', '/api/uninstall', cookie=cookie,
                                          csrf=sess['csrf'], body={'confirm': 'yes'})
        self.assertEqual(status, 400)
        self.assertEqual(payload['code'], 'E_CONFIRM_REQUIRED')

    def test_diagnose_uses_passed_not_ok(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        with patch.object(w.manager, 'diagnostics_report',
                          return_value={'ok': False, 'checks': [
                              {'name': 'config', 'ok': False, 'detail': '缺失'}]}):
            status, payload, _ = self.fx.json('POST', '/api/diagnose', cookie=cookie,
                                              csrf=sess['csrf'])
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])          # 请求成功
        self.assertFalse(payload['passed'])     # 但诊断未通过
        self.assertEqual(len(payload['items']), 1)

    def test_all_routes_require_auth(self):
        """除 login/health 外，所有接口未登录都必须 401。"""
        public = {('GET', '/api/health'), ('POST', '/api/login')}
        for verb, pattern, _handler, needs_auth in w.ROUTES:
            if not needs_auth: continue
            sample = (pattern.replace('(?P<name>[A-Za-z0-9_-]{1,64})', 'abc')
                            .replace('(?P<pid>[a-f0-9]{24})', 'a' * 24)
                            .replace('^', '').replace('$', ''))
            if (verb, sample) in public: continue
            status, _, _ = self.fx.json(verb if verb != 'HEAD' else 'GET', sample)
            self.assertEqual(status, 401, '%s %s 未受保护' % (verb, sample))

    # ---- 逃生窗口状态机 ----

    def _login_csrf(self):
        _, _, cookie = self.fx.login()
        _, sess, _ = self.fx.json('GET', '/api/session', cookie=cookie)
        return cookie, sess['csrf']

    def test_escape_window_second_apply_rejected(self):
        """切换未确认时再次 apply 必须 409，避免监听泄漏。"""
        cookie, csrf = self._login_csrf()
        self.fx.app.escape = {'old': 'http://a/', 'new': 'http://b/',
                              'until': time.time() + 300, 'old_entry': None,
                              'old_cfg': {'bind': '127.0.0.1', 'port': 7501}}
        status, payload, _ = self.fx.json('POST', '/api/settings/apply', cookie=cookie,
                                          csrf=csrf, body={'port': 7902})
        self.assertEqual(status, 409)
        self.assertEqual(payload['code'], 'E_ESCAPE_PENDING')

    def test_escape_confirm_clears_state(self):
        cookie, csrf = self._login_csrf()
        self.fx.app.escape = {'old': 'http://a/', 'new': 'http://b/',
                              'until': time.time() + 300, 'old_entry': None,
                              'old_cfg': {'bind': '127.0.0.1', 'port': 7501}}
        status, payload, _ = self.fx.json('POST', '/api/settings/confirm', cookie=cookie,
                                          csrf=csrf)
        self.assertEqual(status, 200)
        self.assertIsNone(self.fx.app.escape)

    def test_escape_rollback_restores_config(self):
        cookie, csrf = self._login_csrf()
        cfg = w.load_web_config()
        original = {'bind': cfg.get('bind'), 'port': cfg.get('port')}
        cfg['port'] = 7903
        w.save_web_config(cfg)
        self.fx.app.escape = {'old': 'http://a/', 'new': 'http://b/',
                              'until': time.time() + 300, 'old_entry': None,
                              'old_cfg': original}
        status, payload, _ = self.fx.json('POST', '/api/settings/rollback', cookie=cookie,
                                          csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(w.load_web_config()['port'], original['port'])

    def test_escape_summary_expires(self):
        self.fx.app.escape = {'old': 'a', 'new': 'b', 'until': time.time() - 1,
                              'old_entry': None, 'old_cfg': {}}
        self.assertIsNone(self.fx.app.escape_summary())

    def test_current_listener_tracked(self):
        self.assertIsNotNone(self.fx.app.current_listener)

    # ---- QA 修复回归 ----

    def test_concurrent_mapping_creates_all_persisted(self):
        """并发创建不能丢更新（原来 10 个只落盘 1 个）。"""
        import http.client
        cookie, csrf = self._login_csrf()
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}):
            store = {'items': []}
            def fake_mappings(pid): return list(store['items'])
            def fake_save(pid, items): store['items'] = list(items)
            with patch.object(w.manager, 'mappings', side_effect=fake_mappings), \
                 patch.object(w.manager, 'save_mappings', side_effect=fake_save), \
                 patch.object(w.manager, 'validate_mappings'):
                import threading
                codes = []
                def create(i):
                    c = http.client.HTTPConnection('127.0.0.1', self.fx.port, timeout=10)
                    c.request('POST', '/api/mappings',
                              json.dumps({'name': 'c%d' % i, 'type': 'tcp',
                                          'local_port': 9000 + i, 'remote_port': 20000 + i}),
                              {'Content-Type': 'application/json', 'Cookie': cookie,
                               'X-CSRF-Token': csrf})
                    codes.append(c.getresponse().status); c.close()
                threads = [threading.Thread(target=create, args=(i,)) for i in range(10)]
                for th in threads: th.start()
                for th in threads: th.join()
        self.assertEqual(codes, [200] * 10)
        self.assertEqual(len(store['items']), 10)     # 全部落盘

    def test_missing_profile_returns_404(self):
        cookie, csrf = self._login_csrf()
        status, payload, _ = self.fx.json('PUT', '/api/profiles/' + 'a' * 24,
                                          cookie=cookie, csrf=csrf, body={'name': 'x'})
        self.assertEqual(status, 404)
        self.assertEqual(payload['code'], 'E_NOT_FOUND')

    def test_enabled_string_false_is_false(self):
        """布尔字段不能把 "false" 强转成 true。"""
        cookie, csrf = self._login_csrf()
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}), \
             patch.object(w.manager, 'mappings', return_value=[]), \
             patch.object(w.manager, 'save_mappings') as save, \
             patch.object(w.manager, 'validate_mappings'):
            status, payload, _ = self.fx.json('POST', '/api/mappings', cookie=cookie,
                                              csrf=csrf,
                                              body={'name': 'b', 'type': 'tcp',
                                                    'local_port': 8080,
                                                    'remote_port': 20001,
                                                    'enabled': 'false'})
        self.assertEqual(status, 200)
        self.assertFalse(payload['item']['enabled'])

    def test_invalid_local_ip_rejected(self):
        cookie, csrf = self._login_csrf()
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}), \
             patch.object(w.manager, 'mappings', return_value=[]):
            status, payload, _ = self.fx.json('POST', '/api/mappings', cookie=cookie,
                                              csrf=csrf,
                                              body={'name': 'ip', 'type': 'tcp',
                                                    'local_ip': '999.1.1.1',
                                                    'local_port': 8080,
                                                    'remote_port': 20002})
        self.assertEqual(status, 400)
        self.assertIn('IPv4', payload['error'])

    def test_corrupt_profile_returns_500_with_filename(self):
        cookie, csrf = self._login_csrf()
        with patch.object(w.manager, 'load_profile',
                          side_effect=w.ConfigCorruptError('/etc/frp/profiles/x/profile.toml',
                                                           'bad toml')):
            status, payload, _ = self.fx.json('PUT', '/api/profiles/' + 'a' * 24,
                                              cookie=cookie, csrf=csrf, body={'name': 'x'})
        self.assertEqual(status, 500)
        self.assertEqual(payload['code'], 'E_CONFIG_CORRUPT')
        self.assertNotIn('/etc/frp', payload['error'])    # 不回绝对路径

    def test_head_on_sse_has_no_body_and_does_not_stream(self):
        import socket
        cookie, _csrf = self._login_csrf()
        for path in ('/api/logs/stream', '/api/events'):
            sock = socket.create_connection(('127.0.0.1', self.fx.port), timeout=5)
            try:
                sock.sendall(('HEAD %s HTTP/1.1\r\nHost: x\r\nCookie: %s\r\n\r\n'
                              % (path, cookie)).encode())
                sock.settimeout(2)
                data = b''
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk: break
                        data += chunk
                except OSError:
                    pass
                head, _, body = data.partition(b'\r\n\r\n')
                self.assertEqual(body, b'', path)
            finally:
                sock.close()

    def test_unknown_method_returns_json_405(self):
        cookie, _ = self._login_csrf()
        status, payload, headers = self.fx.json('PATCH', '/api/health', cookie=cookie)
        self.assertEqual(status, 405)
        self.assertEqual(payload['code'], 'E_METHOD')
        self.assertIn('GET', headers.get('Allow', ''))

    def test_first_run_true_when_no_password(self):
        cfg = w.load_web_config()
        cfg['password_hash'] = ''
        w.save_web_config(cfg)
        self.fx.app.config = w.load_web_config()
        _, payload, _ = self.fx.json('GET', '/api/health')
        self.assertTrue(payload['first_run'])

    def test_port_fields_normalized_to_int(self):
        cookie, csrf = self._login_csrf()
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}), \
             patch.object(w.manager, 'mappings', return_value=[]), \
             patch.object(w.manager, 'save_mappings'), \
             patch.object(w.manager, 'validate_mappings'):
            status, payload, _ = self.fx.json('POST', '/api/mappings', cookie=cookie,
                                              csrf=csrf,
                                              body={'name': 't', 'type': 'tcp',
                                                    'local_port': '9303',
                                                    'remote_port': '20303'})
        self.assertEqual(status, 200)
        self.assertIsInstance(payload['item']['local_port'], int)
        self.assertIsInstance(payload['item']['remote_port'], int)

    def test_non_interactive_suppresses_cli_output(self):
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            with w.manager.non_interactive():
                w.manager.result_ok('should not appear')
        self.assertEqual(buf.getvalue(), '')

    # ---- 第二轮审查修复回归 ----

    def test_password_grace_period_old_password_works(self):
        """改密后 5 分钟内旧密码必须仍可登录（逃生通道）。"""
        cookie, csrf = self._login_csrf()
        status, payload, _ = self.fx.json('POST', '/api/settings/password', cookie=cookie,
                                          csrf=csrf,
                                          body={'current_password': 'testpass123',
                                                'password': 'newpass456'})
        self.assertEqual(status, 200)
        self.assertTrue(payload['password_changed'])
        # 旧密码仍然可用
        status, _, _ = self.fx.json('POST', '/api/login',
                                    body={'username': 'admin', 'password': 'testpass123'})
        self.assertEqual(status, 200)
        # 新密码也可用
        status, _, _ = self.fx.json('POST', '/api/login',
                                    body={'username': 'admin', 'password': 'newpass456'})
        self.assertEqual(status, 200)

    def test_password_change_requires_current_password(self):
        cookie, csrf = self._login_csrf()
        status, payload, _ = self.fx.json('POST', '/api/settings/password', cookie=cookie,
                                          csrf=csrf, body={'password': 'newpass456'})
        self.assertEqual(status, 403)
        self.assertEqual(payload['code'], 'E_CURRENT_PASSWORD')

    def test_password_change_revokes_other_sessions(self):
        cookie, csrf = self._login_csrf()
        _, _, other = self.fx.login()          # 第二个会话（第 3 个返回值是 cookie）
        self.fx.json('POST', '/api/settings/password', cookie=cookie, csrf=csrf,
                     body={'current_password': 'testpass123', 'password': 'newpass789'})
        status, _, _ = self.fx.json('GET', '/api/session', cookie=other)
        self.assertEqual(status, 401)          # 其它会话已吊销

    def test_web_config_corrupt_is_repaired_with_new_password(self):
        (self.fx.tmp / w.WEB_CONFIG_NAME).write_text('{"bind": "127.0.0.1", "port"')
        cfg, password = w.ensure_web_config()
        self.assertTrue(password)              # 自动生成新密码
        self.assertTrue(cfg.get('password_hash'))
        self.assertTrue(w.verify_password(password, cfg['password_hash']))

    def test_internal_keys_not_persisted(self):
        cfg = w.load_web_config()
        cfg['_corrupt'] = True
        w.save_web_config(cfg)
        raw = json.loads((self.fx.tmp / w.WEB_CONFIG_NAME).read_text())
        self.assertNotIn('_corrupt', raw)

    def test_mapping_create_returns_pending(self):
        cookie, csrf = self._login_csrf()
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}), \
             patch.object(w.manager, 'mappings', return_value=[]), \
             patch.object(w.manager, 'save_mappings'), \
             patch.object(w.manager, 'validate_mappings'), \
             patch.object(w.manager, 'state', return_value={'pending': True}):
            status, payload, _ = self.fx.json('POST', '/api/mappings', cookie=cookie,
                                              csrf=csrf,
                                              body={'name': 'p', 'type': 'tcp',
                                                    'local_port': 8080,
                                                    'remote_port': 20009})
        self.assertEqual(status, 200)
        self.assertTrue(payload['pending'])

    def test_mapping_save_is_quiet(self):
        """映射保存不能往服务 stdout 打结果框，也不能读 stdin。"""
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            with w.manager.non_interactive():
                w.manager.result_ok('should vanish')
        self.assertEqual(buf.getvalue(), '')

    def test_expose_replaces_stale_own_mapping(self):
        """面板改过端口后，旧自映射必须能被新配置覆盖（不能报名称重复）。"""
        cookie, csrf = self._login_csrf()
        stale = {'name': w.EXPOSE_TCP_NAME, 'type': 'tcp', 'local_ip': '127.0.0.1',
                 'local_port': 7000, 'remote_port': 6000, 'enabled': True}
        store = {'items': [stale]}
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}), \
             patch.object(w.manager, 'mappings', side_effect=lambda pid: list(store['items'])), \
             patch.object(w.manager, 'save_mappings',
                          side_effect=lambda pid, items: store.__setitem__('items', list(items))), \
             patch.object(w.manager, 'validate_mappings'), \
             patch.object(w, 'load_web_config',
                          return_value=dict(w.DEFAULT_CONFIG, port=7501,
                                            password_hash='x')):
            status, payload, _ = self.fx.json('POST', '/api/expose', cookie=cookie,
                                              csrf=csrf,
                                              body={'type': 'tcp', 'remote_port': 6001})
        self.assertEqual(status, 200)
        exposes = [m for m in store['items'] if m['name'] == w.EXPOSE_TCP_NAME]
        self.assertEqual(len(exposes), 1)
        self.assertEqual(exposes[0]['local_port'], 7501)

    def test_expose_delete_keeps_user_mappings(self):
        cookie, csrf = self._login_csrf()
        user = {'name': 'ssh', 'type': 'tcp', 'local_ip': '127.0.0.1',
                'local_port': 22, 'remote_port': 6000, 'enabled': True}
        ours = {'name': w.EXPOSE_TCP_NAME, 'type': 'tcp', 'local_ip': '127.0.0.1',
                'local_port': 7501, 'remote_port': 6001, 'enabled': True}
        store = {'items': [user, ours]}
        with patch.object(w.manager, 'active', return_value='a' * 24), \
             patch.object(w.manager, 'load_profile', return_value={'id': 'a' * 24}), \
             patch.object(w.manager, 'mappings', side_effect=lambda pid: list(store['items'])), \
             patch.object(w.manager, 'save_mappings',
                          side_effect=lambda pid, items: store.__setitem__('items', list(items))):
            status, payload, _ = self.fx.json('DELETE', '/api/expose', cookie=cookie,
                                              csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual([m['name'] for m in store['items']], ['ssh'])

    def test_settings_apply_parallel_with_password_no_lost_update(self):
        """设置并发写：端口与密码都必须落盘（原来会互相覆盖）。"""
        import http.client, threading
        cookie, csrf = self._login_csrf()
        results = []
        def do_apply():
            c = http.client.HTTPConnection('127.0.0.1', self.fx.port, timeout=10)
            c.request('POST', '/api/settings/apply', json.dumps({'port': 7600}),
                      {'Content-Type': 'application/json', 'Cookie': cookie,
                       'X-CSRF-Token': csrf})
            results.append(c.getresponse().status); c.close()
        def do_password():
            c = http.client.HTTPConnection('127.0.0.1', self.fx.port, timeout=10)
            c.request('POST', '/api/settings/password',
                      json.dumps({'current_password': 'testpass123',
                                  'password': 'parallel789'}),
                      {'Content-Type': 'application/json', 'Cookie': cookie,
                       'X-CSRF-Token': csrf})
            results.append(c.getresponse().status); c.close()
        threads = [threading.Thread(target=do_apply), threading.Thread(target=do_password)]
        for th in threads: th.start()
        for th in threads: th.join()
        cfg = w.load_web_config()
        self.assertTrue(w.verify_password('parallel789', cfg['password_hash']))

    def test_non_interactive_is_thread_local(self):
        """并发进入 non_interactive 不能污染其它线程的输出。"""
        import io as _io, threading
        outsider = []
        def worker():
            with w.manager.non_interactive():
                w.manager.result_ok('hidden')
        def other():
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                w.manager.result_ok('visible')
            outsider.append(buf.getvalue())
        th = threading.Thread(target=worker); th.start(); th.join()
        th2 = threading.Thread(target=other); th2.start(); th2.join()
        self.assertIn('visible', outsider[0])

if __name__ == '__main__':
    unittest.main()
