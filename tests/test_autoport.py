"""端口自动开通（auto-port）单元测试。

覆盖：配置默认值与往返、白名单/黑名单/配额过滤、端口冲突、diff 同步、
回收策略、一键撤销、防火墙命令构造（必须参数化、不得拼接 shell）、审计。
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('manager_autoport', str(ROOT / 'lib' / 'manager.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class AutoPortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            patch.object(m, 'ROOT', self.tmp),
            patch.object(m, 'BIN', self.tmp / 'bin'),
            patch.object(m, 'INSTALL_ROOT', self.tmp),
            patch.object(m, 'TEST', True),
            patch.object(m, 'UI_COLOR', False),
        ]
        for item in self.patches:
            item.start()
        m.initialize()
        m.atomic(self.tmp / 'frps.toml', m.toml({
            'bindPort': 7000, 'auth.token': 'abcdefgh',
            'webServer.addr': '127.0.0.1', 'webServer.port': 7500,
            'webServer.user': 'admin', 'webServer.password': 'pw',
        }))

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, **overrides):
        cfg = m.auto_port_config()
        cfg.update(overrides)
        return cfg

    def proxy(self, port, client='clientA', phase='online', ptype='tcp', name='p'):
        return {'name': name, 'clientID': client,
                'spec': {'type': ptype, 'remotePort': port},
                'status': {'phase': phase}}

    # ---- 配置 ----

    def test_default_config(self):
        cfg = m.auto_port_config()
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['max_ports_per_client'], 20)
        self.assertTrue(cfg['deny_ports_enabled'])
        self.assertEqual(cfg['prune_after_seconds'], 120)
        for port in (22, 23, 3306, 6379, 9200, 27017):
            self.assertIn(port, cfg['deny_ports'])

    def test_config_roundtrip_and_permissions(self):
        cfg = m.auto_port_config()
        cfg['allow_clients'] = ['abc', 'def']
        cfg['max_ports_per_client'] = 0
        cfg['deny_ports'] = [22, 80]
        m.save_auto_port_config(cfg)
        back = m.auto_port_config()
        self.assertEqual(back['allow_clients'], ['abc', 'def'])
        self.assertEqual(back['max_ports_per_client'], 0)   # 0 = 不限
        self.assertEqual(back['deny_ports'], [22, 80])
        self.assertEqual((self.tmp / 'auto-port.toml').stat().st_mode & 0o777, 0o600)

    # ---- 过滤 ----

    def test_desired_filters_offline_denied_and_unknown_client(self):
        cfg = self.cfg(allow_clients=['clientA'])
        rows = [self.proxy(6000), self.proxy(6001, phase='offline'),
                self.proxy(3306), self.proxy(6002, client='evil'),
                self.proxy(6003, ptype='http')]
        with patch.object(m, 'frps_online_proxies', return_value=rows), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1):
            desired, skipped = m.ports_desired(cfg)
        self.assertEqual(sorted(desired), [(6000, 'tcp')])
        reasons = sorted({s['reason'] for s in skipped})
        self.assertEqual(reasons, ['client_not_allowed', 'denied_port'])

    def test_desired_respects_quota_and_zero_means_unlimited(self):
        cfg = self.cfg(allow_clients=['clientA'], max_ports_per_client=2,
                       deny_ports_enabled=False)
        rows = [self.proxy(6000 + i) for i in range(5)]
        with patch.object(m, 'frps_online_proxies', return_value=rows), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1):
            desired, skipped = m.ports_desired(cfg)
        self.assertEqual(len(desired), 2)
        self.assertTrue(any(s['reason'] == 'quota_exceeded' for s in skipped))

        cfg['max_ports_per_client'] = 0
        with patch.object(m, 'frps_online_proxies', return_value=rows), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1):
            desired, _ = m.ports_desired(cfg)
        self.assertEqual(len(desired), 5)

    def test_desired_skips_port_held_by_other_process(self):
        cfg = self.cfg(allow_clients=['clientA'])
        with patch.object(m, 'frps_online_proxies', return_value=[self.proxy(6000)]), \
             patch.object(m, 'port_conflict', return_value=True), \
             patch.object(m, '_service_pid', return_value=1):
            desired, skipped = m.ports_desired(cfg)
        self.assertEqual(desired, {})
        self.assertEqual(skipped[0]['reason'], 'port_in_use')

    def test_port_conflict_ignores_frps_itself(self):
        with patch.object(m, 'port_listener_pids', return_value=[42, 42]):
            self.assertFalse(m.port_conflict(6000, 'tcp', frps_pid=42))
            self.assertTrue(m.port_conflict(6000, 'tcp', frps_pid=1))
        with patch.object(m, 'port_listener_pids', return_value=[]):
            self.assertFalse(m.port_conflict(6000, 'tcp', frps_pid=None))

    # ---- 同步 ----

    def test_sync_adds_and_tracks_state(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'])
        m.save_auto_port_config(cfg)
        with patch.object(m, 'frps_online_proxies', return_value=[self.proxy(6000)]), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1), \
             patch.object(m, 'firewall_add_port') as add, \
             patch.object(m, 'firewall_del_port'):
            report = m.ports_sync(apply_changes=True)
        add.assert_called_once_with(6000, 'tcp')
        self.assertEqual(report['added'], [{'port': 6000, 'proto': 'tcp'}])
        self.assertEqual([(r['port'], r['proto']) for r in m.auto_port_state()['opened']],
                         [(6000, 'tcp')])

    def test_sync_prunes_only_after_ttl(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'], prune_after_seconds=3600)
        m.save_auto_port_config(cfg)
        m.save_auto_port_state({'schema_version': 1, 'backend': 'ufw', 'opened': [
            {'port': 6000, 'proto': 'tcp', 'client_id': 'clientA', 'proxy': 'old'}]})
        with patch.object(m, 'frps_online_proxies', return_value=[]), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1), \
             patch.object(m, 'firewall_add_port'), \
             patch.object(m, 'firewall_del_port') as delete:
            report = m.ports_sync(apply_changes=True)
        delete.assert_not_called()                       # 未到 TTL，先只打标记
        self.assertEqual(report['removed'], [])
        self.assertIn('missing_since', m.auto_port_state()['opened'][0])

    def test_sync_prune_after_ttl_removes_rule(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'], prune_after_seconds=10)
        m.save_auto_port_config(cfg)
        m.save_auto_port_state({'schema_version': 1, 'backend': 'ufw', 'opened': [
            {'port': 6000, 'proto': 'tcp', 'client_id': 'clientA', 'proxy': 'old',
             'missing_since': '2000-01-01T00:00:00'}]})
        with patch.object(m, 'frps_online_proxies', return_value=[]), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1), \
             patch.object(m, 'firewall_add_port'), \
             patch.object(m, 'firewall_del_port') as delete:
            report = m.ports_sync(apply_changes=True)
        delete.assert_called_once_with(6000, 'tcp')
        self.assertEqual(report['removed'], [{'port': 6000, 'proto': 'tcp'}])
        self.assertEqual(m.auto_port_state()['opened'], [])

    def test_sync_reports_but_does_not_apply_when_disabled(self):
        with patch.object(m, 'firewall_add_port') as add:
            report = m.ports_sync(apply_changes=True)
        self.assertFalse(report['enabled'])
        add.assert_not_called()

    def test_sync_refuses_when_firewall_inactive(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'])
        m.save_auto_port_config(cfg)
        detected = {'backend': 'ufw', 'active': False, 'available': ['ufw'],
                    'family': 'debian', 'preferred': 'ufw', 'detail': {},
                    'panels': [], 'conflict': False}
        with patch.object(m, 'firewall_detect', return_value=detected):
            with self.assertRaises(ValueError):
                m.ports_sync(apply_changes=True)

    def test_plan_mode_does_not_change_state(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'])
        m.save_auto_port_config(cfg)
        with patch.object(m, 'frps_online_proxies', return_value=[self.proxy(6000)]), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1), \
             patch.object(m, 'firewall_add_port') as add:
            report = m.ports_sync(apply_changes=False)
        add.assert_not_called()
        self.assertEqual(report['added'], [{'port': 6000, 'proto': 'tcp'}])
        self.assertEqual(m.auto_port_state()['opened'], [])

    # ---- 撤销 / 启用 ----

    def test_revert_disables_and_clears_state(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'])
        m.save_auto_port_config(cfg)
        m.save_auto_port_state({'schema_version': 1, 'backend': 'ufw', 'opened': [
            {'port': 6000, 'proto': 'tcp'}, {'port': 6001, 'proto': 'udp'}]})
        with patch.object(m, 'firewall_del_port') as delete, \
             patch.object(m, 'ports_timer_remove'):
            removed = m.ports_revert()
        self.assertEqual(len(removed), 2)
        self.assertEqual(delete.call_count, 2)
        self.assertFalse(m.auto_port_config()['enabled'])
        self.assertEqual(m.auto_port_state()['opened'], [])

    def test_enable_requires_whitelist(self):
        with patch.object(m, 'firewall_backend', return_value=('ufw', True)), \
             patch.object(m, 'frps_dashboard_config', return_value=('http', '127.0.0.1', {})):
            with self.assertRaises(ValueError) as ctx:
                m.ports_enable(self.cfg(allow_clients=[]))
        self.assertIn('白名单', str(ctx.exception))

    def test_enable_requires_dashboard(self):
        with patch.object(m, 'firewall_backend', return_value=('ufw', True)), \
             patch.object(m, 'frps_dashboard_config', side_effect=ValueError('Dashboard 未启用')):
            with self.assertRaises(ValueError) as ctx:
                m.ports_enable(self.cfg(allow_clients=['clientA']))
        self.assertIn('Dashboard', str(ctx.exception))

    def test_enable_installs_timer(self):
        cfg = self.cfg(allow_clients=['clientA'])
        with patch.object(m, 'firewall_backend', return_value=('ufw', True)), \
             patch.object(m, 'frps_dashboard_config', return_value=('http', '127.0.0.1', {})), \
             patch.object(m, 'ports_timer_install', return_value=True) as timer:
            self.assertTrue(m.ports_enable(cfg))
        timer.assert_called_once()

    # ---- 防火墙命令构造 ----

    def _capture(self, backend, fn, *args):
        calls = []
        def fake(args_list, **kwargs):
            calls.append(list(args_list))
            return type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        detected = {'backend': backend, 'active': True, 'available': [backend],
                    'family': 'debian', 'preferred': 'ufw', 'detail': {}}
        with patch.object(m, 'firewall_detect', return_value=detected), \
             patch.object(m, 'run_cmd', side_effect=fake):
            fn(*args)
        return calls

    def test_ufw_commands_are_argument_lists(self):
        calls = self._capture('ufw', m.firewall_add_port, 6000, 'tcp')
        self.assertEqual(calls[0], ['ufw', 'allow', '6000/tcp'])
        for call in calls:
            self.assertIsInstance(call, list)
            self.assertNotIn(';', ' '.join(call))
            self.assertNotIn('&&', ' '.join(call))

    def test_firewalld_commands(self):
        calls = self._capture('firewalld', m.firewall_add_port, 6001, 'udp')
        self.assertIn(['firewall-cmd', '--permanent', '--add-port=6001/udp'], calls)
        self.assertIn(['firewall-cmd', '--reload'], calls)

    def test_iptables_commands_carry_comment_marker(self):
        calls = self._capture('iptables', m.firewall_add_port, 6002, 'tcp')
        flat = ' '.join(calls[0])
        self.assertIn('-I INPUT', flat)
        self.assertIn('--dport 6002', flat)
        self.assertIn('frp-simple-bash:auto', flat)

    def test_add_refuses_inactive_firewall(self):
        detected = {'backend': 'ufw', 'active': False, 'available': ['ufw'],
                    'family': 'debian', 'preferred': 'ufw', 'detail': {}}
        with patch.object(m, 'firewall_detect', return_value=detected):
            with self.assertRaises(ValueError):
                m.firewall_add_port(6000, 'tcp')

    # ---- 审计 ----

    def test_audit_records_without_secrets(self):
        m.ports_audit('port-open', port=6000, proto='tcp', client_id='abc',
                      proxy='ssh', result='ok')
        text = m.auto_port_audit_path().read_text()
        self.assertIn('"action": "port-open"', text)
        self.assertIn('"port": 6000', text)
        self.assertNotIn('token', text.lower())
        self.assertNotIn('password', text.lower())

    # ---- 定时器在非 systemd 平台为 no-op ----

    def test_timer_is_noop_without_systemd(self):
        with patch.object(m, '_systemd_available', return_value=False):
            self.assertFalse(m.ports_timer_install(self.cfg()))
            self.assertFalse(m.ports_timer_remove())
            self.assertIn('不适用', m.ports_timer_status())


    # ---- 防火墙兼容层（多发行版） ----

    def _detect_with(self, handlers, available=('ufw', 'firewall-cmd', 'nft', 'iptables'),
                     osr=None):
        """Run firewall_detect with TEST disabled and stubbed commands."""
        os_release = osr if osr is not None else {'ID': 'debian', 'ID_LIKE': 'debian'}
        def fake_run(args_list, **kwargs):
            key = ' '.join(args_list)
            return type('R', (), {'returncode': 0, 'stdout': handlers.get(key, ''),
                                  'stderr': ''})()
        def fake_which(name):
            return '/usr/sbin/' + name if name in available else None
        with patch.object(m, 'TEST', False), \
             patch.object(m, 'os_release', return_value=os_release), \
             patch.object(m, 'run_cmd', side_effect=fake_run), \
             patch.object(m.shutil, 'which', side_effect=fake_which):
            return m.firewall_detect()

    def test_os_family_mapping(self):
        cases = [({'ID': 'ubuntu'}, 'debian'), ({'ID': 'debian'}, 'debian'),
                 ({'ID': 'centos'}, 'rhel'), ({'ID': 'rocky', 'ID_LIKE': 'rhel'}, 'rhel'),
                 ({'ID': 'opensuse-leap', 'ID_LIKE': 'suse'}, 'suse'),
                 ({'ID': 'alpine'}, 'alpine'), ({'ID': 'arch'}, 'arch'),
                 ({'ID': 'weird'}, 'other')]
        for release, expected in cases:
            with patch.object(m, 'os_release', return_value=release):
                self.assertEqual(m.os_family(), expected, release)

    def test_preferred_firewall_per_family(self):
        for release, expected in (({'ID': 'ubuntu'}, 'ufw'),
                                  ({'ID': 'centos'}, 'firewalld'),
                                  ({'ID': 'alpine'}, 'nftables')):
            with patch.object(m, 'os_release', return_value=release):
                self.assertEqual(m.preferred_firewall(), expected, release)

    def test_detect_prefers_active_ufw_over_inactive_firewalld(self):
        info = self._detect_with({'ufw status': 'Status: active\n',
                                  'firewall-cmd --state': 'not running\n'})
        self.assertEqual(info['backend'], 'ufw')
        self.assertTrue(info['active'])
        self.assertIn('firewalld', info['available'])

    def test_detect_falls_back_to_running_firewalld(self):
        info = self._detect_with({'ufw status': 'Status: inactive\n',
                                  'firewall-cmd --state': 'running\n'})
        self.assertEqual(info['backend'], 'firewalld')
        self.assertTrue(info['active'])

    def test_detect_falls_back_to_iptables_when_managers_inactive(self):
        info = self._detect_with({'ufw status': 'Status: inactive\n',
                                  'firewall-cmd --state': 'not running\n',
                                  'iptables -S': '-P INPUT DROP\n-A INPUT -p tcp --dport 22 -j ACCEPT\n'})
        self.assertEqual(info['backend'], 'iptables')
        self.assertTrue(info['active'])

    def test_detect_reports_none_active_when_all_installed_but_off(self):
        info = self._detect_with({'ufw status': 'Status: inactive\n',
                                  'firewall-cmd --state': 'not running\n',
                                  'iptables -S': '-P INPUT ACCEPT\n',
                                  'nft list ruleset': 'table inet filter {}\n'})
        self.assertFalse(info['active'])
        self.assertEqual(info['backend'], '')
        self.assertEqual(sorted(info['available']), ['firewalld', 'iptables', 'nftables', 'ufw'])

    def test_detect_nftables_when_policy_drop(self):
        info = self._detect_with({'iptables -S': '',
                                  'nft list ruleset': 'chain input { type filter hook input priority 0; policy drop; }\n'},
                                 available=('nft',))
        self.assertEqual(info['backend'], 'nftables')
        self.assertTrue(info['active'])

    def test_ssh_ports_strict_refuses_to_guess(self):
        with patch.object(m, '_cmd_out', return_value=''), \
             patch.object(m.Path, 'read_text', side_effect=OSError):
            with self.assertRaises(ValueError):
                m.ssh_listen_ports(strict=True)
            self.assertEqual(m.ssh_listen_ports(strict=False), [22])

    def test_ssh_ports_from_listening_sockets(self):
        ss_out = ('LISTEN 0 128 0.0.0.0:2222 0.0.0.0:* users:(("sshd",pid=900,fd=3))\n'
                  'LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=900,fd=4))\n')
        with patch.object(m, '_cmd_out', return_value=ss_out):
            self.assertEqual(m.ssh_listen_ports(strict=True), [22, 2222])

    def test_frps_keep_ports_includes_bind_and_vhost(self):
        m.atomic(self.tmp / 'frps.toml', m.toml({
            'bindPort': 7000, 'vhostHTTPPort': 8080, 'kcpBindPort': 7001,
            'webServer.addr': '0.0.0.0', 'webServer.port': 7500}))
        ports = m.frps_keep_ports()
        self.assertIn((7000, 'tcp'), ports)
        self.assertIn((8080, 'tcp'), ports)
        self.assertIn((7001, 'tcp'), ports)
        self.assertIn((7500, 'tcp'), ports)   # Dashboard 对外监听才需要放行

    def test_enable_plan_puts_ssh_before_frps_and_enable(self):
        with patch.object(m, 'os_release', return_value={'ID': 'debian'}), \
             patch.object(m, 'firewall_detect', return_value={
                 'backend': '', 'active': False, 'available': [], 'family': 'debian',
                 'preferred': 'ufw', 'detail': {}}), \
             patch.object(m, 'ssh_listen_ports', return_value=[22]), \
             patch.object(m, 'frps_keep_ports', return_value=[(7000, 'tcp')]):
            plan = m.firewall_enable_plan()
        self.assertEqual(plan['target'], 'ufw')
        self.assertEqual(plan['install_cmds'], [['apt-get', 'install', '-y', 'ufw']])
        flat = [' '.join(c) for c in plan['allow_cmds']]
        self.assertTrue(any('22/tcp' in c for c in flat))
        self.assertTrue(any('7000/tcp' in c for c in flat))
        self.assertIn('22', plan['notes'][0])
        self.assertEqual(plan['enable_cmds'], [['ufw', '--force', 'enable']])

    def test_enable_aborts_when_ssh_allow_fails(self):
        plan = {'target': 'ufw', 'family': 'debian', 'installed': True,
                'ssh_ports': [22], 'keep': [], 'install_cmds': [],
                'allow_cmds': [['ufw', 'allow', '22/tcp']],
                'enable_cmds': [['ufw', '--force', 'enable']], 'notes': []}
        def fake_run(args_list, **kwargs):
            code = 1 if args_list[:2] == ['ufw', 'allow'] else 0
            return type('R', (), {'returncode': code, 'stdout': '', 'stderr': 'denied'})()
        with patch.object(m, 'firewall_enable_plan', return_value=plan), \
             patch.object(m, 'run_cmd', side_effect=fake_run):
            with self.assertRaises(ValueError) as ctx:
                m.firewall_enable(yes=True)
        self.assertIn('SSH', str(ctx.exception))

    def test_windows_backend_uses_netsh(self):
        calls = self._capture('windows', m.firewall_add_port, 6000, 'tcp')
        flat = ' '.join(calls[0])
        self.assertTrue(calls[0][0] == 'netsh')
        self.assertIn('advfirewall', flat)
        self.assertIn('localport=6000', flat)
        self.assertIn('action=allow', flat)

    def test_nftables_add_refuses_unknown_table(self):
        detected = {'backend': 'nftables', 'active': True, 'available': ['nftables'],
                    'family': 'alpine', 'preferred': 'nftables', 'detail': {}}
        with patch.object(m, 'firewall_detect', return_value=detected), \
             patch.object(m, '_cmd_ok', return_value=False):
            with self.assertRaises(ValueError) as ctx:
                m.firewall_add_port(6000, 'tcp')
        self.assertIn('nftables', str(ctx.exception))

    def test_hint_text_guides_installed_but_inactive(self):
        text = m._firewall_hint_text({'family': 'debian', 'preferred': 'ufw',
                                      'available': ['ufw'], 'backend': ''})
        self.assertIn('ports firewall enable', text)
        text2 = m._firewall_hint_text({'family': 'alpine', 'preferred': 'nftables',
                                       'available': [], 'backend': ''})
        self.assertIn('alpine', text2)


    # ---- 菜单（所有命令都要能用数字执行） ----

    def test_menu_exposes_every_command(self):
        out = io.StringIO()
        with patch('builtins.input', side_effect=['0']), \
             contextlib.redirect_stdout(out):
            m.auto_port_menu()
        shown = out.getvalue()
        expected = ['查看完整状态', '预览变更计划（不执行）', '立即同步一次',
                    '查看已放行规则', '启用自动放行', '关闭自动放行',
                    '查看可发现的客户端（clientID）', '添加客户端到白名单',
                    '从白名单移除客户端', '检测防火墙状态',
                    '预览防火墙启用计划', '一键启用防火墙（先放行 SSH）',
                    '撤销全部自动放行并关闭']
        for label in expected:
            self.assertIn(label, shown, label)
        # 编号必须连续 1..13
        for index in range(1, 14):
            self.assertIn('%d)' % index, shown.replace(' 1)', ' 1)'))

    def test_menu_status_block_under_title(self):
        out = io.StringIO()
        with patch('builtins.input', side_effect=['0']), \
             contextlib.redirect_stdout(out):
            m.auto_port_menu()
        shown = out.getvalue()
        self.assertLess(shown.index('端口自动开通'), shown.index('功能状态'))
        self.assertLess(shown.index('功能状态'), shown.index('状态与预览'))


    # ---- 面板接管（宝塔等）兼容 ----

    def test_panel_detect_finds_baota(self):
        with patch.object(m, 'TEST', False), \
             patch.object(m.os, 'name', 'posix'), \
             patch.object(m.shutil, 'which', return_value=None), \
             patch.object(m.Path, 'exists', return_value=True):
            panels = m.panel_detect()
        self.assertTrue(any(p['key'] == 'baota' for p in panels))

    def test_panel_detect_clean_machine(self):
        with patch.object(m, 'TEST', False), \
             patch.object(m.os, 'name', 'posix'), \
             patch.object(m.shutil, 'which', return_value=None), \
             patch.object(m.Path, 'exists', return_value=False):
            self.assertEqual(m.panel_detect(), [])

    def test_firewall_mode_defaults_to_readonly_with_panel(self):
        info = {'panels': [{'key': 'baota', 'name': '宝塔面板 / aaPanel'}],
                'detail': {'iptables': 'active'}, 'backend': 'iptables', 'active': True,
                'managers': ['iptables'], 'conflict': True}
        self.assertEqual(m.firewall_mode(info), 'readonly')

    def test_firewall_mode_defaults_to_direct_without_panel(self):
        info = {'panels': [], 'detail': {'ufw': 'active'}, 'backend': 'ufw',
                'active': True, 'managers': ['ufw'], 'conflict': False}
        self.assertEqual(m.firewall_mode(info), 'direct')

    def test_firewall_mode_override(self):
        cfg = self.cfg(panel_mode='direct')
        m.save_auto_port_config(cfg)
        info = {'panels': [{'key': 'baota', 'name': '宝塔'}], 'detail': {},
                'backend': '', 'active': False}
        self.assertEqual(m.firewall_mode(info), 'direct')
        cfg['panel_mode'] = 'readonly'
        m.save_auto_port_config(cfg)
        self.assertEqual(m.firewall_mode(info), 'readonly')

    def test_sync_readonly_mode_never_touches_firewall(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'], panel_mode='readonly')
        m.save_auto_port_config(cfg)
        detected = {'backend': 'iptables', 'active': True, 'available': ['iptables'],
                    'family': 'debian', 'preferred': 'ufw', 'detail': {'iptables': 'active'},
                    'panels': [{'key': 'baota', 'name': '宝塔面板 / aaPanel'}],
                    'managers': ['iptables'], 'conflict': True}
        with patch.object(m, 'firewall_detect', return_value=detected), \
             patch.object(m, 'frps_online_proxies', return_value=[self.proxy(6000)]), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1), \
             patch.object(m, 'firewall_open_ports', return_value=set()), \
             patch.object(m, 'firewall_add_port') as add, \
             patch.object(m, 'firewall_del_port') as delete:
            report = m.ports_sync(apply_changes=True)
        add.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(report['mode'], 'readonly')
        self.assertEqual([x['port'] for x in report['manual']], [6000])
        self.assertTrue(report['conflict'])
        self.assertEqual(m.auto_port_state()['opened'], [])   # 不改状态

    def test_sync_detects_rules_cleared_by_panel(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'], panel_mode='direct')
        m.save_auto_port_config(cfg)
        m.save_auto_port_state({'schema_version': 1, 'backend': 'iptables', 'opened': [
            {'port': 6000, 'proto': 'tcp', 'client_id': 'clientA', 'proxy': 'ssh'}]})
        detected = {'backend': 'iptables', 'active': True, 'available': ['iptables'],
                    'family': 'debian', 'preferred': 'ufw', 'detail': {'iptables': 'active'},
                    'panels': [], 'managers': ['iptables'], 'conflict': False}
        with patch.object(m, 'firewall_detect', return_value=detected), \
             patch.object(m, 'frps_online_proxies', return_value=[self.proxy(6000)]), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1), \
             patch.object(m, 'firewall_open_ports', return_value=set()), \
             patch.object(m, 'firewall_add_port'):
            report = m.ports_sync(apply_changes=True)
        self.assertEqual([(x['port'], x['proto']) for x in report['lost']], [(6000, 'tcp')])
        self.assertIn('rule-lost', m.auto_port_audit_path().read_text())

    def test_report_mentions_readonly_and_conflict(self):
        report = {'mode': 'readonly', 'backend': 'iptables', 'panels': ['宝塔面板'],
                  'conflict': True, 'manual': [{'port': 6000, 'proto': 'tcp', 'proxy': 'ssh'}],
                  'lost': [{'port': 6001, 'proto': 'tcp'}], 'added': [], 'removed': [],
                  'kept': [], 'skipped': []}
        text = '\n'.join(m.auto_port_report_lines(report))
        self.assertIn('只读', text)
        self.assertIn('宝塔面板', text)
        self.assertIn('冲突', text)
        self.assertIn('待手动放行：6000/tcp', text)
        self.assertIn('规则已丢失：6001/tcp', text)


    # ---- 宝塔 API ----

    def test_panel_config_defaults_and_roundtrip(self):
        panel = m.panel_config()
        self.assertEqual(panel['type'], 'baota')
        self.assertFalse(panel['api_key'])
        panel['url'] = 'http://127.0.0.1:8888'
        panel['api_key'] = 'secret-key'
        m.save_panel_config(panel)
        back = m.panel_config()
        self.assertEqual(back['url'], 'http://127.0.0.1:8888')
        self.assertEqual(back['api_key'], 'secret-key')
        # 不能破坏其它配置段
        cfg = m.auto_port_config()
        self.assertEqual(cfg['max_ports_per_client'], 20)
        self.assertTrue(cfg['deny_ports'])
        self.assertEqual((self.tmp / 'auto-port.toml').stat().st_mode & 0o777, 0o600)

    def test_panel_api_ready(self):
        panel = m.panel_config()
        self.assertFalse(m.panel_api_ready(panel))
        panel['api_key'] = 'k'
        self.assertTrue(m.panel_api_ready(panel))

    def test_panel_signature_format(self):
        stamp, token = m._panel_sign('abc')
        self.assertTrue(stamp.isdigit())
        expected = hashlib.md5((stamp + hashlib.md5(b'abc').hexdigest()).encode()).hexdigest()
        self.assertEqual(token, expected)

    def test_panel_request_builds_signed_post(self):
        captured = {}
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"status": true, "data": []}'
        class FakeOpener:
            def open(self, request, timeout=None):
                captured['url'] = request.full_url
                captured['data'] = request.data.decode()
                return FakeResponse()
        panel = {'type': 'baota', 'url': 'http://127.0.0.1:8888', 'api_key': 'k',
                 'verify_tls': False, 'timeout': 5, 'node': '', 'mode': 'auto',
                 'list_action': 'GetRules', 'add_action': 'AddRule', 'del_action': 'DelRule'}
        with patch.object(m.urllib.request, 'build_opener', return_value=FakeOpener()):
            payload = m.panel_request('AddRule', {'port': 6000, 'types': 'tcp'}, panel)
        self.assertIn('/firewall?action=AddRule', captured['url'])
        for key in ('request_time=', 'request_token=', 'port=6000', 'types=tcp'):
            self.assertIn(key, captured['data'])
        self.assertEqual(payload['status'], True)

    def test_panel_rule_helpers_use_marker_and_id(self):
        panel = {'type': 'baota', 'url': 'http://127.0.0.1:8888', 'api_key': 'k',
                 'verify_tls': False, 'timeout': 5, 'node': '', 'mode': 'auto',
                 'list_action': 'GetRules', 'add_action': 'AddRule', 'del_action': 'DelRule'}
        with patch.object(m, 'panel_request', side_effect=[
                {'status': True},                                                  # AddRule
                {'status': True, 'data': [{'id': '7', 'port': '6000', 'types': 'tcp'}]},  # GetRules
                {'status': True}]) as request:                                     # DelRule
            m.panel_add_rule(6000, 'tcp', panel)
            self.assertEqual(request.call_args_list[0][0][0], 'AddRule')
            self.assertEqual(request.call_args_list[0][0][1]['ps'], 'frp-simple-bash-auto')
            self.assertTrue(m.panel_del_rule(6000, 'tcp', panel))
            self.assertEqual(request.call_args_list[2][0][1], {'id': '7'})

    def test_mode_is_panel_api_when_configured(self):
        panel = m.panel_config()
        panel['url'] = 'http://127.0.0.1:8888'
        panel['api_key'] = 'k'
        m.save_panel_config(panel)
        info = {'panels': [{'key': 'baota', 'name': '宝塔'}], 'detail': {}, 'backend': ''}
        self.assertEqual(m.firewall_mode(info), 'panel-api')

    def test_sync_panel_api_does_not_require_system_firewall(self):
        cfg = self.cfg(enabled=True, allow_clients=['clientA'], panel_mode='panel-api')
        m.save_auto_port_config(cfg)
        panel = m.panel_config(); panel['url'] = 'http://127.0.0.1:8888'; panel['api_key'] = 'k'
        m.save_panel_config(panel)
        detected = {'backend': '', 'active': False, 'available': [], 'family': 'debian',
                    'preferred': 'ufw', 'detail': {},
                    'panels': [{'key': 'baota', 'name': '宝塔'}], 'managers': [], 'conflict': False}
        with patch.object(m, 'firewall_detect', return_value=detected), \
             patch.object(m, 'frps_online_proxies', return_value=[self.proxy(6000)]), \
             patch.object(m, 'port_conflict', return_value=False), \
             patch.object(m, '_service_pid', return_value=1), \
             patch.object(m, 'panel_rules', return_value=[]), \
             patch.object(m, 'panel_add_rule') as add, \
             patch.object(m, 'panel_del_rule') as delete, \
             patch.object(m, 'firewall_add_port') as sysadd:
            report = m.ports_sync(apply_changes=True)
        add.assert_called_once_with(6000, 'tcp')
        delete.assert_not_called()
        sysadd.assert_not_called()          # 绝不能直写系统防火墙
        self.assertEqual(report['mode'], 'panel-api')
        self.assertEqual(report['added'], [{'port': 6000, 'proto': 'tcp'}])


if __name__ == '__main__':
    unittest.main()