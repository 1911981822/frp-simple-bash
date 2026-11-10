#!/usr/bin/env python3
"""DESTRUCTIVE opt-in Debian/systemd installation test, restricted to fresh FRP paths.

Run ONLY on the explicitly authorized disposable installation paths:
  python3 tests/real_host_validation.py --allow-system-test \
    --testroot /root/frp-simple-bash-test-20261109

Uses an already downloaded, SHA256-verified archive; never downloads, changes
firewalls, reboots the OS, or accesses SSH. All FRP listeners bind to loopback.
This script itself must not be run as part of unittest discovery or CI.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from unittest.mock import patch

TESTROOT = Path('/root/frp-simple-bash-test-20261109')
INSTALL = Path('/opt/frp-simple-bash')
CONFIG = Path('/etc/frp')
CHECKSUM = 'daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804'
MARKER = '.real-host-test-owner'
ROLES = ('frps', 'frpc')


class Validation:
    def __init__(self, root):
        self.root = root
        self.run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4)
        self.artifacts = root / 'artifacts' / self.run_id
        self.owner = secrets.token_hex(32)
        self.owned = False
        self.claimed_roots = []
        self.processes = []
        self.handles = []
        self.echo = None
        self.echo_thread = None
        self.reservations = []
        self.secret_values = [self.owner]
        self.report = {
            'run_id': self.run_id,
            'environment': {'platform': platform.platform(), 'python': platform.python_version()},
            'notes': ['No OS reboot is performed. is-enabled proves registration only, not startup after a real reboot.',
                      'No firewall, nginx, Java or unrelated systemd service is modified.',
                      'Only localhost listeners and preflight-absent frp installation paths are used.'],
            'results': [], 'cleanup': [], 'ok': False,
        }
        self.manager = None

    def redact(self, value):
        value = str(value)
        for secret in sorted(self.secret_values, key=len, reverse=True):
            if secret: value = value.replace(secret, '[REDACTED]')
        return value

    def command(self, args, label, *, check=True, input_text='', timeout=90, env=None):
        result = subprocess.run(args, input=input_text, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout, env=env)
        if self.artifacts.exists():
            (self.artifacts / (label + '.log')).write_text(self.redact(result.stdout), encoding='utf-8')
        if check and result.returncode:
            raise RuntimeError(f'{label}: command failed with exit {result.returncode}; see redacted artifact')
        return result

    def systemctl(self, *args, check=True):
        return self.command(['systemctl', *args], 'systemctl-' + '-'.join(args).replace('/', '_'), check=check, timeout=45)

    def assert_true(self, condition, message):
        if not condition: raise AssertionError(message)

    def step(self, label, callback):
        start = time.monotonic()
        try:
            callback()
        except Exception as exc:
            self.report['results'].append({'name': label, 'ok': False, 'error_type': type(exc).__name__,
                                           'error': self.redact(exc), 'seconds': round(time.monotonic()-start, 3)})
            raise
        else:
            self.report['results'].append({'name': label, 'ok': True, 'seconds': round(time.monotonic()-start, 3)})

    def fresh_path(self, path):
        self.assert_true(not os.path.lexists(path), 'Refusing existing resource: ' + str(path))

    def preflight(self):
        self.assert_true(sys.platform.startswith('linux') and os.geteuid() == 0, 'Requires Linux root')
        self.assert_true(Path('/run/systemd/system').is_dir() and shutil.which('systemctl'), 'Requires real systemd')
        self.assert_true(platform.machine() in ('x86_64', 'amd64'), 'Archive and test support amd64 only')
        self.assert_true(self.root == TESTROOT and self.root.resolve() == TESTROOT and not self.root.is_symlink(), 'Testroot is fixed and must not be a symlink')
        self.assert_true(self.root.is_dir(), 'Authorized testroot must already exist')
        self.assert_true((self.root / 'source').is_dir() and not (self.root / 'source').is_symlink(), 'Missing local source checkout')
        for name in ('install-frps.sh', 'install-frpc.sh', 'uninstall.sh', 'lib/manager.py'):
            source = self.root / 'source' / name
            self.assert_true(source.is_file() and str(source.resolve()).startswith(str((self.root / 'source').resolve()) + '/'), 'Missing/escaped source file')
        self.assert_true(self.root.joinpath('frp-linux.tar.gz').is_file(), 'Missing predownloaded archive')
        digest = hashlib.sha256(self.root.joinpath('frp-linux.tar.gz').read_bytes()).hexdigest()
        self.assert_true(digest == CHECKSUM, 'Archive SHA256 mismatch')
        for path in (INSTALL, CONFIG): self.fresh_path(path)
        for role in ROLES:
            for directory in ('/usr/local/bin', '/usr/bin', '/bin', '/etc/init.d'):
                self.fresh_path(Path(directory) / role)
            for directory in ('/etc/systemd/system', '/run/systemd/system', '/lib/systemd/system', '/usr/lib/systemd/system'):
                self.fresh_path(Path(directory) / (role + '.service'))
            self.fresh_path(Path('/etc/systemd/system/multi-user.target.wants') / (role + '.service'))
            result = self.systemctl('show', role + '.service', '--property=LoadState', '--value', check=False)
            self.assert_true(result.stdout.strip() == 'not-found', 'Existing systemd service: ' + role)
            self.assert_true(shutil.which(role) is None, 'An existing FRP command is already on PATH')
        for tool in ('curl', 'tar', 'sha256sum', 'python3'):
            self.assert_true(shutil.which(tool) is not None, 'Preinstall dependency manually: ' + tool)
        self.assert_true(sys.version_info >= (3, 11), 'Debian test requires Python 3.11+; no dependency installation allowed')
        artifact_root = self.root / 'artifacts'
        self.assert_true(not artifact_root.is_symlink(), 'Artifacts directory must not be a symlink')
        self.artifacts.mkdir(parents=True, exist_ok=False, mode=0o700)
        for path in (INSTALL, CONFIG):
            path.mkdir(mode=0o700)
            marker = path / MARKER
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as out: out.write(self.owner)
            self.claimed_roots.append(path)
        self.owned = True

    def check_owned(self, path):
        self.assert_true(path in (INSTALL, CONFIG) and path.resolve() == path and not path.is_symlink(), 'Refusing ambiguous cleanup path')
        marker = path / MARKER
        self.assert_true(marker.is_file() and not marker.is_symlink() and marker.read_text() == self.owner, 'Ownership marker missing or mismatched: ' + str(path))

    def reserve_port(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.reservations.append(sock)
        return sock.getsockname()[1]

    def release_ports(self):
        for sock in self.reservations: sock.close()
        self.reservations.clear()

    def setup(self):
        os.environ['FRP_CONFIG_DIR'] = str(CONFIG)
        os.environ['FRP_BINARY_DIR'] = str(INSTALL / 'native')
        os.environ.pop('FRP_TEST_MODE', None)
        spec = importlib.util.spec_from_file_location('real_host_manager', self.root / 'source/lib/manager.py')
        self.manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.manager)
        self.assert_true(not self.manager.TEST and self.manager.ROOT == CONFIG, 'Test mode must not be active')
        self.manager.initialize()
        self.token = secrets.token_urlsafe(32)
        self.user = 'real-host-test'
        self.password = secrets.token_urlsafe(32)
        self.admin_password = secrets.token_urlsafe(32)
        self.secret_values += [self.token, self.password, self.admin_password]
        self.bind_a, self.dash_a, self.bind_b, self.dash_b, self.admin, self.remote_a, self.remote_b = [self.reserve_port() for _ in range(7)]
        class Echo(socketserver.BaseRequestHandler):
            def handle(self): self.request.sendall(self.request.recv(256))
        self.echo = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo)
        self.echo.daemon_threads = True
        self.echo_thread = threading.Thread(target=self.echo.serve_forever, daemon=True)
        self.echo_thread.start()
        self.backend = self.echo.server_address[1]
        self.manager.atomic(CONFIG / 'frps.toml', self.manager.toml(self.server_config(self.bind_a, self.dash_a)))
        self.manager.mark_hash('frps')
        self.release_ports()

    def server_config(self, bind, dashboard):
        return {'bindAddr': '127.0.0.1', 'proxyBindAddr': '127.0.0.1', 'bindPort': bind,
                'auth.method': 'token', 'auth.token': self.token,
                'webServer.addr': '127.0.0.1', 'webServer.port': dashboard,
                'webServer.user': self.user, 'webServer.password': self.password}

    def install(self, role):
        env = dict(os.environ, FRP_ARCHIVE_PATH=str(self.root / 'frp-linux.tar.gz'))
        self.command(['bash', str(self.root / 'source' / ('install-' + role + '.sh'))], 'install-' + role, env=env, timeout=120)
        self.assert_true(self.systemctl('is-active', role).stdout.strip() == 'active', role + ' not active')
        self.assert_true(self.systemctl('is-enabled', role).stdout.strip() == 'enabled', role + ' not enabled')
        for component in ROLES:
            cmd = Path('/usr/local/bin') / component
            self.assert_true(cmd.is_symlink() and os.readlink(cmd) == str(INSTALL / 'bin' / component), 'Unexpected command target')

    def make_profile(self, alias, bind, dash, mapping_name, remote):
        m = self.manager
        pid = m.create_profile({'name': alias, 'server_addr': '127.0.0.1', 'server_port': bind,
                                'dashboard_addr': '127.0.0.1', 'dashboard_port': dash, 'dashboard_scheme': 'http',
                                'admin_port': self.admin, 'tls_enable': True},
                               {'token': self.token, 'admin_password': self.admin_password,
                                'dashboard_username': self.user, 'dashboard_password': self.password})
        item = {'name': mapping_name, 'type': 'tcp', 'local_ip': '127.0.0.1',
                'local_port': self.backend, 'remote_port': remote, 'enabled': True}
        m.atomic(m.pdir(pid) / 'mappings.toml', m.toml({'schema_version': 1, 'mappings': [item]}))
        return pid

    def client_initial_config(self):
        self.pid_a = self.make_profile('测试服务器A', self.bind_a, self.dash_a, 'only_a', self.remote_a)
        self.manager.set_current(self.pid_a)
        # frpc is deliberately not installed yet; installation does native verify.
        self.manager.atomic(CONFIG / 'frpc.toml', self.manager.render(self.pid_a))
        self.manager.mark_hash('frpc')

    def eventually(self, callback, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if callback(): return
            except (OSError, ValueError, KeyError): pass
            if time.monotonic() >= deadline: raise AssertionError('Timed out waiting for localhost test state')
            time.sleep(0.2)

    def assert_connected(self, pid):
        self.eventually(lambda: self.manager.connection(pid)['state'] == 'connected')

    def echo_roundtrip(self, remote):
        with socket.create_connection(('127.0.0.1', remote), timeout=3) as sock:
            sock.sendall(b'frp-real-host-test')
            self.assert_true(sock.recv(256) == b'frp-real-host-test', 'TCP tunnel data mismatch')

    def api_checks(self):
        self.assert_connected(self.pid_a)
        m = self.manager
        data = m.api(f'http://127.0.0.1:{self.dash_a}/api/v2/clients', self.user, self.password)
        self.assert_true(any(x['clientID'] == self.pid_a and x['online'] for x in data['items']), 'Client missing from Dashboard')
        status = m.api(f'http://127.0.0.1:{self.admin}/api/status', 'frp-simple-bash', self.admin_password)
        self.assert_true([x['name'] for rows in status.values() for x in rows] == ['only_a'], 'Unexpected client proxy list')
        self.echo_roundtrip(self.remote_a)

    def service_checks(self):
        m = self.manager
        for role in ('frpc', 'frps'):
            m.service(role, 'disable')
            self.assert_true(self.systemctl('is-enabled', role, check=False).stdout.strip() == 'disabled', 'disable did not apply')
            m.service(role, 'enable')
            self.assert_true(self.systemctl('is-enabled', role).stdout.strip() == 'enabled', 'enable did not apply')
        m.service('frpc', 'stop'); self.assert_true(not m.running('frpc'), 'stop failed')
        m.service('frpc', 'start'); self.assert_connected(self.pid_a)
        m.service('frpc', 'restart'); self.assert_connected(self.pid_a)
        m.service('frps', 'stop'); self.assert_true(not m.running('frps'), 'frps stop failed')
        m.service('frps', 'start'); self.assert_connected(self.pid_a)
        m.service('frps', 'restart'); self.assert_connected(self.pid_a)

    def second_server(self):
        m = self.manager
        config_b = self.root / ('server-b-' + self.run_id + '.toml')
        m.atomic(config_b, m.toml(self.server_config(self.bind_b, self.dash_b)))
        self.secondary_config = config_b
        out = (self.artifacts / 'server-b.log').open('w', encoding='utf-8')
        self.handles.append(out)
        process = subprocess.Popen([str(INSTALL / 'native/frps'), '-c', str(config_b)], stdout=out, stderr=subprocess.STDOUT)
        self.processes.append(process)
        self.eventually(lambda: m.api(f'http://127.0.0.1:{self.dash_b}/api/v2/clients', self.user, self.password) is not None)
        self.pid_b = self.make_profile('测试服务器B', self.bind_b, self.dash_b, 'only_b', self.remote_b)

    def switch_and_isolation(self):
        m = self.manager
        self.assert_true([x['name'] for x in m.mappings(self.pid_a)] == ['only_a'], 'A isolation failed')
        self.assert_true([x['name'] for x in m.mappings(self.pid_b)] == ['only_b'], 'B isolation failed')
        ordered = m.profiles()
        b_index = next(i for i,p in enumerate(ordered, 1) if p['id'] == self.pid_b)
        with patch('builtins.input', return_value=str(b_index)):
            self.assert_true(m.select_profile() == self.pid_b, 'Number selection returned wrong profile')
        with patch('builtins.input', return_value='y'): m.switch(self.pid_b)
        self.assert_true(m.active() == self.pid_b and m.running('frpc'), 'Running switch state not retained')
        self.assert_connected(self.pid_b); self.echo_roundtrip(self.remote_b)
        runtime = m.read_toml(CONFIG / 'frpc.toml')
        self.assert_true([x['name'] for x in runtime['proxies']] == ['only_b'], 'A proxy leaked into B runtime')
        m.service('frpc', 'stop'); m.switch(self.pid_a)
        self.assert_true(m.active() == self.pid_a and not m.running('frpc'), 'Stopped switch unexpectedly started client')
        m.service('frpc', 'start'); self.assert_connected(self.pid_a)

    def mapping_changes(self):
        m = self.manager
        items = m.mappings(self.pid_a); items[0]['enabled'] = False
        with patch('builtins.input', return_value='y'): m.save_mappings(self.pid_a, items)
        self.assert_true(not m.read_toml(CONFIG / 'frpc.toml').get('proxies'), 'Disabled mapping still generated')
        items[0]['enabled'] = True
        with patch('builtins.input', return_value='y'): m.save_mappings(self.pid_a, items)
        self.assert_connected(self.pid_a); self.echo_roundtrip(self.remote_a)

    def external_import_and_delete(self):
        m = self.manager
        content = (CONFIG / 'frpc.toml').read_text()
        m.atomic(CONFIG / 'frpc.toml', content.replace('name = "only_a"', 'name = "imported_a"'))
        with patch('builtins.input', return_value='1'): m.import_external('frpc')
        self.assert_true([x['name'] for x in m.mappings(self.pid_a)] == ['imported_a'], 'External change not imported')
        m.service('frpc', 'restart'); self.assert_connected(self.pid_a)
        # Exercise actual profile delete menu; snapshot only backup names, no secrets.
        before = {p.name for p in (CONFIG / 'backups').iterdir()}
        with patch('builtins.input', side_effect=['9', 'y']): m.edit_profile()
        self.assert_true(not m.pdir(self.pid_a).exists() and m.active() == '' and not m.running('frpc'), 'Delete did not clear owned active profile')
        self.assert_true(m.pdir(self.pid_b).is_dir(), 'Deleting A removed B')
        self.assert_true(before == {p.name for p in (CONFIG / 'backups').iterdir()}, 'Delete unexpectedly created backup')
        m.switch(self.pid_b); m.service('frpc', 'start'); self.assert_connected(self.pid_b)
        for role in ROLES:
            status = self.command(['/usr/local/bin/' + role, 'status'], 'status-' + role)
            self.assert_true(json.loads(status.stdout)['service_state'] == 'active', 'Installed status command failed')
            r = self.command(['/usr/local/bin/' + role], 'menu-' + role, input_text='0\n')
            self.assert_true('请输入数字' in r.stdout, 'Menu smoke failed')

    def uninstall_preserve(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (CONFIG / 'profiles').glob('*/*.toml')}
        for role in reversed(ROLES):
            self.command(['bash', str(self.root / 'source/uninstall.sh'), role], 'uninstall-' + role)
            self.assert_true(not os.path.lexists(Path('/usr/local/bin') / role), 'Uninstall left command')
            self.assert_true(not (Path('/etc/systemd/system') / (role + '.service')).exists(), 'Uninstall left unit')
        for path, digest in before.items():
            self.assert_true(path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest, 'Uninstall did not preserve profile config')

    def cleanup(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)
        for handle in self.handles: handle.close()
        if self.echo:
            self.echo.shutdown(); self.echo.server_close()
        self.release_ports()
        if hasattr(self, 'secondary_config'):
            path = self.secondary_config
            if path.parent == TESTROOT and path.name == 'server-b-' + self.run_id + '.toml': path.unlink(missing_ok=True)
        if not self.owned:
            # Partial preflight creation: no installer has run, remove only claimed roots.
            for path in self.claimed_roots:
                self.check_owned(path)
                shutil.rmtree(path)
            return
        # Markers and exact canonical roots must match before invoking uninstall.
        for path in (CONFIG, INSTALL): self.check_owned(path)
        for role in reversed(ROLES):
            unit = Path('/etc/systemd/system') / (role + '.service')
            if unit.exists():
                self.assert_true(not unit.is_symlink() and f'ExecStart={INSTALL}/native/{role} -c {CONFIG}/{role}.toml' in unit.read_text(), 'Refusing foreign unit cleanup: ' + role)
            command = Path('/usr/local/bin') / role
            if os.path.lexists(command):
                self.assert_true(command.is_symlink() and os.readlink(command) == str(INSTALL / 'bin' / role), 'Refusing foreign command cleanup')
            self.command(['bash', str(self.root / 'source/uninstall.sh'), role], 'cleanup-' + role)
            self.assert_true(not os.path.lexists(command) and not os.path.lexists(unit), 'Cleanup left command/unit')
            load = self.systemctl('show', role + '.service', '--property=LoadState', '--value', check=False)
            self.assert_true(load.stdout.strip() == 'not-found', 'Cleanup left registered unit')
        for path in (CONFIG, INSTALL):
            self.check_owned(path)
            shutil.rmtree(path)
            self.assert_true(not os.path.lexists(path), 'Owned path cleanup failed')
        self.report['cleanup'].append({'name':'owned-frp-resources', 'ok': True})

    def run(self):
        error = None
        try:
            self.step('preflight-and-ownership', self.preflight)
            with contextlib.redirect_stdout(io.StringIO()):
                self.step('prepare-loopback-config', self.setup)
                self.step('install-frps-active-enabled', lambda: self.install('frps'))
                self.step('prepare-profile-a', self.client_initial_config)
                self.step('install-frpc-active-enabled', lambda: self.install('frpc'))
                self.step('dashboard-admin-api-tcp-roundtrip', self.api_checks)
                self.step('systemd-stop-start-restart-enable-disable', self.service_checks)
                self.step('second-loopback-server-profile-b', self.second_server)
                self.step('number-selection-switch-and-isolation', self.switch_and_isolation)
                self.step('mapping-disable-enable-restart', self.mapping_changes)
                self.step('external-import-profile-delete-menu-smoke', self.external_import_and_delete)
                self.step('uninstall-preserves-configuration', self.uninstall_preserve)
            self.report['ok'] = True
        except BaseException as exc:
            error = exc
            self.report['fatal_error'] = {'type': type(exc).__name__, 'message': self.redact(exc)}
        finally:
            try: self.cleanup()
            except BaseException as exc:
                self.report['ok'] = False
                self.report['cleanup'].append({'ok':False, 'error':self.redact(exc)})
                if error is None: error = exc
            if self.artifacts.exists():
                # Redact subprocess output once more; never include configuration files.
                for path in self.artifacts.glob('*.log'):
                    path.write_text(self.redact(path.read_text(errors='replace')), encoding='utf-8')
                    path.chmod(0o600)
                report_path = self.artifacts / 'report.json'
                report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                report_path.chmod(0o600)
                print('Report: ' + str(report_path))
            print('PASS' if self.report['ok'] else 'FAIL (review redacted report and cleanup status)')
        return 0 if self.report['ok'] and error is None else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-system-test', action='store_true', help='Explicitly authorize creation/removal of the fresh default FRP installation')
    parser.add_argument('--testroot', required=True, type=Path)
    args = parser.parse_args()
    if not args.allow_system_test: parser.error('Refusing system test without --allow-system-test')
    if args.testroot != TESTROOT: parser.error('Only /root/frp-simple-bash-test-20261109 is allowed')
    def interrupted(signum, frame): raise KeyboardInterrupt('Interrupted; cleaning up owned resources')
    signal.signal(signal.SIGTERM, interrupted)
    return Validation(args.testroot).run()


if __name__ == '__main__':
    sys.exit(main())
