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
        self.assertEqual(shown.count('frps 服务端管理'), 3)
        self.assertEqual(shown.count('运行状态：'), 3)
        self.assertIn('[提示] 正在刷新服务信息。', shown)
        self.assertIn('业务监听：0.0.0.0:7000',shown)
        self.assertIn('Dashboard：http://127.0.0.1:7500',shown)
        self.assertIn('1. 查看服务信息（状态、版本、路径）', shown)
        self.assertNotIn('2. 查看版本信息', shown)
        self.assertNotIn('3. 查看安装路径', shown)
        for hidden in ('"component"','do-not-print-token','do-not-print-password'): self.assertNotIn(hidden,shown)
    def test_frpc_menu_labels_and_json_status(self):
        pid=self.create();m.set_current(pid)
        output=io.StringIO()
        with contextlib.redirect_stdout(output), patch('builtins.input', side_effect=['1','0']): m.main_menu('frpc')
        shown=output.getvalue();self.assertEqual(shown.count('frpc 客户端管理'),2)
        self.assertIn('生产服务器（[::1]:7000）',shown)
        self.assertIn('连接状态：已停止',shown);self.assertNotIn('"component"',shown)
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
        external = m.render(pid).replace('log.maxDays = 30', 'log.maxDays = 7')
        m.atomic(m.ROOT / 'frpc.toml', external)
        with patch('builtins.input', return_value='1'):
            with self.assertRaises(ValueError): m.import_external('frpc')
        self.assertEqual((m.ROOT / 'frpc.toml').read_text(), external)
    def test_connection_timeout_is_bounded(self):
        pid = self.create()
        with patch.object(m, 'connection', return_value={'state':'connecting'}), patch.object(m.time, 'monotonic', side_effect=[0, 30]), patch.object(m.time, 'sleep') as sleeper:
            self.assertFalse(m.wait_connection(pid))
            sleeper.assert_not_called()
    def test_service_process_alone_is_not_online(self):
        pid = self.create()
        with patch.object(m, 'running', return_value=True), patch.object(m, 'api', return_value={}):
            self.assertEqual(m.connection(pid)['state'], 'unknown')
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

if __name__ == '__main__': unittest.main()
