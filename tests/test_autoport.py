"""端口自动开通（auto-port）单元测试。

覆盖：配置默认值与往返、白名单/黑名单/配额过滤、端口冲突、diff 同步、
回收策略、一键撤销、防火墙命令构造（必须参数化、不得拼接 shell）、审计。
"""
import contextlib
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
        with patch.object(m, 'firewall_backend', return_value=('ufw', False)):
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
        with patch.object(m, 'firewall_backend', return_value=(backend, True)), \
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
        with patch.object(m, 'firewall_backend', return_value=('ufw', False)):
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


if __name__ == '__main__':
    unittest.main()