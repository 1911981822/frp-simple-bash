"""Opt-in tests against official frp binaries: FRP_NATIVE_DIR=/path python -m unittest discover -s tests."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import threading
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('native_manager', Path(__file__).resolve().parents[1] / 'lib' / 'manager.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]

@unittest.skipUnless(os.environ.get('FRP_NATIVE_DIR'), 'Set FRP_NATIVE_DIR to verified frp 0.72.0 binaries')
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.patch = patch.multiple(m, ROOT=self.root, BIN=Path(os.environ['FRP_NATIVE_DIR']).resolve(), TEST=False)
        self.patch.start(); m.initialize(); self.processes=[]; self.files=[]
    def tearDown(self):
        for p in reversed(self.processes):
            p.terminate()
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=5)
        for f in self.files: f.close()
        self.patch.stop(); self.temp.cleanup()
    def launch(self, role, config):
        output = (self.root / (role + '.stdout')).open('w'); self.files.append(output)
        p = subprocess.Popen([str(m.binary(role)), '-c', str(config)], stdout=output, stderr=subprocess.STDOUT)
        self.processes.append(p); return p
    def test_generated_config_and_real_api(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(self): self.request.sendall(self.request.recv(64))
        backend = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo)
        thread = threading.Thread(target=backend.serve_forever, daemon=True); thread.start()
        self.addCleanup(backend.server_close); self.addCleanup(backend.shutdown)
        local_port = backend.server_address[1]
        bind, dashboard, admin, remote, http_port, https_port = [free_port() for _ in range(6)]
        token = secrets.token_hex(16)
        server = m.toml({'bindAddr':'127.0.0.1','proxyBindAddr':'127.0.0.1','bindPort':bind,'auth.method':'token','auth.token':token,'webServer.addr':'127.0.0.1','webServer.port':dashboard,'webServer.user':'admin','webServer.password':'test-password','vhostHTTPPort':http_port,'vhostHTTPSPort':https_port})
        m.verify('frps', server); m.atomic(self.root / 'frps.toml', server)
        self.launch('frps', self.root / 'frps.toml')
        pid = m.create_profile({'name':'生产服务器_01','server_addr':'127.0.0.1','server_port':bind,'dashboard_port':dashboard,'admin_port':admin,'tls_enable':True}, {'token':token,'admin_password':'local-test-password'})
        rows = [
            {'name':'tcp-test','type':'tcp','local_ip':'127.0.0.1','local_port':local_port,'remote_port':remote,'enabled':True},
            {'name':'udp-test','type':'udp','local_ip':'127.0.0.1','local_port':local_port,'remote_port':remote,'enabled':True},
            {'name':'http-test','type':'http','local_ip':'127.0.0.1','local_port':local_port,'domain':'test.example.com','enabled':True},
            {'name':'https-test','type':'https','local_ip':'127.0.0.1','local_port':local_port,'domain':'test.example.com','enabled':True},
        ]
        m.atomic(m.pdir(pid) / 'mappings.toml', m.toml({'schema_version':1,'mappings':rows}))
        m.set_current(pid); m.apply(pid)
        self.launch('frpc', self.root / 'frpc.toml')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                clients = m.api(f'http://127.0.0.1:{dashboard}/api/v2/clients', 'admin', 'test-password')
                status = m.api(f'http://127.0.0.1:{admin}/api/status', 'frp-simple-bash', 'local-test-password')
                proxies = [x for values in status.values() for x in values]
                if any(x.get('online') and x.get('clientID') == pid for x in clients['items']) and len(proxies) == 4 and all(x['status']=='running' for x in proxies): break
            except (OSError, ValueError): pass
            time.sleep(0.1)
        else: self.fail('Native frpc failed to become online (see native logs in temporary directory during test)')
        self.assertEqual(len(proxies), 4)
        with socket.create_connection(('127.0.0.1', remote), timeout=3) as tunnel:
            tunnel.sendall(b'frp-simple-bash'); self.assertEqual(tunnel.recv(64), b'frp-simple-bash')
        listing = m.api(f'http://127.0.0.1:{dashboard}/api/v2/proxies', 'admin', 'test-password')
        self.assertEqual(listing['total'], 4)
        request = urllib.request.Request(f'http://127.0.0.1:{dashboard}/api/v2/clients')
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as err: opener.open(request, timeout=2)
        self.assertEqual(err.exception.code, 401)
        err.exception.close()
        with patch.object(m, 'running', return_value=True): self.assertEqual(m.connection(pid)['state'], 'connected')
        # Disabled proxies are absent from the generated runtime config.
        rows[0]['enabled'] = False
        m.atomic(m.pdir(pid) / 'mappings.toml', m.toml({'schema_version':1,'mappings':rows})); m.apply(pid)
        self.assertNotIn('name = "tcp-test"', (self.root / 'frpc.toml').read_text())
    def test_ipv6_native_login(self):
        try:
            with socket.socket(socket.AF_INET6) as sock:
                sock.bind(('::1', 0)); bind = sock.getsockname()[1]
        except OSError: self.skipTest('IPv6 loopback unavailable')
        dashboard, admin = free_port(), free_port()
        config = m.toml({'bindAddr':'::1','proxyBindAddr':'127.0.0.1','bindPort':bind,'auth.method':'token','auth.token':'ipv6-test','webServer.addr':'127.0.0.1','webServer.port':dashboard,'webServer.user':'admin','webServer.password':'password'})
        m.verify('frps', config); m.atomic(self.root / 'frps.toml', config); self.launch('frps', self.root / 'frps.toml')
        pid = m.create_profile({'name':'IPv6_服务器','server_addr':'::1','server_port':bind,'admin_port':admin,'dashboard_port':dashboard,'tls_enable':True}, {'token':'ipv6-test','admin_password':'local-password'})
        m.set_current(pid); m.apply(pid); self.launch('frpc', self.root / 'frpc.toml')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                data=m.api(f'http://127.0.0.1:{dashboard}/api/v2/clients','admin','password')
                if any(x.get('online') and x.get('clientID') == pid for x in data['items']): return
            except (OSError, ValueError): pass
            time.sleep(0.1)
        self.fail('IPv6 client did not become online')
    def test_verify_rejects_invalid_config_without_overwrite(self):
        m.atomic(self.root / 'frps.toml', 'bindPort = 7000\n')
        with self.assertRaises(ValueError): m.verify('frps', 'bindPort = "not-a-port"\n')
        self.assertEqual((self.root / 'frps.toml').read_text(), 'bindPort = 7000\n')

if __name__ == '__main__': unittest.main()
