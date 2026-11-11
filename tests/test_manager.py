import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tomllib

spec = importlib.util.spec_from_file_location('manager', Path(__file__).resolve().parents[1] / 'lib' / 'manager.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.multiple(m, ROOT=self.root, TEST=True)
        self.patch.start(); m.initialize()
    def tearDown(self): self.patch.stop(); self.temp.cleanup()
    def create(self, name='生产服务器'):
        return m.create_profile({'name':name, 'server_addr':'::1', 'server_port':7000, 'dashboard_port':7500, 'tls_enable':True}, {'token':'token"\\&/', 'admin_password':'secret'})
    def mapping(self, name='ssh', port=6000):
        return {'name':name, 'type':'tcp', 'local_ip':'127.0.0.1', 'local_port':22, 'remote_port':port, 'enabled':True}
    def test_server_clients_renders_human_readable_status(self):
        m.atomic(m.ROOT / 'frps.toml', m.toml({'bindPort':7000,'webServer.addr':'127.0.0.1','webServer.port':7500,'webServer.user':'admin','webServer.password':'password'}))
        clients={'total':1,'items':[{'hostname':'内网机器','clientID':'c1','version':'0.72.0','clientIP':'127.0.0.1','online':True,'status':{'proxyCount':2}}]}
        proxies={'total':1,'items':[{'name':'web','spec':{'type':'tcp'},'status':{'phase':'online','todayTrafficIn':1,'todayTrafficOut':2,'curConns':3}}]}
        with patch.object(m,'api',side_effect=[clients,proxies]), contextlib.redirect_stdout(io.StringIO()) as output: m.server_clients()
        shown=output.getvalue();self.assertIn('内网机器 [在线]',shown);self.assertIn('代理数量=2',shown);self.assertIn('web [online]',shown);self.assertNotIn('password',shown)
    def test_backup_and_restore_roundtrip(self):
        pid=self.create();m.set_current(pid);m.save_mappings(pid,[self.mapping()])
        directory=m.backup(m.managed_paths(),'test-snapshot')
        self.assertTrue((directory/'manifest.json').exists())
        m.save_mappings(pid,[])
        self.assertEqual(m.mappings(pid),[])
        restored=m.restore_backup(directory)
        self.assertTrue(restored)
        self.assertEqual([x['name'] for x in m.mappings(pid)],['ssh'])
    def test_restore_rejects_tampered_manifest(self):
        pid=self.create();directory=m.backup(m.managed_paths(),'test')
        m.save_json(directory/'manifest.json',{'files':['../../escape']})
        with self.assertRaises(ValueError):m.restore_backup(directory)
    def test_diagnostics_report_machine_readable(self):
        m.atomic(m.ROOT/'frps.toml',m.toml({'bindPort':7000}))
        with patch.object(m,'service',return_value='active'),patch.object(m,'firewall_hint',return_value=[]):
            report=m.diagnostics_report('frps')
        self.assertEqual(report['component'],'frps');self.assertTrue(report['checks'][0]['ok']);self.assertNotIn('token',json.dumps(report))
    def test_upgrade_rejects_malformed_version(self):
        with patch.object(m,'confirm',return_value=True):
            with self.assertRaises(ValueError): m.upgrade('frps', 'not-a-version!')
    def test_upgrade_keeps_config_when_download_fails(self):
        m.atomic(m.ROOT/'frps.toml', m.toml({'bindPort':7000}))
        with patch.object(m,'confirm',return_value=True), patch.object(m,'_https_get',side_effect=OSError('offline')):
            with self.assertRaises(ValueError): m.upgrade('frps')
        self.assertEqual(m.read_toml(m.ROOT/'frps.toml')['bindPort'], 7000)
    def test_logs_menu_cleans_rotated_only(self):
        (m.ROOT/'logs').mkdir(exist_ok=True)
        current=m.ROOT/'logs'/'frps.log'; current.write_text('current\n')
        rotated=m.ROOT/'logs'/'frps.log.123'; rotated.write_text('old\n')
        with patch.object(m,'clear_screen'),patch.object(m,'pause'),patch('builtins.input',side_effect=['4','0']):
            m.logs_and_diagnostics_menu('frps')
        self.assertTrue(current.exists()); self.assertFalse(rotated.exists())
    def test_show_logs_line_limit(self):
        (m.ROOT/'logs').mkdir(exist_ok=True)
        (m.ROOT/'logs'/'frpc.log').write_text('\n'.join(f'line{i}' for i in range(100)))
        out=io.StringIO()
        with contextlib.redirect_stdout(out): m.show_logs('frpc', 10)
        shown=out.getvalue()
        self.assertIn('line99', shown); self.assertNotIn('line80', shown)
    def test_profile_transport_and_log_fields_render(self):
        pid=self.create(); p=m.load_profile(pid); p.update({'transport_protocol':'kcp','log_level':'warn','log_max_days':7,'transport_use_encryption':True})
        m.atomic(m.pdir(pid)/'profile.toml', m.toml(p))
        rendered=m.render(pid)
        self.assertIn('transport.protocol = "kcp"', rendered)
        self.assertIn('log.level = "warn"', rendered)
        self.assertIn('log.maxDays = 7', rendered)
        self.assertIn('transport.useEncryption = true', rendered)
    def test_backup_captures_pre_change_state(self):
        pid=self.create();m.set_current(pid)
        m.save_mappings(pid,[self.mapping()])
        # Backup is taken before writing, so newest backup holds the empty pre-change mappings.
        newest=m.list_backups()[0]
        self.assertIn(f'profiles/{pid}/mappings.toml',newest['files'])
        self.assertEqual(tomllib.loads((newest['path']/f'profiles/{pid}/mappings.toml').read_text()).get('mappings',[]),[])
        m.save_mappings(pid,[])
        previous=m.list_backups()[0]
        m.restore_backup(previous['path'])
        self.assertEqual([x['name'] for x in m.mappings(pid)],['ssh'])
    def test_backup_menu_lists_and_cancels(self):
        pid=self.create();m.set_current(pid);m.save_mappings(pid,[self.mapping()])
        with patch.object(m,'clear_screen'),patch.object(m,'pause'),patch.object(m,'confirm',return_value=False),patch('builtins.input',side_effect=['1','0']):
            m.backup_menu()
        self.assertEqual([x['name'] for x in m.mappings(pid)],['ssh'])
    def test_nested_toml_roundtrip(self):
        data={'bindPort':7000,'auth':{'method':'token','token':'abcdefgh'},'webServer':{'addr':'127.0.0.1','port':7500,'user':'admin','password':'pw'},'proxies':[{'name':'a','type':'tcp','localPort':80,'remotePort':6000}]}
        parsed=tomllib.loads(m.toml(data));self.assertEqual(parsed,data)
    def test_frps_config_validation_and_redaction(self):
        m.atomic(m.ROOT/'frps.toml',m.toml({'bindPort':7000,'auth':{'method':'token','token':'supersecret'},'webServer':{'addr':'127.0.0.1','port':7500,'user':'admin','password':'pw'}}))
        data=m.read_frps_config();m.validate_frps_config(data)
        with self.assertRaises(ValueError):m.validate_frps_config({'bindPort':70000})
        with self.assertRaises(ValueError):m.validate_frps_config({'bindPort':7000,'auth':{'method':'token','token':'short'}})
        with self.assertRaises(ValueError):m.validate_frps_config({'bindPort':7000,'webServer':{'port':7500}})
        out=io.StringIO()
        with contextlib.redirect_stdout(out),patch('builtins.input',side_effect=['1','0']),patch.object(m,'clear_screen'),patch.object(m,'pause'):
            m.frps_config_menu()
        self.assertIn('******',out.getvalue());self.assertNotIn('supersecret',out.getvalue());self.assertNotIn('"pw"',out.getvalue())
    def test_diagnostics_reports_unreachable(self):
        m.atomic(m.ROOT/'frps.toml',m.toml({'bindPort':7000}))
        with patch.object(m,'service',return_value='inactive'),patch.object(m,'firewall_hint',return_value=[]),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(m.diagnostics('frps'))
        self.assertIn('服务状态',out.getvalue())
    def test_server_menu_refreshes_once_and_hides_secrets(self):
        m.atomic(m.ROOT / 'frps.toml', m.toml({'bindPort':7000,'auth.token':'do-not-print-token','webServer.addr':'127.0.0.1','webServer.port':7500,'webServer.user':'admin','webServer.password':'do-not-print-password'}))
        output=io.StringIO()
        with contextlib.redirect_stdout(output), patch('builtins.input', side_effect=['1','0']): m.main_menu('frps')
        shown=output.getvalue()
        self.assertEqual(shown.count('frps 服务端管理'), 1)
        self.assertEqual(shown.count('状态'.ljust(10)), 1)
        self.assertIn('[提示] 正在刷新服务信息。', shown)
        self.assertIn('监听'.ljust(10) + '0.0.0.0:7000', shown)
        self.assertIn('Dashboard http://127.0.0.1:7500', shown)
        self.assertIn('1) 查看服务信息（状态、版本、路径）', shown)
        self.assertNotIn('2) 查看版本信息', shown)
        self.assertNotIn('3) 查看安装路径', shown)
        for hidden in ('"component"','do-not-print-token','do-not-print-password'): self.assertNotIn(hidden,shown)
    def test_frpc_menu_labels_and_json_status(self):
        pid=self.create();m.set_current(pid)
        output=io.StringIO()
        with contextlib.redirect_stdout(output), patch('builtins.input', side_effect=['1','0']): m.main_menu('frpc')
        shown=output.getvalue();self.assertEqual(shown.count('frpc 客户端管理'),1)
        self.assertIn('生产服务器（[::1]:7000）',shown)
        self.assertIn('连接状态'.ljust(10) + '已停止', shown);self.assertNotIn('"component"',shown)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):m.run(['frpc','status','--json'])
        self.assertEqual(json.loads(output.getvalue())['component'],'frpc')
    def test_frps_status_independent_of_broken_client_profile(self):
        m.save_json(m.ROOT/'state.json',{'active_profile_id':'invalid'})
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input',return_value='0'):m.main_menu('frps')
    def test_unicode_name_and_unique_rename(self):
        pid = self.create(); self.assertIn('[::1]:7000', m.label(m.load_profile(pid)))
        with self.assertRaises(ValueError): self.create()
        for bad in ('生产 服务器', '../x', 'abc\n', 'a;touch'):
            with self.assertRaises(ValueError): m.name(bad)
    def test_secret_escaping_roundtrip(self):
        pid = self.create(); config = tomllib.loads(m.render(pid))
        self.assertEqual(config['auth']['token'], 'token"\\&/')
        if os.name != 'nt': self.assertEqual((m.pdir(pid) / 'secrets.toml').stat().st_mode & 0o777, 0o600)
    def test_profile_mapping_isolation_and_disable(self):
        a = self.create('服务器A'); b = self.create('服务器B')
        m.set_current(a); m.save_mappings(a, [self.mapping('only_a')])
        m.set_current(b); m.save_mappings(b, [self.mapping('only_b')])
        self.assertEqual([x['name'] for x in tomllib.loads(m.render(a))['proxies']], ['only_a'])
        self.assertEqual([x['name'] for x in tomllib.loads(m.render(b))['proxies']], ['only_b'])
        with self.assertRaises(ValueError): m.save_mappings(a, [])
        disabled = self.mapping('only_b'); disabled['enabled'] = False
        m.save_mappings(b, [disabled]); self.assertNotIn('proxies', tomllib.loads(m.render(b)))
        m.save_mappings(b, []); self.assertEqual(m.mappings(b), [])
    def test_duplicate_port_reject_and_restore(self):
        pid = self.create(); m.set_current(pid); m.save_mappings(pid, [self.mapping()])
        original = (m.ROOT / 'frpc.toml').read_bytes()
        with self.assertRaises(ValueError): m.save_mappings(pid, [self.mapping(), self.mapping('duplicate')])
        self.assertEqual((m.ROOT / 'frpc.toml').read_bytes(), original)
        self.assertEqual(len(m.mappings(pid)), 1)
    def test_verify_failure_does_not_overwrite(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        original = (m.ROOT / 'frpc.toml').read_bytes()
        with patch.object(m, 'verify', side_effect=ValueError('invalid')):
            with self.assertRaises(ValueError): m.save_mappings(pid, [self.mapping()])
        self.assertEqual((m.ROOT / 'frpc.toml').read_bytes(), original)
        self.assertEqual(m.mappings(pid), [])
    def test_external_import_and_disabled_preservation(self):
        pid = self.create(); m.set_current(pid)
        item = self.mapping('disabled'); item['enabled'] = False
        m.save_mappings(pid, [item])
        content = m.render(pid).replace('serverPort = 7000', 'serverPort = 8000')
        m.atomic(m.ROOT / 'frpc.toml', content)
        with patch('builtins.input', return_value='1'): m.import_external('frpc')
        self.assertEqual(m.load_profile(pid)['server_port'], 8000)
        self.assertEqual(m.mappings(pid)[0]['enabled'], False)
    def test_external_unknown_rejected(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        content = m.render(pid) + '\nincludes = ["other/*.toml"]\n'
        m.atomic(m.ROOT / 'frpc.toml', content)
        with patch('builtins.input', return_value='1'):
            with self.assertRaises(ValueError): m.import_external('frpc')
        self.assertEqual((m.ROOT / 'frpc.toml').read_text(), content)
    def test_choose_retries_invalid(self):
        with patch('builtins.input', side_effect=['', '-1', 'text', '999', '1']): self.assertEqual(m.choose(['x']), 1)
    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError): m.pdir('../escape')
    def test_lock_exclusion(self):
        with m.lock():
            with self.assertRaises(ValueError):
                with m.lock(): pass
    def test_late_failure_restores_all_files(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        before = {p: p.read_bytes() for p in m.managed_paths()}
        with patch.object(m, 'mark_hash', side_effect=ValueError('injected after runtime write')):
            with self.assertRaises(ValueError): m.save_mappings(pid, [self.mapping()])
        self.assertEqual({p: p.read_bytes() for p in m.managed_paths()}, before)
    def test_switch_failure_restores_active_and_runtime(self):
        first = self.create('A'); second = self.create('B'); m.set_current(first); m.apply(first)
        before = {p: p.read_bytes() for p in m.managed_paths()}
        with patch.object(m, 'mark_hash', side_effect=ValueError('injected')):
            with self.assertRaises(ValueError): m.switch(second)
        self.assertEqual({p: p.read_bytes() for p in m.managed_paths()}, before)
        self.assertEqual(m.active(), first)
    def test_frps_keep_restores_canonical(self):
        m.atomic(m.ROOT / 'frps.toml', 'bindPort = 7000\n'); m.mark_hash('frps')
        m.atomic(m.ROOT / 'frps.toml', 'bindPort = 8000\n')
        with patch('builtins.input', side_effect=['2', 'y']): m.import_external('frps')
        self.assertEqual(m.read_toml(m.ROOT / 'frps.toml')['bindPort'], 7000)
    def test_unknown_log_import_is_not_dropped(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        external = m.render(pid).replace('log.maxDays = 30', 'log.maxDays = 7\nlog.timestampFormat = "2006"')
        m.atomic(m.ROOT / 'frpc.toml', external)
        with patch('builtins.input', return_value='1'):
            with self.assertRaises(ValueError): m.import_external('frpc')
        self.assertEqual((m.ROOT / 'frpc.toml').read_text(), external)
    def test_log_and_transport_import_roundtrip(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        external = m.render(pid).replace('log.maxDays = 30', 'log.maxDays = 7').replace('transport.protocol = "tcp"', 'transport.protocol = "kcp"')
        m.atomic(m.ROOT / 'frpc.toml', external)
        with patch('builtins.input', return_value='1'): m.import_external('frpc')
        p = m.load_profile(pid)
        self.assertEqual(p['log_max_days'], 7); self.assertEqual(p['transport_protocol'], 'kcp')
        self.assertIn('transport.protocol = "kcp"', m.render(pid))
    def test_connection_timeout_is_bounded(self):
        pid = self.create()
        with patch.object(m, 'connection', return_value={'state':'connecting'}), patch.object(m.time, 'monotonic', side_effect=[0, 30]), patch.object(m.time, 'sleep') as sleeper:
            self.assertFalse(m.wait_connection(pid))
            sleeper.assert_not_called()
    def test_service_process_alone_is_not_online(self):
        pid = self.create()
        with patch.object(m, 'running', return_value=True), patch.object(m, 'api', return_value={}):
            self.assertEqual(m.connection(pid)['state'], 'unknown')
    def test_login_log_proves_connected_with_zero_proxies(self):
        # Regression: frps shows the client online but frpc showed "connecting"
        # because the Dashboard fallback timed out and masked a successful login.
        pid = self.create()
        (m.ROOT / 'logs').mkdir(exist_ok=True)
        (m.ROOT / 'logs' / 'frpc.log').write_text(
            'start frpc service for config file\n'
            'try to connect to server...\n'
            'login to server success, get run id [abc]\n'
            'http response [/api/status]: code [200]\n')
        with patch.object(m, 'running', return_value=True), patch.object(m, 'api', side_effect=OSError('dashboard unreachable')):
            result = m.connection(pid)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(result.get('source'), 'frpc-log')
    def test_admin_api_online_status_is_connected(self):
        # frp 0.72 /api/status reports "online", not "running".
        pid = self.create()
        with patch.object(m, 'running', return_value=True), patch.object(m, 'api', return_value={'tcp': [{'name': 'ssh', 'status': 'online'}]}):
            self.assertEqual(m.connection(pid)['state'], 'connected')
    def test_reconnect_after_login_is_not_connected(self):
        pid = self.create()
        (m.ROOT / 'logs').mkdir(exist_ok=True)
        (m.ROOT / 'logs' / 'frpc.log').write_text(
            'start frpc service\nlogin to server success\n'
            'connect to server error: i/o timeout\n')
        with patch.object(m, 'running', return_value=True), patch.object(m, 'api', side_effect=OSError('unreachable')):
            self.assertEqual(m.connection(pid)['state'], 'connecting')
    def test_foreign_profile_import_is_rejected(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        external = m.render(pid).replace('clientID = "' + pid + '"', 'clientID = "other"')
        m.atomic(m.ROOT / 'frpc.toml', external)
        with patch('builtins.input', return_value='1'):
            with self.assertRaises(ValueError): m.import_external('frpc')
        self.assertEqual((m.ROOT / 'frpc.toml').read_text(), external)
    def test_cancel_configuration_preserves_disk(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        before = {p: p.read_bytes() for p in m.managed_paths()}
        with patch('builtins.input', side_effect=['2','新名称','0']): m.edit_profile()
        self.assertEqual({p: p.read_bytes() for p in m.managed_paths()}, before)
    def test_http_requires_domain(self):
        item = self.mapping(); item['type'] = 'http'
        with self.assertRaises(ValueError): m.validate_mapping(item)

    # ---- Python 3.6 fallback parser (_toml_lib is None) ----
    def test_mini_toml_matches_tomllib(self):
        content = m.toml({'bindAddr': '0.0.0.0', 'bindPort': 7000,
                          'auth.method': 'token', 'auth.token': 'SUPERSECRET',
                          'log.to': '/x', 'log.maxDays': 30, 'log.disablePrintColor': True,
                          'webServer.port': 7500, 'webServer.user': 'admin',
                          'webServer.password': 'DASHSECRET',
                          'transport': {'tls': {'enable': True}},
                          'proxies': [{'name': 'a', 'type': 'tcp'}]})
        with patch.object(m, '_toml_lib', None):
            self.assertEqual(m._load_toml(content), tomllib.loads(content))
    def test_mini_toml_escape_roundtrip(self):
        values = ['a\\nb', 'tok\\rvalue', 'ctrl\\u0001x', 'C:\\path\\to\\cert', 'quote"x', '\u4e2d\u6587']
        with patch.object(m, '_toml_lib', None):
            for value in values:
                self.assertEqual(m._load_toml(m.toml({'k': value}))['k'], value)
    def test_mini_toml_inline_and_numeric(self):
        with patch.object(m, '_toml_lib', None):
            self.assertEqual(m._load_toml('a = {b = 1, c.d = 2}'), {'a': {'b': 1, 'c': {'d': 2}}})
            self.assertEqual(m._load_toml('a = 0x10'), {'a': 16})
            self.assertEqual(m._load_toml('a = 1_000'), {'a': 1000})
            self.assertEqual(m._load_toml('a = ["x,y", 2]'), {'a': ['x,y', 2]})
    def test_mini_toml_redaction_keys_are_nested(self):
        # Regression: flat dotted keys bypassed the secret redaction in config show.
        content = m.toml({'auth.token': 'SUPERSECRET', 'webServer.password': 'DASHSECRET'})
        with patch.object(m, '_toml_lib', None):
            data = m._load_toml(content)
        self.assertEqual(data['auth']['token'], 'SUPERSECRET')
        self.assertEqual(data['webServer']['password'], 'DASHSECRET')
    def test_toml_rejects_none(self):
        with self.assertRaises(ValueError): m.toml({'k': None})

    # ---- Input layer: blank / q-cancel must never persist None ----
    def test_ask_allow_empty_returns_blank(self):
        with patch('builtins.input', return_value=''):
            self.assertEqual(m.ask('可留空', allow_empty=True), '')
    def test_ask_q_returns_none(self):
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', return_value='q'):
            self.assertIsNone(m.ask('字段'))
    def test_edit_profile_q_cancel_does_not_write(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        before = {p: p.read_bytes() for p in m.managed_paths()}
        with patch('builtins.input', return_value='q'):
            m.edit_profile()
        self.assertEqual({p: p.read_bytes() for p in m.managed_paths()}, before)
    def test_edit_profile_blank_token_clears(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        with patch('builtins.input', side_effect=['5', '10']), patch('getpass.getpass', return_value=''), \
             patch.object(m, 'confirm', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            m.edit_profile()
        self.assertEqual(m.secrets_for(pid)['token'], '')
    def test_redact_secrets_by_field_name(self):
        data = {'auth': {'token': 'SUPERSECRET'}, 'webServer': {'password': 'DASHSECRET', 'tls': {'certFile': '/x'}},
                'nested': {'password': 'HIDDEN'}}
        out = json.dumps(m.redact_secrets(data))
        self.assertNotIn('SUPERSECRET', out); self.assertNotIn('DASHSECRET', out); self.assertNotIn('HIDDEN', out)
        self.assertIn('/x', out)
    def test_frps_config_show_redacts_on_mini_toml(self):
        m.atomic(m.ROOT / 'frps.toml', m.toml({'bindPort': 7000, 'auth.token': 'SUPERSECRET',
                                               'webServer.user': 'admin', 'webServer.password': 'DASHSECRET'}))
        out = io.StringIO()
        with patch.object(m, '_toml_lib', None), contextlib.redirect_stdout(out):
            m.run(['frps', 'config', 'show'])
        self.assertNotIn('SUPERSECRET', out.getvalue()); self.assertNotIn('DASHSECRET', out.getvalue())
    # ---- Service control / connection state / pending ----
    def test_service_feedback_actually_calls_service(self):
        with patch.object(m, 'service', side_effect=[None, 'active']) as svc, contextlib.redirect_stdout(io.StringIO()):
            m.service_feedback('frpc', 'start')
        calls = [c[0][1] for c in svc.call_args_list]
        self.assertIn('start', calls)
        self.assertIn('status', calls)
    def test_login_success_does_not_mask_proxy_error(self):
        pid = self.create(); m.set_current(pid)
        with patch.object(m, 'apply'):
            m.save_mappings(pid, [self.mapping()])  # an enabled proxy must reach the admin API
        (m.ROOT / 'logs').mkdir(exist_ok=True)
        (m.ROOT / 'logs' / 'frpc.log').write_text('start frpc service\nlogin to server success\n')
        with patch.object(m, 'running', return_value=True), \
             patch.object(m, 'api', return_value={'tcp': [{'name': 'ssh', 'status': 'error'}]}):
            self.assertEqual(m.connection(pid)['state'], 'proxy-error')
    def test_zero_proxies_skips_network_probes(self):
        pid = self.create(); m.set_current(pid)
        m._CONN_CACHE.clear()
        (m.ROOT / 'logs').mkdir(exist_ok=True)
        (m.ROOT / 'logs' / 'frpc.log').write_text('start frpc service\nlogin to server success\n')
        n = [0]
        def fake_api(*a, **k):
            n[0] += 1; raise OSError('unreachable')
        with patch.object(m, 'running', return_value=True), patch.object(m, 'api', side_effect=fake_api):
            self.assertEqual(m.connection(pid, fresh=True)['state'], 'connected')
        self.assertEqual(n[0], 0)  # no blocking API probes on the menu path
    def test_auth_failure_detected_from_frp_072_log(self):
        pid = self.create()
        (m.ROOT / 'logs').mkdir(exist_ok=True)
        (m.ROOT / 'logs' / 'frpc.log').write_text('start frpc service\nUser authentication failed\n')
        with patch.object(m, 'running', return_value=True), patch.object(m, 'api', side_effect=OSError('x')):
            self.assertEqual(m.connection(pid)['state'], 'auth-failed')
    def test_apply_sets_pending_when_running(self):
        pid = self.create(); m.set_current(pid)
        with patch.object(m, 'running', return_value=True):
            m.apply(pid)
        self.assertTrue(m.state()['pending'])
    def test_pending_cleared_after_restart(self):
        pid = self.create(); m.set_current(pid)
        m.save_json(m.ROOT / 'state.json', {'active_profile_id': pid, 'pending': True})
        with patch.object(m, 'TEST', False), patch.object(m, 'run_cmd') as rc, \
             patch.object(m.shutil, 'which', return_value='/bin/systemctl'):
            rc.return_value = type('R', (), {'returncode': 0, 'stdout': 'active', 'stderr': ''})()
            m.run(['frpc', 'restart'])
        self.assertFalse(m.state()['pending'])
    def test_status_json_survives_broken_profile(self):
        m.save_json(m.ROOT / 'state.json', {'active_profile_id': 'invalid'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.run(['frpc', 'status'])
        self.assertEqual(json.loads(out.getvalue())['component'], 'frpc')
        self.assertIsNone(json.loads(out.getvalue())['active_profile'])
    def test_main_menu_survives_broken_profile(self):
        m.save_json(m.ROOT / 'state.json', {'active_profile_id': 'invalid'})
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', return_value='0'):
            m.main_menu('frpc')  # must not raise UnboundLocalError

    # ---- Transaction / lock / robustness ----
    def test_lock_clears_stale_directory(self):
        stale = m.ROOT / '.operation.lock'; stale.mkdir()
        (stale / 'owner.json').write_text(json.dumps({'pid': 99999999, 'time': 0}))
        with m.lock():
            self.assertTrue(stale.exists())
        self.assertFalse(stale.exists())
    def test_lock_rejects_live_holder(self):
        live = m.ROOT / '.operation.lock'; live.mkdir()
        (live / 'owner.json').write_text(json.dumps({'pid': os.getpid(), 'time': __import__('time').time()}))
        with self.assertRaises(ValueError):
            with m.lock(): pass
        m.shutil.rmtree(live)
    def test_transaction_rolls_back_on_baseexception(self):
        original = m.ROOT / 'state.json'
        before = original.read_bytes()
        with self.assertRaises(KeyboardInterrupt):
            with m.lock(), m.transaction():
                m.atomic(original, '{"changed": true}\n')
                raise KeyboardInterrupt()
        self.assertEqual(original.read_bytes(), before)
    def test_port_rejects_unicode_digits(self):
        with self.assertRaises(ValueError): m.port('７０００')
        self.assertEqual(m.port(7000), 7000)
    def test_rotate_logs_has_no_time_ns(self):
        import inspect
        self.assertNotIn('time_ns', inspect.getsource(m.rotate_logs))
    def test_import_external_missing_field_is_readable(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        m.atomic(m.ROOT / 'frpc.toml', m.render(pid).replace('serverPort = 7000', ''))
        with patch.object(m, '_login_state_from_log', return_value=None), \
             patch.object(m, 'running', return_value=False), patch('builtins.input', return_value='1'):
            with self.assertRaises(ValueError) as ctx: m.import_external('frpc')
        self.assertIn('serverPort', str(ctx.exception))

    # ---- Upgrade transaction / mode / HTTPS ----
    def test_upgrade_rejects_non_https_mirror(self):
        with patch.object(m, 'confirm', return_value=True), \
             patch.dict(os.environ, {'FRP_MIRROR_URL': 'http://evil.example'}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError): m.upgrade('frpc')
    def test_upgrade_rejects_off_mirror(self):
        with patch.object(m, 'confirm', return_value=True), \
             patch.dict(os.environ, {'FRP_MIRROR_URL': 'off'}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError): m.upgrade('frpc')
    def test_upgrade_stages_all_files_and_restores_mode(self):
        import tarfile, hashlib, tempfile, io as _io
        files = {
            'frp-simple-bash/lib/manager.py': b'# new manager\n',
            'frp-simple-bash/lib/download.sh': b'# dl\n',
            'frp-simple-bash/install-common.sh': b'# ic\n',
            'frp-simple-bash/install-frps.sh': b'# is\n',
            'frp-simple-bash/install-frpc.sh': b"FRP_MANAGER_VERSION='v9.9.9-test'\n",
            'frp-simple-bash/install-windows.ps1': b'# iw\n',
            'frp-simple-bash/install-frps.ps1': b'# ips\n',
            'frp-simple-bash/install-frpc.ps1': b'# ipc\n',
            'frp-simple-bash/uninstall.sh': b'# u\n',
            'frp-simple-bash/uninstall-frps.ps1': b'# ups\n',
            'frp-simple-bash/uninstall-frpc.ps1': b'# upc\n',
            'frp-simple-bash/uninstall-windows.ps1': b'# uw\n',
            'frp-simple-bash/bin/frpc': b'#!/bin/bash\n',
            'frp-simple-bash/templates/frpc.service': b'[Unit]\n',
        }
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o755
                tar.addfile(info, _io.BytesIO(data))
        bundle = buf.getvalue()
        manifest = hashlib.sha256(bundle).hexdigest() + '  source.tar.gz\n'
        install_root = Path(tempfile.mkdtemp())
        (install_root / 'install-frpc.sh').write_text("FRP_MANAGER_VERSION='v0.0.1-old'\n")
        with patch.object(m, 'INSTALL_ROOT', install_root), patch.object(m, 'confirm', return_value=True), \
             patch.object(m, '_https_get', side_effect=[bundle, manifest.encode()]), \
             patch.object(m, 'installed_frp_version', return_value='0.72.0'), \
             contextlib.redirect_stdout(io.StringIO()):
            m.upgrade('frpc', 'v9.9.9-test')
        self.assertIn('v9.9.9-test', (install_root / 'install-frpc.sh').read_text())
        self.assertTrue((install_root / 'install-windows.ps1').exists())
        self.assertEqual((install_root / 'bin' / 'frpc').stat().st_mode & 0o111, 0o111)
    def test_upgrade_rejects_incomplete_bundle(self):
        import tarfile, hashlib, io as _io
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            data = b'# only manager\n'
            info = tarfile.TarInfo('frp-simple-bash/lib/manager.py'); info.size = len(data); tar.addfile(info, _io.BytesIO(data))
        bundle = buf.getvalue(); manifest = hashlib.sha256(bundle).hexdigest() + '  source.tar.gz\n'
        with patch.object(m, 'confirm', return_value=True), \
             patch.object(m, '_https_get', side_effect=[bundle, manifest.encode()]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError): m.upgrade('frpc', 'v9.9.9-test')
    def test_within_works_on_windows_paths(self):
        from pathlib import PureWindowsPath
        # _within must not depend on '/' string prefixes.
        self.assertTrue(m._within(Path('/a/b/c'), Path('/a/b')))
        self.assertFalse(m._within(Path('/a/../b'), Path('/a')) if os.name != 'nt' else True)

    # ---- Menu: status screen must stay visible and not block on double probes ----
    def test_show_status_accepts_precomputed_data(self):
        pid = self.create(); data = m.status('frpc')
        calls = []
        with patch.object(m, 'status', side_effect=lambda r: (calls.append(r) or data)), \
             contextlib.redirect_stdout(io.StringIO()):
            m.show_status('frpc', data)
        self.assertEqual(calls, [])  # no second status() / API probe
    def test_menu_status_action_pauses(self):
        m.atomic(m.ROOT / 'frps.toml', m.toml({'bindPort': 7000}))
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', side_effect=['1', '0']), \
             patch.object(m, 'service', return_value='active'), patch.object(m, 'pause') as paused:
            m.main_menu('frps')
        self.assertGreaterEqual(paused.call_count, 1)
    def test_connection_cache_avoids_repeated_api_probes(self):
        pid = self.create(); m.set_current(pid)
        with patch.object(m, 'apply'):
            m.save_mappings(pid, [self.mapping()])  # enabled proxy -> admin API is consulted
        m._CONN_CACHE.clear()
        n = [0]
        def fake_api(*a, **k):
            n[0] += 1
            return {'tcp': [{'name': 'ssh', 'status': 'online'}]}
        with patch.object(m, 'running', return_value=True), patch.object(m, 'api', side_effect=fake_api), \
             patch.object(m, '_login_state_from_log', return_value=None):
            r1 = m.connection(pid); r2 = m.connection(pid)
        self.assertEqual(r1, r2)
        self.assertEqual(n[0], 1)  # second call served from cache
    def test_wait_connection_uses_fresh_state(self):
        pid = self.create()
        seq = [{'state': 'connecting'}, {'state': 'connected'}]
        with patch.object(m, 'connection', side_effect=lambda p, fresh=False: (self.assertTrue(fresh) or seq.pop(0))), \
             patch.object(m.time, 'monotonic', side_effect=[0, 0, 0, 100]), patch.object(m.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(m.wait_connection(pid))

    def test_frps_config_menu_preserves_webserver_tls(self):
        m.atomic(m.ROOT / 'frps.toml', m.toml({'bindPort': 7000,
                                               'webServer.addr': '127.0.0.1', 'webServer.port': 7500,
                                               'webServer.user': 'admin', 'webServer.password': 'pw',
                                               'webServer.tls.certFile': '/cert.pem'}))
        with patch('builtins.input', side_effect=['4', 'admin', '127.0.0.1', '7500', '0']), \
             patch('getpass.getpass', return_value=''), patch.object(m, 'clear_screen'), \
             patch.object(m, 'pause'), patch.object(m, 'save_frps_config', side_effect=lambda d: m.atomic(m.ROOT / 'frps.toml', m.toml(d))):
            m.frps_config_menu()
        data = m.read_frps_config()
        self.assertEqual(data['webServer']['tls']['certFile'], '/cert.pem')

if __name__ == '__main__': unittest.main()
