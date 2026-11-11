import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
try:
    import tomllib                      # Python 3.11+
except ImportError:                     # Python 3.6-3.10 fallback
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

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
        with patch.object(m,'clear_screen'),patch.object(m,'pause'),patch('builtins.input',side_effect=['5','0']):
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
        before=set(str(b['path']) for b in m.list_backups())
        with patch.object(m, 'apply'):
            m.save_mappings(pid,[self.mapping()])
        # The first write must leave a pre-change snapshot holding the empty state.
        first=[b for b in m.list_backups() if str(b['path']) not in before
               and f'profiles/{pid}/mappings.toml' in b['files']]
        self.assertTrue(first, 'save_mappings must snapshot mappings.toml before writing')
        empty=min(first, key=lambda b: b['path'].stat().st_mtime)
        self.assertEqual(tomllib.loads((empty['path']/f'profiles/{pid}/mappings.toml').read_text()).get('mappings',[]),[])
        # The second write snapshots the state that still contains 'ssh'.
        seen=set(str(b['path']) for b in m.list_backups())
        with patch.object(m, 'apply'):
            m.save_mappings(pid,[])
        second=[b for b in m.list_backups() if str(b['path']) not in seen
                and f'profiles/{pid}/mappings.toml' in b['files']]
        self.assertTrue(second, 'second save_mappings must snapshot the previous mappings')
        keep=max(second, key=lambda b: b['path'].stat().st_mtime)
        m.restore_backup(keep['path'])
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

    # ---- port conflict must not flag the component itself ----
    def test_check_port_conflict_tcp_uses_listen_state(self):
        # ss State is LISTEN for TCP; the check must not filter by 'tcp' prefix.
        line = 'LISTEN 0 128 0.0.0.0:7000 0.0.0.0:* users:((\"nginx\",pid=999,fd=3))'
        with patch.object(m.shutil, 'which', return_value='/usr/sbin/ss'), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R', (), {'stdout': line + '\n', 'stderr': ''})()
            self.assertTrue(m.check_port_conflict('0.0.0.0', 7000, proto='tcp', exclude_role='frps'))
    def test_check_port_conflict_udp_uses_unconn_state(self):
        line = 'UNCONN 0 0 0.0.0.0:7000 0.0.0.0:* users:((\"dnsmasq\",pid=77,fd=3))'
        with patch.object(m.shutil, 'which', return_value='/usr/sbin/ss'), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R', (), {'stdout': line + '\n', 'stderr': ''})()
            self.assertTrue(m.check_port_conflict('0.0.0.0', 7000, proto='udp'))
    def test_check_port_conflict_ignores_wrong_protocol(self):
        # A UDP listener must NOT count as a TCP conflict.
        line = 'UNCONN 0 0 0.0.0.0:7000 0.0.0.0:* users:((\"dnsmasq\",pid=77,fd=3))'
        with patch.object(m.shutil, 'which', return_value='/usr/sbin/ss'), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R', (), {'stdout': line + '\n', 'stderr': ''})()
            self.assertFalse(m.check_port_conflict('0.0.0.0', 7000, proto='tcp'))

    # ---- port conflict must not flag the component itself ----
    def test_check_port_conflict_excludes_self_role(self):
        # frps itself listening on 7000 must NOT be reported as a conflict.
        line = 'LISTEN 0 128 0.0.0.0:7000 0.0.0.0:* users:((\"frps\",pid=12345,fd=3))'
        with patch.object(m.shutil, 'which', return_value='/usr/sbin/ss'), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R', (), {'stdout': line + '\n', 'stderr': ''})()
            self.assertFalse(m.check_port_conflict('0.0.0.0', 7000, exclude_role='frps'))
    def test_check_port_conflict_flags_other_process(self):
        line = 'LISTEN 0 128 0.0.0.0:7000 0.0.0.0:* users:((\"nginx\",pid=999,fd=3))'
        with patch.object(m.shutil, 'which', return_value='/usr/sbin/ss'), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R', (), {'stdout': line + '\n', 'stderr': ''})()
            self.assertTrue(m.check_port_conflict('0.0.0.0', 7000, exclude_role='frps'))
    def test_check_port_conflict_none_when_ss_missing(self):
        with patch.object(m.shutil, 'which', return_value=None):
            self.assertIsNone(m.check_port_conflict('0.0.0.0', 7000))
    def test_result_error_quotes_frps_log_in_frps_menu(self):
        m._CURRENT_ROLE = 'frps'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.result_error('测试失败')
        self.assertIn('frps.log', out.getvalue())
        self.assertNotIn('frpc.log', out.getvalue())

    # ---- dashboard default listen address (external access) ----
    def test_configure_server_dashboard_defaults_to_all_interfaces(self):
        # input: 业务监听地址, 业务端口, Dashboard用户名, Dashboard监听地址,
        #        Dashboard端口, vhostHTTP(空), vhostHTTPS(空)
        # getpass: token 和 Dashboard 密码都返回 secret123
        # confirm: 对外监听=True, 启用TLS=False
        with patch('builtins.input', side_effect=['0.0.0.0','7000','admin','0.0.0.0','7500','','']), \
             patch('getpass.getpass', return_value='secret123'), \
             patch.object(m, 'verify'), patch.object(m, 'running', return_value=False), \
             patch.object(m, 'confirm', side_effect=[True, False]), \
             contextlib.redirect_stdout(io.StringIO()):
            m.configure_server()
        cfg = m.read_toml(m.ROOT / 'frps.toml')
        self.assertEqual(cfg['webServer']['addr'], '0.0.0.0')
        self.assertEqual(cfg['webServer']['port'], 7500)
    def test_config_menu_dashboard_default_is_all_interfaces(self):
        m.ROOT.mkdir(exist_ok=True)
        # pre-seed a config with webServer present
        m.atomic(m.ROOT / 'frps.toml', m.toml({'bindAddr':'0.0.0.0','bindPort':7000,
            'webServer':{'addr':'0.0.0.0','port':7500,'user':'admin','password':'pw'}}))
        # assert the menu's default string is all-interfaces (not 127.0.0.1)
        import inspect
        src = inspect.getsource(m.frps_config_menu)
        self.assertIn("web.get('addr', '0.0.0.0')", src)
        self.assertNotIn("web.get('addr', '127.0.0.1')", src)
    def test_validate_frps_accepts_external_dashboard(self):
        m.validate_frps_config({'bindPort':7000,
            'webServer':{'addr':'0.0.0.0','port':7500,'user':'admin','password':'pw'}})

    # ---- unit refresh on upgrade (template changes reach a running host) ----
    def test_unit_differs_detects_template_change(self):
        unit = m.ROOT / 'installed.service'; unit.write_text('[Service]\n# v1\n')
        template = m.ROOT / 'template.service'; template.write_text('[Service]\n# v2\n')
        with patch.object(m, '_service_manager', return_value='systemd'), \
             patch.object(m, 'installed_unit_path', return_value=unit), \
             patch.object(m, 'template_path', return_value=template):
            self.assertTrue(m.unit_differs('frps'))
            unit.write_text('[Service]\n# v2\n')
            self.assertFalse(m.unit_differs('frps'))
    def test_refresh_units_noop_when_no_manager(self):
        with patch.object(m, '_service_manager', return_value=''):
            self.assertEqual(m.refresh_units(('frps',)), [])
    def test_refresh_units_skips_when_identical(self):
        with patch.object(m, '_service_manager', return_value='systemd'), \
             patch.object(m, 'unit_differs', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.refresh_units(('frps',)), [])
    def test_refresh_units_respects_decline(self):
        unit = m.ROOT / 'unit'; unit.write_text('# old\n')
        template = m.ROOT / 'template'; template.write_text('# new\n')
        with patch.object(m, '_service_manager', return_value='systemd'), \
             patch.object(m, 'installed_unit_path', return_value=unit), \
             patch.object(m, 'template_path', return_value=template), \
             patch.object(m, 'confirm', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.refresh_units(('frps',)), [])
        self.assertEqual(unit.read_text(), '# old\n')
    def test_refresh_units_applies_and_reloads(self):
        unit = m.ROOT / 'unit'; unit.write_text('# old\n')
        template = m.ROOT / 'template'; template.write_text('# new\n')
        with patch.object(m, '_service_manager', return_value='systemd'), \
             patch.object(m, 'installed_unit_path', return_value=unit), \
             patch.object(m, 'template_path', return_value=template), \
             patch.object(m, 'confirm', return_value=True), \
             patch.object(m, 'run_cmd') as runner, \
             contextlib.redirect_stdout(io.StringIO()):
            out = m.refresh_units(('frps',))
        self.assertEqual(out, ['frps'])
        self.assertEqual(unit.read_text(), '# new\n')
        self.assertIn('daemon-reload', ' '.join(runner.call_args[0][0]))
        backups = [p for p in (m.ROOT / 'backups').iterdir() if 'unit-frps' in p.name]
        self.assertTrue(backups)
        self.assertEqual((backups[0] / 'frps.service.old').read_text(), '# old\n')

    # ---- §17.4 security / §17.2 integration (offline, mocked boundaries) ----
    def test_secret_files_have_restricted_permissions(self):
        if os.name == 'nt': self.skipTest('POSIX permissions only')
        pid = self.create()
        for name in ('profile.toml', 'secrets.toml', 'mappings.toml'):
            mode = (m.pdir(pid) / name).stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, name + ' mode ' + oct(mode))
        self.assertEqual(m.ROOT.stat().st_mode & 0o777, 0o700)
    def test_token_never_appears_in_json_status(self):
        pid = self.create(); m.set_current(pid)
        with contextlib.redirect_stdout(io.StringIO()):
            m.apply(pid)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.run(['frpc', 'status', '--json'])
        self.assertNotIn('token"\\&/', out.getvalue())
    def test_shell_metacharacters_in_name_are_rejected(self):
        for bad in ('a;rm -rf /', 'a$(id)', 'a`id`', 'a|b', 'a&b', 'a>b'):
            with self.assertRaises(ValueError): m.name(bad)
    def test_path_traversal_inputs_are_rejected(self):
        with self.assertRaises(ValueError): m.pdir('../../etc')
        with self.assertRaises(ValueError): m.pdir('/etc/passwd')
        self.create()
        directory = m.backup(m.managed_paths(), 'sec')
        m.save_json(directory / 'manifest.json', {'files': ['../../escape']})
        with self.assertRaises(ValueError): m.restore_backup(directory)
    def test_upgrade_rejects_non_https_and_off(self):
        for value in ('http://evil.example', 'off'):
            with patch.object(m, 'confirm', return_value=True), \
                 patch.dict(os.environ, {'FRP_MIRROR_URL': value}), \
                 patch.object(m, 'upgrade_snapshot', return_value=None), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError): m.upgrade('frpc')
    def test_restore_failure_does_not_corrupt_current_config(self):
        pid = self.create(); m.set_current(pid)
        with contextlib.redirect_stdout(io.StringIO()):
            m.apply(pid)
        before = (m.ROOT / 'frpc.toml').read_bytes()
        directory = m.backup([m.ROOT / 'frpc.toml'], 'broken')
        (directory / 'frpc.toml').unlink()
        with self.assertRaises(ValueError): m.restore_backup(directory)
        self.assertEqual((m.ROOT / 'frpc.toml').read_bytes(), before)

    # ---- R11/R12/M6: CLI flags, JSON envelope, error codes, audit ----
    def test_parse_flags_handles_documented_options(self):
        args, flags = m._parse_flags(['alpha', '--json', '--yes', '--lines', '200',
                                      '--version', 'v1.2.3', '--purge', '--profile', 'p1'])
        self.assertEqual(args, ['alpha'])
        for key in ('json', 'yes', 'purge', 'profile'):
            self.assertTrue(flags.get(key), key)
        self.assertEqual(flags['lines'], '200')
        self.assertEqual(flags['version'], 'v1.2.3')
    def test_status_json_envelope_has_stable_fields(self):
        pid = self.create(); m.set_current(pid)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.run(['frpc', 'status', '--json'])
        payload = json.loads(out.getvalue())
        for key in ('ok', 'code', 'component', 'service_state', 'connection_state',
                    'active_profile', 'pending', 'error', 'detail'):
            self.assertIn(key, payload)
        self.assertTrue(payload['ok']); self.assertEqual(payload['code'], 0)
        self.assertEqual(payload['active_profile']['name'], '生产服务器')
        self.assertNotIn('token', json.dumps(payload))
    def test_status_without_json_keeps_legacy_shape(self):
        pid = self.create(); m.set_current(pid)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.run(['frpc', 'status'])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['component'], 'frpc')
        self.assertIn('config_path', payload)
    def test_error_codes_are_attached_without_changing_exception_type(self):
        with self.assertRaises(ValueError) as ctx:
            m.err('E006', '端口非法')
        self.assertIn('[E006]', str(ctx.exception))
        self.assertEqual(ValueError, type(ctx.exception))
        for code in ('E001', 'E007', 'E013', 'E014', 'E017'):
            self.assertIn(code, m.ERROR_CODES)
    def test_profile_use_accepts_menu_number(self):
        a = self.create('A'); b = self.create('B'); m.set_current(a)
        with patch.object(m, 'running', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            m.run(['frpc', 'profile', 'use', '2'])
        self.assertEqual(m.active(), b)
    def test_audit_records_service_actions(self):
        pid = self.create(); m.set_current(pid)
        with patch.object(m, 'TEST', False), patch.object(m, 'run_cmd') as rc, \
             patch.object(m.shutil, 'which', return_value='/bin/systemctl'):
            rc.return_value = type('R', (), {'returncode': 0, 'stdout': 'active', 'stderr': ''})()
            m.service('frpc', 'restart')
        log = (m.ROOT / 'logs' / 'manager.log').read_text()
        self.assertIn('service-restart', log)

    # ---- FLOW F4: one bad field must not abort the wizard/session ----
    def test_edit_profile_bad_field_keeps_session(self):
        pid = self.create(); m.set_current(pid); m.apply(pid)
        with patch('builtins.input', side_effect=['4', 'abc', '2', '改名成功', '10']), \
             patch.object(m, 'confirm', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            m.edit_profile()
        self.assertEqual(m.load_profile(pid)['name'], '改名成功')
    def test_mapping_wizard_reasks_bad_remote_port(self):
        answers = ['m1', 'tcp', '127.0.0.1', '22', 'not-a-port', '6000', 'y']
        with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            result = m.mapping_wizard()
        self.assertIsNotNone(result)
        self.assertEqual(result['remote_port'], 6000)
    def test_mapping_wizard_rejects_http_without_domain_then_recovers(self):
        answers = ['m2', 'http', '127.0.0.1', '80', '', 'example.com', 'y']
        with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            result = m.mapping_wizard()
        self.assertIsNotNone(result)
        self.assertEqual(result['domain'], 'example.com')

    # ---- FLOW F6/F7: delete profile requires YES and guides afterwards ----
    def test_delete_profile_requires_yes_confirmation(self):
        pid = self.create('D'); m.set_current(pid)
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', side_effect=['y','y']):
            m.delete_profile_id(pid)
        self.assertTrue((m.ROOT / 'profiles' / pid).exists())
    def test_delete_active_profile_guides_and_clears_state(self):
        a = self.create('A'); b = self.create('B'); m.set_current(a)
        m.save_json(m.ROOT / 'state.json', {'active_profile_id': a, 'pending': True,
                                            'frpc_hash': 'x', 'frpc_log_offset': 5})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('builtins.input', return_value='YES'), \
             patch.object(m, 'running', return_value=False):
            m.delete_profile_id(a)
        self.assertFalse((m.ROOT / 'profiles' / a).exists())
        self.assertEqual(m.profiles()[0]['name'], 'B')
        self.assertIn('剩余服务端', out.getvalue())
        st = m.state()
        for key in ('pending', 'frpc_hash', 'frpc_log_offset'):
            self.assertNotIn(key, st)
    def test_delete_last_profile_guides_to_create(self):
        pid = self.create('Only'); m.set_current(pid)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('builtins.input', return_value='YES'), \
             patch.object(m, 'running', return_value=False):
            m.delete_profile_id(pid)
        self.assertEqual(m.profiles(), [])
        self.assertIn('新建 frps 服务端', out.getvalue())

    # ---- FLOW F1: switch must compensate on start failure ----
    def test_switch_start_failure_restores_old_runtime(self):
        a = self.create('A'); b = self.create('B'); m.set_current(a)
        def fake_service(role, action):
            if action == 'status': return 'active'
            if action == 'stop': return None
            if action == 'start': raise ValueError('unit masked')
            return None
        with patch.object(m, 'service', side_effect=fake_service), \
             patch.object(m, 'confirm', return_value=True), \
             patch.object(m, 'wait_connection'), contextlib.redirect_stdout(io.StringIO()):
            m.switch(b)  # must not raise; service failure is reported, not propagated
        # Target profile stays active per 详细设计 §15.2(4)
        self.assertEqual(m.active(), b)
    def test_switch_does_not_hold_lock_during_probe(self):
        import inspect
        src = inspect.getsource(m.switch)
        # The transactional block must close before start/probe happen.
        locked = src.index('with lock()')
        probe = src.index('wait_connection')
        self.assertLess(locked, probe)
        # Only a single `with lock()` in switch, and it wraps just the write.
        self.assertEqual(src.count('with lock()'), 1)

    # ---- R7/R1: token strength + frps TLS ----
    def test_wizard_rejects_weak_token(self):
        with patch('builtins.input', side_effect=['0.0.0.0','7000','']), \
             patch('getpass.getpass', return_value='abc'), \
             patch.object(m, 'verify'), patch.object(m, 'running', return_value=False), \
             patch.object(m, 'confirm', return_value=True), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError) as ctx: m.configure_server()
        self.assertIn('token', str(ctx.exception).lower())
    def test_wizard_autogenerates_strong_token_when_blank(self):
        with patch('builtins.input', side_effect=['0.0.0.0','7000','', '', '', '', '', 'n']), \
             patch('getpass.getpass', return_value=''), \
             patch.object(m, 'verify'), patch.object(m, 'running', return_value=False), \
             patch.object(m, 'confirm', return_value=True), contextlib.redirect_stdout(io.StringIO()):
            m.configure_server()
        token = m.read_toml(m.ROOT / 'frps.toml')['auth']['token']
        self.assertGreaterEqual(len(token), 8)
        m.validate_frps_config(m.read_toml(m.ROOT / 'frps.toml'))  # config must be valid
    def test_validate_frps_rejects_same_dashboard_port(self):
        with self.assertRaises(ValueError):
            m.validate_frps_config({'bindPort': 7000, 'webServer': {'port': 7000, 'user': 'a', 'password': 'b'}})
    def test_validate_frps_accepts_tls_when_files_exist(self):
        import tempfile as _tf
        cert = Path(_tf.mkdtemp()) / 'cert.pem'; cert.write_text('x')
        key = cert.parent / 'key.pem'; key.write_text('y')
        m.validate_frps_config({'bindPort': 7000, 'transport': {'tls': {'certFile': str(cert), 'keyFile': str(key)}}})
    def test_validate_frps_rejects_tls_without_key(self):
        with self.assertRaises(ValueError):
            m.validate_frps_config({'bindPort': 7000, 'transport': {'tls': {'certFile': '/nope.pem'}}})
    def test_expand_keys_builds_nested(self):
        self.assertEqual(m.expand_keys({'auth.token': 'x', 'a': 1}), {'auth': {'token': 'x'}, 'a': 1})

    # ---- STATE: status/state mapping/diagnostics robustness ----
    def test_status_bar_survives_broken_mappings_file(self):
        pid = self.create(); m.set_current(pid)
        (m.pdir(pid) / 'mappings.toml').write_text('this is not toml\n')
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', return_value='0'):
            m.main_menu('frpc')  # must not raise / exit
    def test_broken_profile_reports_config_error(self):
        m.save_json(m.ROOT / 'state.json', {'active_profile_id': 'deadbeefdeadbeefdeadbeef'})
        data = m.status('frpc')
        self.assertIsNone(data['active_profile'])
        self.assertEqual(data['connection']['state'], 'config-error')
        self.assertIn('profile_error', data)
    def test_state_labels_cover_documented_enums(self):
        for key in ('not-installed', 'stopped', 'starting', 'running', 'stopping', 'failed', 'unknown'):
            self.assertIn(key, m.SERVICE_STATES)
        for key in ('not-configured', 'connecting', 'connected', 'disconnected', 'auth-failed',
                    'unreachable', 'tls-failed', 'config-error', 'timeout', 'unknown'):
            self.assertIn(key, m.CONNECTION_STATES)
    def test_wait_connection_fails_fast_when_service_stopped(self):
        pid = self.create()
        with patch.object(m, 'connection', return_value={'state': 'stopped'}), \
             patch.object(m, 'running', return_value=False), \
             patch.object(m.time, 'monotonic', side_effect=[0, 1]), \
             patch.object(m.time, 'sleep') as sleeper, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(m.wait_connection(pid, timeout=30))
            sleeper.assert_not_called()  # no pointless 30s wait
    def test_toml_errors_do_not_echo_values(self):
        with self.assertRaises(ValueError) as ctx:
            m._mini_toml('auth.token = "SUPERSECRET')
        self.assertNotIn('SUPERSECRET', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx2:
            m._mini_toml('a = @BADVALUE@')
        self.assertNotIn('@BADVALUE@', str(ctx2.exception))

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

    # ---- upgrade must discover the latest version from the mirror ----
    def test_latest_manager_version_parses_bootstrap(self):
        bootstrap = "#!/bin/bash\nFRP_MANAGER_VERSION='v9.9.9-test.1'\necho hi\n"
        with patch.object(m, '_https_get', return_value=bootstrap.encode()):
            self.assertEqual(m._latest_manager_version('https://x.example'), 'v9.9.9-test.1')
    def test_latest_manager_version_rejects_malformed(self):
        with patch.object(m, '_https_get', return_value=b'no version here'):
            with self.assertRaises(ValueError):
                m._latest_manager_version('https://x.example')
    def test_upgrade_same_version_short_circuits(self):
        out = io.StringIO()
        with patch.object(m, 'installed_manager_version', return_value='v1.2.3'), \
             patch.object(m, 'installed_frp_version', return_value='0.72.0'), \
             patch.object(m, '_latest_manager_version', return_value='v1.2.3'), \
             contextlib.redirect_stdout(out):
            m.upgrade('frps')
        self.assertIn('已是最新版本', out.getvalue())

    # ---- bug fixes: flags, error codes, firewall, log, upgrade rollback ----
    def test_no_start_flag_parses_to_no_start_key(self):
        args, flags = m._parse_flags(['--no-start', '--yes'])
        self.assertTrue(flags.get('no_start'))
        self.assertTrue(flags.get('yes'))
    def test_mapping_delete_requires_yes(self):
        pid = self.create(); m.set_current(pid)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(m, 'apply'):
            m.save_mappings(pid, [self.mapping()])
        # CLI delete with plain 'y' must be rejected (danger confirm requires YES)
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', side_effect=['y']):
            m.mapping_command(['delete', 'ssh'])
        self.assertEqual(len(m.mappings(pid)), 1)  # unchanged
    def test_err_rejects_unknown_code(self):
        with self.assertRaises(ValueError) as ctx:
            m.err('E999', 'boom')
        self.assertIn('未知错误码', str(ctx.exception))
    def test_port_error_has_code(self):
        with self.assertRaises(ValueError) as ctx:
            m.port('abc')
        self.assertIn('[E006]', str(ctx.exception))
    def test_firewall_hint_port_not_substring_match(self):
        # "80" must not match "8080" as a rule
        fake = 'Status: active\nTo                         Action      From\n8080/tcp                  ALLOW       Anywhere\n'
        with patch.object(m.shutil, 'which', return_value='/usr/sbin/ufw'), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R', (), {'stdout': fake, 'stderr': ''})()
            hints = m.firewall_hint(80, 'tcp')
        # 80 is NOT explicitly allowed (only 8080), so a hint should appear
        self.assertTrue(any('80/tcp' in h for h in hints))
    def test_firewall_hint_exact_port_allowed(self):
        fake = 'Status: active\nTo                         Action      From\n80/tcp                    ALLOW       Anywhere\n'
        with patch.object(m.shutil, 'which', return_value='/usr/sbin/ufw'), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R', (), {'stdout': fake, 'stderr': ''})()
            hints = m.firewall_hint(80, 'tcp')
        self.assertEqual(hints, [])  # explicitly allowed, no hint
    def test_login_state_no_false_auth_from_token_word(self):
        log = m.ROOT / 'logs' / 'frpc.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('start frpc service\nuse token: xyz\nconnecting to server...\n')
        self.assertIsNone(m._login_state_from_log())
    def test_restore_upgrade_snapshot_dispatches(self):
        # Simulate an upgrade snapshot dir and verify restore dispatches correctly.
        from pathlib import Path as _P
        snap = m.ROOT / 'backups' / '20260101-000000-upgrade-abc'
        (snap / 'install-root' / 'lib').mkdir(parents=True)
        (snap / 'install-root' / 'lib' / 'manager.py').write_text('# old\n')
        m.save_json(snap / 'manifest.json', {'reason': 'upgrade',
                                             'files': ['lib/manager.py'],
                                             'sha256': {}})
        # patch INSTALL_ROOT to a temp dir so we don't touch the real one
        import tempfile as _tf
        fake_install = _P(_tf.mkdtemp())
        with patch.object(m, 'INSTALL_ROOT', fake_install):
            restored = m.restore_upgrade_snapshot(snap)
        self.assertEqual(restored, ['lib/manager.py'])
        self.assertEqual((fake_install / 'lib' / 'manager.py').read_text(), '# old\n')

    # ---- launchd enable/disable uses persistent boot policy (macOS) ----
    def test_launchd_disable_uses_disable_not_unload(self):
        def fake_which(cmd):
            return '/bin/launchctl' if cmd == 'launchctl' else None
        # setUp patches TEST=True which short-circuits service(); override here.
        with patch.object(m, 'TEST', False), \
             patch.object(m.shutil, 'which', side_effect=fake_which), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R',(),{'returncode':0,'stdout':'','stderr':''})()
            m.service('frpc', 'disable')
        call = rc.call_args[0][0]
        # disable must be launchctl disable system/<label>, NOT unload (unload
        # would not survive a reboot because RunAtLoad re-loads it).
        self.assertEqual(call[:2], ['launchctl', 'disable'])
        self.assertTrue(call[2].startswith('system/frp-simple-bash.frpc'))
    def test_launchd_enable_uses_enable(self):
        def fake_which(cmd):
            return '/bin/launchctl' if cmd == 'launchctl' else None
        with patch.object(m, 'TEST', False), \
             patch.object(m.shutil, 'which', side_effect=fake_which), \
             patch.object(m, 'run_cmd') as rc:
            rc.return_value = type('R',(),{'returncode':0,'stdout':'','stderr':''})()
            m.service('frpc', 'enable')
        self.assertEqual(rc.call_args[0][0][:2], ['launchctl', 'enable'])
    def test_install_script_launchd_uses_enable_disable(self):
        from pathlib import Path as _P
        repo = _P(__file__).resolve().parent.parent
        text = (repo / 'install-common.sh').read_text()
        self.assertIn('launchctl enable "system/$plist_label"', text)
        self.assertIn('launchctl disable "system/$plist_label"', text)

if __name__ == '__main__': unittest.main()
