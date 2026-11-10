#!/usr/bin/env python3
"""frp-simple-bash shared management engine; requires Python >= 3.11."""
from __future__ import annotations
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
try:
    import tomllib as _toml_lib
except ImportError:
    try:
        import tomli as _toml_lib
    except ImportError:
        _toml_lib = None
import urllib.request
import urllib.error
import base64
import socket

VERSION = '0.72.0'
ROOT = Path(os.environ.get('FRP_CONFIG_DIR', '/etc/frp' if os.name != 'nt' else os.environ.get('ProgramData', 'C:/ProgramData') + '/frp-simple-bash'))
BIN = Path(os.environ.get('FRP_BINARY_DIR', '/opt/frp-simple-bash/native' if os.name != 'nt' else os.environ.get('ProgramFiles', 'C:/Program Files') + '/frp-simple-bash/native'))
TEST = os.environ.get('FRP_TEST_MODE') == '1'

def unlink_quiet(path):
    try: os.unlink(str(path))
    except OSError: pass

def run_cmd(args, **kwargs):
    if 'capture_output' in kwargs:
        capture = kwargs.pop('capture_output')
        if capture:
            kwargs.setdefault('stdout', subprocess.PIPE)
            kwargs.setdefault('stderr', subprocess.PIPE)
    if 'text' in kwargs:
        yes = kwargs.pop('text')
        if yes: kwargs.setdefault('universal_newlines', True)
    return subprocess.run(args, **kwargs)

def text(value):
    return json.dumps(value, ensure_ascii=False)

def toml(data):
    """Serialize the manager schema: scalars, scalar arrays, nested tables and arrays of tables."""
    lines = []
    def scalar(v):
        if isinstance(v, bool): return str(v).lower()
        if isinstance(v, (int, float)): return str(v)
        if isinstance(v, str): return text(v)
        if isinstance(v, list): return '[' + ', '.join(scalar(x) for x in v) + ']'
        raise ValueError('不支持的配置字段类型')
    def table(obj, prefix=''):
        scalars, tables = [], []
        for k, v in obj.items():
            if isinstance(v, dict): tables.append((k, v))
            elif isinstance(v, list) and v and isinstance(v[0], dict): tables.append((k, v))
            else: scalars.append((k, v))
        for k, v in scalars: lines.append(f'{k} = {scalar(v)}')
        for k, v in tables:
            name = f'{prefix}{k}'
            if isinstance(v, dict):
                lines.append(f'\n[{name}]'); table(v, name + '.')
            else:
                for item in v:
                    lines.append(f'\n[[{name}]]'); table(item, name + '.')
    table(data)
    return '\n'.join(lines) + '\n'

def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(5) + '.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        unlink_quiet(tmp)

def managed_paths():
    return set(ROOT.glob('*.json')) | set(ROOT.glob('*.toml')) | set((ROOT / 'profiles').glob('*/*.toml'))

@contextlib.contextmanager
def transaction():
    """Restore model, generated config, index and state together on any write failure."""
    originals = {p: p.read_bytes() for p in managed_paths()}
    try: yield
    except Exception:
        for p in managed_paths() - set(originals): unlink_quiet(p)
        for p, value in originals.items(): atomic(p, value.decode('utf-8-sig'))
        raise

def _mini_toml(text):
    """Dependency-free TOML subset parser (tables, arrays of tables, strings, ints, bools, arrays)."""
    root, path = {}, []
    def target():
        node = root
        for key in path:
            node = node.setdefault(key, {})
        # Inside an array of tables, assign to its newest element.
        if isinstance(node, list): node = node[-1]
        return node
    def scalar(raw):
        raw = raw.strip()
        if raw[:1] in ('"', "'"):
            quote = raw[0]
            if len(raw) < 2 or not raw.endswith(quote): raise ValueError('字符串未闭合：' + raw)
            body = raw[1:-1]
            if quote == '"':
                body = body.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t').replace('\\\\', '\\')
            return body
        if raw in ('true', 'false'): return raw == 'true'
        if raw.startswith('['):
            if not raw.endswith(']'): raise ValueError('数组未闭合：' + raw)
            inner = raw[1:-1].strip()
            return [scalar(x) for x in inner.split(',')] if inner else []
        try: return int(raw)
        except ValueError:
            try: return float(raw)
            except ValueError: raise ValueError('无法解析的值：' + raw)
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('[['):
            if not line.endswith(']]'): raise ValueError('表数组未闭合（第 %d 行）' % number)
            parts = line[2:-2].strip().split('.')
            parent = root
            for part in parts[:-1]: parent = parent.setdefault(part, {})
            parent.setdefault(parts[-1], []).append({})
            path = parts
            continue
        if line.startswith('['):
            if not line.endswith(']'): raise ValueError('表未闭合（第 %d 行）' % number)
            path = line[1:-1].strip().split('.')
            target()
            continue
        if '=' not in line: raise ValueError('缺少 =（第 %d 行）' % number)
        key, value = line.split('=', 1)
        target()[key.strip()] = scalar(value)
    return root

def _load_toml(text):
    if _toml_lib is not None: return _toml_lib.loads(text)
    return _mini_toml(text)

def read_toml(path):
    return _load_toml(Path(path).read_text(encoding='utf-8-sig'))

def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else ({} if default is None else default)

def save_json(path, data): atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')

@contextlib.contextmanager
def lock():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = ROOT / '.operation.lock'
    try: p.mkdir(mode=0o700)
    except FileExistsError: raise ValueError('另一个配置操作正在执行；若进程已异常退出，请检查后移除 .operation.lock')
    try: yield
    finally: p.rmdir()

def initialize():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    for n in ('profiles', 'backups', 'logs'): (ROOT / n).mkdir(exist_ok=True, mode=0o700)
    if not (ROOT / 'state.json').exists(): save_json(ROOT / 'state.json', {'schema_version': 1, 'active_profile_id': '', 'pending': False})
    rotate_logs()

def rotate_logs():
    files = [p for p in (ROOT / 'logs').glob('*') if p.is_file() and not p.is_symlink()]
    cutoff = time.time() - 30 * 86400
    for p in files:
        if p.stat().st_mtime < cutoff and p.suffix != '.log': unlink_quiet(p)
    files = [p for p in files if p.exists()]
    limit = 500 * 1024 * 1024
    # Never unlink a live log: truncate at a bounded size before reclaiming archives.
    for p in files:
        if p.suffix == '.log' and p.stat().st_size > 50 * 1024 * 1024:
            with p.open('rb') as f: f.seek(-1024 * 1024, 2); tail = f.read()
            archive = p.with_name(p.name + '.' + str(time.time_ns()))
            shutil.copy2(p, archive)
            with p.open('wb') as f: f.write(tail)
    files = sorted([p for p in (ROOT / 'logs').glob('*') if p.is_file() and not p.is_symlink()], key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= limit: break
        if p.suffix == '.log': continue
        size = p.stat().st_size; p.unlink(); total -= size

def audit(event, pid=''):
    # Only fixed event names and validated IDs; never serialize caller data/secrets.
    if pid: pdir(pid)
    with (ROOT / 'logs' / 'manager.log').open('a', encoding='utf-8') as f:
        f.write(json.dumps({'time': dt.datetime.now(dt.timezone.utc).isoformat(), 'event': event, 'profile_id': pid}) + '\n')
    if os.name != 'nt': os.chmod(ROOT / 'logs' / 'manager.log', 0o600)

def state(): return read_json(ROOT / 'state.json')
def active(): return state().get('active_profile_id', '')
def pdir(pid):
    if not re.fullmatch(r'[a-f0-9]{24}', pid): raise ValueError('非法 profile ID')
    return ROOT / 'profiles' / pid

def load_profile(pid):
    p = read_toml(pdir(pid) / 'profile.toml')
    if p.get('id') != pid: raise ValueError('profile ID 与目录不一致')
    validate_profile(p, pid)
    return p

def profiles():
    result = []
    for path in sorted((ROOT / 'profiles').glob('*/profile.toml')):
        result.append(load_profile(path.parent.name))
    return sorted(result, key=lambda p: (p['name'].casefold(), p['id']))

def port(v):
    if isinstance(v, bool) or not str(v).isdigit() or not 1 <= int(v) <= 65535: raise ValueError('端口必须是 1-65535')
    return int(v)

def address(v):
    if not isinstance(v, str) or not v or any(c.isspace() for c in v): raise ValueError('地址不能为空或包含空白')
    try: ipaddress.ip_address(v); return v
    except ValueError: pass
    if len(v) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part) for part in v.rstrip('.').split('.')): raise ValueError('请填写合法 IPv4、IPv6 或域名，不包含协议或端口')
    return v

def name(v):
    if not isinstance(v, str) or not 1 <= len(v) <= 64 or not re.fullmatch(r'[A-Za-z0-9_\-\u3400-\u9fff]+', v): raise ValueError('名称只允许中文、字母、数字、-、_，长度 1-64，不含空格')
    return v

def label(p):
    addr = '[' + p['server_addr'] + ']' if ':' in p['server_addr'] else p['server_addr']
    return f"{p['name']}（{addr}:{p['server_port']}）"

def validate_profile(p, pid=''):
    name(p['name']); address(p['server_addr']); port(p['server_port']); port(p.get('dashboard_port', 7500))
    if not isinstance(p.get('tls_enable', True), bool): raise ValueError('TLS 必须为布尔值')
    if p.get('dashboard_addr'): address(p['dashboard_addr'])
    if p.get('dashboard_scheme', 'http') not in ('http', 'https'): raise ValueError('Dashboard 协议必须为 http/https')
    # ID remains stable when renaming.

def unique_name(value, except_id=''):
    name(value)
    for p in profiles():
        if p['id'] != except_id and p['name'].casefold() == value.casefold(): raise ValueError('名称已存在，请重新输入')

def mappings(pid):
    data = read_toml(pdir(pid) / 'mappings.toml')
    items = data.get('mappings', [])
    if not isinstance(items, list): raise ValueError('mappings 必须是数组')
    seen = set(); ports = set()
    for m in items:
        validate_mapping(m)
        if m['name'] in seen: raise ValueError('映射名称重复')
        seen.add(m['name'])
        if m['type'] in ('tcp', 'udp') and m.get('enabled', True):
            key = (m['type'], m['remote_port'])
            if key in ports: raise ValueError('同类型远程端口冲突')
            ports.add(key)
    return items

def validate_mapping(m):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', m['name']): raise ValueError('映射名称允许 1-64 个 ASCII 字母、数字、-、_')
    if m['type'] not in ('tcp', 'udp', 'http', 'https'): raise ValueError('仅支持 TCP/UDP/HTTP/HTTPS')
    address(m.get('local_ip', '127.0.0.1')); port(m['local_port'])
    if m['type'] in ('tcp', 'udp'): port(m['remote_port'])
    else:
        if not m.get('domain'): raise ValueError('HTTP/HTTPS 必须填写域名')
        address(m['domain'])
    if not isinstance(m.get('enabled', True), bool): raise ValueError('enabled 必须为布尔值')

def secrets_for(pid): return read_toml(pdir(pid) / 'secrets.toml')

def create_profile(p, secret):
    with lock(), transaction():
        unique_name(p['name']); validate_profile(p)
        pid = secrets.token_hex(12); p = dict(p, id=pid, schema_version=1)
        folder = pdir(pid); folder.mkdir(mode=0o700)
        try:
            atomic(folder / 'profile.toml', toml(p))
            atomic(folder / 'secrets.toml', toml(secret))
            atomic(folder / 'mappings.toml', 'schema_version = 1\nmappings = []\n')
            rebuild_index(); audit('profile-created', pid)
        except Exception:
            shutil.rmtree(folder); raise
        return pid

def rebuild_index():
    save_json(ROOT / 'index.json', {'schema_version': 1, 'profiles': profiles()})

def binary(role): return BIN / (role + ('.exe' if os.name == 'nt' else ''))
def verify(role, content):
    _load_toml(content)
    if TEST: return
    if not binary(role).exists(): raise ValueError('原生 frp 尚未安装；请先运行安装器')
    tmp = ROOT / ('.verify-' + secrets.token_hex(6) + '.toml')
    atomic(tmp, content)
    try:
        r = run_cmd([str(binary(role)), 'verify', '-c', str(tmp)], capture_output=True, timeout=15)
        if r.returncode: raise ValueError('frp 配置校验失败；原配置已保留（输出可能含敏感字段，未回显）')
    finally: unlink_quiet(tmp)

def render(pid):
    p = load_profile(pid); s = secrets_for(pid); items = mappings(pid)
    lines = [f'serverAddr = {text(p["server_addr"])}', f'serverPort = {p["server_port"]}',
             f'transport.tls.enable = {str(p.get("tls_enable", True)).lower()}',
             f'transport.protocol = {text(p.get("transport_protocol", "tcp"))}',
             'loginFailExit = false', f'clientID = {text(pid)}',
             f'log.to = {text(str(ROOT / "logs" / "frpc.log"))}',
             f'log.level = {text(p.get("log_level", "info"))}',
             f'log.maxDays = {int(p.get("log_max_days", 30))}', 'log.disablePrintColor = true']
    if p.get('tls_use_system_ca'): lines.append('transport.tls.useSystemCa = true')
    if p.get('transport_use_encryption'): lines.append('transport.useEncryption = true')
    if p.get('transport_use_compression'): lines.append('transport.useCompression = true')
    for key in ('certFile', 'keyFile', 'trustedCaFile'):
        if s.get('tls_' + key): lines.append(f'transport.tls.{key} = {text(s["tls_" + key])}')
    if s.get('token'): lines += ['auth.method = "token"', f'auth.token = {text(s["token"])}']
    lines += ['webServer.addr = "127.0.0.1"', f'webServer.port = {p.get("admin_port", 7400)}',
              'webServer.user = "frp-simple-bash"', f'webServer.password = {text(s["admin_password"])}']
    for m in items:
        if not m.get('enabled', True): continue
        lines += ['', '[[proxies]]', f'name = {text(m["name"])}', f'type = {text(m["type"])}',
                  f'localIP = {text(m.get("local_ip", "127.0.0.1"))}', f'localPort = {m["local_port"]}']
        if m['type'] in ('tcp', 'udp'): lines.append(f'remotePort = {m["remote_port"]}')
        else: lines.append(f'customDomains = [{text(m["domain"])}]')
    return '\n'.join(lines) + '\n'

def mark_hash(role):
    path = ROOT / (role + '.toml')
    s = state(); s[role + '_hash'] = hashlib.sha256(path.read_bytes()).hexdigest(); save_json(ROOT / 'state.json', s)
    if role == 'frps': atomic(ROOT / '.canonical-frps.toml', path.read_text(encoding='utf-8-sig'))

def apply(pid):
    content = render(pid); verify('frpc', content)
    atomic(ROOT / 'frpc.toml', content); mark_hash('frpc')

def set_current(pid):
    s = state(); s['active_profile_id'] = pid; s['pending'] = False; save_json(ROOT / 'state.json', s); audit('profile-selected', pid)

def service(role, action):
    if TEST: return 'stopped' if action == 'status' else None
    if role == 'frpc' and action in ('start', 'restart'):
        logfile = ROOT / 'logs' / 'frpc.log'
        current = state(); current['frpc_log_offset'] = logfile.stat().st_size if logfile.exists() else 0
        save_json(ROOT / 'state.json', current)
    if os.name == 'nt':
        wrapper = BIN.parent / 'services' / ('frp-simple-bash-' + role + '.exe')
        if not wrapper.exists(): raise ValueError('Windows 服务包装器未安装')
        if action in ('enable', 'disable'):
            result = run_cmd(['sc.exe', 'config', 'frp-simple-bash-' + role, 'start=', 'auto' if action == 'enable' else 'demand'], capture_output=True, text=True, timeout=30)
        else: result = run_cmd([str(wrapper), action], capture_output=True, text=True, timeout=30)
    elif shutil.which('systemctl') and Path('/run/systemd/system').exists():
        args = ['systemctl', 'is-active' if action == 'status' else action, role]
        result = run_cmd(args, capture_output=True, text=True, timeout=30)
    elif shutil.which('rc-service'):
        if action in ('enable', 'disable'):
            result = run_cmd(['rc-update', 'add' if action == 'enable' else 'del', role, 'default'], capture_output=True, text=True, timeout=30)
        else: result = run_cmd(['rc-service', role, action], capture_output=True, text=True, timeout=30)
    else: return 'unknown' if action == 'status' else fail('无支持的服务管理器，无法保证后台运行与自启动')
    if action == 'status': return result.stdout.strip() or 'stopped'
    if result.returncode: raise ValueError('服务操作失败：' + result.stderr.strip())

def fail(message): raise ValueError(message)
def running(role): return service(role, 'status').lower() in ('active', 'started', 'running')

def api(url, user, password):
    req = urllib.request.Request(url)
    if user or password: req.add_header('Authorization', 'Basic ' + base64.b64encode((user + ':' + password).encode()).decode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=2) as response: data = json.load(response)
    if '/api/v2/' in url:
        if data.get('code') != 200 or 'data' not in data: raise ValueError('Dashboard v2 API 返回错误')
        return data['data']
    return data

def connection(pid):
    if not running('frpc'): return {'state': 'stopped'}
    p = load_profile(pid); s = secrets_for(pid)
    try:
        data = api(f'http://127.0.0.1:{p.get("admin_port", 7400)}/api/status', 'frp-simple-bash', s['admin_password'])
        rows = [x for group in data.values() for x in group]
        if rows: return {'state': 'connected' if all(x.get('status') == 'running' for x in rows) else 'proxy-error', 'proxies': rows}
        # An empty proxy list alone cannot prove login success.
        logfile = ROOT / 'logs' / 'frpc.log'
        if logfile.exists():
            with logfile.open('rb') as f:
                size = logfile.stat().st_size
                f.seek(min(state().get('frpc_log_offset', size), size))
                logs = f.read()[-262144:].decode('utf-8', errors='replace')
            success = logs.rfind('login to server success')
            failure = max(logs.rfind('login to server failed'), logs.rfind('try to reconnect'), logs.rfind('control writer is closing'))
            if success >= 0 and success > failure: return {'state': 'connected', 'source': 'current-process-log'}
            if failure > success:
                recent = logs[max(0, failure - 256):].lower()
                if 'token' in recent or 'authentication' in recent: return {'state': 'auth-failed', 'detail': 'token 或认证参数不匹配'}
                if 'tls' in recent or 'certificate' in recent: return {'state': 'tls-failed', 'detail': 'TLS 证书或信任配置错误'}
        if s.get('dashboard_username'):
            host = p.get('dashboard_addr') or p['server_addr']; host = '[' + host + ']' if ':' in host else host
            scheme = p.get('dashboard_scheme', 'http')
            data = api(f'{scheme}://{host}:{p.get("dashboard_port", 7500)}/api/v2/clients?pageSize=200', s['dashboard_username'], s.get('dashboard_password', ''))
            for item in data.get('items', []):
                if item.get('clientID') == pid and item.get('online'): return {'state': 'connected'}
        return {'state': 'unknown', 'detail': '无启用映射且无可访问服务端 API，无法确认登录状态'}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403): return {'state': 'auth-failed', 'detail': f'管理 API 认证失败（HTTP {exc.code}）'}
        return {'state': 'connecting', 'detail': f'管理 API 返回 HTTP {exc.code}'}
    except (OSError, ValueError): return {'state': 'connecting'}

def wait_connection(pid):
    deadline = time.monotonic() + 30
    while True:
        result = connection(pid)
        if result['state'] == 'connected': print('连接成功'); return True
        if result['state'] in ('auth-failed', 'tls-failed'):
            print('连接失败：' + result.get('detail', result['state']) + '；保留当前 profile。'); return False
        if time.monotonic() >= deadline: print('连接检测超时（30 秒），保留新 profile；请检查认证、网络和日志。'); return False
        time.sleep(min(2, deadline - time.monotonic()))

def switch(pid):
    with lock(), transaction():
        load_profile(pid); content = render(pid); verify('frpc', content)
        was_running = running('frpc')
        if was_running and not confirm('切换将中断当前连接，确认继续？'): return
        old = (ROOT / 'frpc.toml').read_bytes() if (ROOT / 'frpc.toml').exists() else None
        if was_running: service('frpc', 'stop')
        try:
            atomic(ROOT / 'frpc.toml', content); set_current(pid); mark_hash('frpc')
        except Exception:
            if old is not None: atomic(ROOT / 'frpc.toml', old.decode('utf-8'))
            if was_running: service('frpc', 'start')
            raise
        if was_running: service('frpc', 'start'); wait_connection(pid)
        else: print('已切换当前配置，frpc 保持停止。')

RULE = '━' * 46
CANCEL_KEYS = ('q', 'Q')
UI_CLEAR = os.environ.get('FRP_UI_CLEAR') == '1'

def ask(prompt, default=None, secret=False, example=None):
    """Prompt once. 'q' cancels the current operation and returns None."""
    hint = ''
    if example: hint += f'（例：{example}）'
    if default is not None: hint += f'（回车使用 {default}）'
    if secret: hint += '（输入不回显）'
    while True:
        try:
            if secret:
                import getpass
                v = getpass.getpass(f'\n  {prompt}{hint}：')
            else:
                v = input(f'\n  {prompt}{hint}：')
        except EOFError:
            return None
        v = v.strip()
        if v in CANCEL_KEYS:
            print('  [提示] 已取消当前操作。')
            return None
        if not v and default is not None: return default
        if v or default is None: return v

def confirm(prompt, danger=False):
    if danger:
        answer = ask(f'{prompt}（确认请输入 YES，其他内容取消）')
        return (answer or '').strip() == 'YES'
    return (ask(prompt + ' [y/N]') or '').lower() in ('y', 'yes')

def clear_screen():
    if UI_CLEAR and sys.stdout.isatty(): print('\033[2J\033[H', end='')

def section(title):
    print()
    print(RULE)
    print(' ' + title)
    print(RULE)

def pause(message='按回车返回上一级菜单'):
    if sys.stdin.isatty():
        try: input(f'  {message} … ')
        except EOFError: pass

def result_ok(message, detail=None):
    print(f'\n[成功] {message}')
    if detail:
        for line in str(detail).splitlines(): print('       ' + line)

def result_info(message, detail=None):
    print(f'\n[提示] {message}')
    if detail:
        for line in str(detail).splitlines(): print('       ' + line)

def result_error(message, hint=None):
    print(f'\n[失败] {message}')
    if hint:
        for line in str(hint).splitlines(): print('       建议：' + line)

def choose(items, title='请选择', back_label='返回上一级'):
    section(title)
    for n, item in enumerate(items, 1): print(f'  {n}) {item}')
    print(f'  0) {back_label}')
    while True:
        v = ask(f'请选择 [0-{len(items)}]')
        if v is None: return None
        if v.isdecimal() and 0 <= int(v) <= len(items): return int(v)
        result_error('无效编号', f'请输入 0 到 {len(items)} 之间的数字，输入 q 取消。')

def require_active():
    pid = active()
    if not pid: raise ValueError('请先新建或选择 frps 服务端')
    load_profile(pid); return pid

def select_profile():
    ps = profiles()
    n = choose([label(p) + (' [当前]' if p['id'] == active() else '') + f' [{len(mappings(p["id"]))} 个映射]' for p in ps], '服务端列表')
    return ps[n-1]['id'] if n else None

def profile_wizard():
    p = {'name': ask('服务端名称'), 'server_addr': ask('服务端 IP/IPv6/域名'), 'server_port': port(ask('frps 业务端口', '7000')), 'dashboard_port': port(ask('Dashboard/API 管理端口', '7500')), 'tls_enable': ask('启用 TLS？[Y/n]', 'y').lower() != 'n', 'admin_port': 7400}
    unique_name(p['name']); validate_profile(p)
    s = {'token': ask('token（不使用时留空）', secret=True), 'admin_password': secrets.token_urlsafe(32), 'dashboard_username': ask('Dashboard 用户名（留空关闭远程查询）')}
    s['dashboard_password'] = ask('Dashboard 密码', secret=True) if s['dashboard_username'] else ''
    p['dashboard_addr'] = ask('Dashboard 主机地址', p['server_addr']) if s['dashboard_username'] else ''
    if s['dashboard_username']:
        p['dashboard_scheme'] = ask('Dashboard 协议 http/https', 'https')
        if p['dashboard_scheme'] == 'http' and not confirm('HTTP 会明文传输管理凭据，请使用可信网络或隧道，仍继续？'): return
    if not confirm('确认创建 ' + label(p) + '？'): return
    pid = create_profile(p, s)
    if not active() or confirm('设为当前服务端？'): switch(pid)
    print('创建成功；端口映射请从主菜单第 9 项单独设置。')
    if active() == pid and not running('frpc') and confirm('立即启动 frpc 并检查连接？'):
        service('frpc', 'start'); wait_connection(pid)

def backup(paths, reason='change'):
    """Copy managed files into a timestamped directory, preserving paths relative to ROOT."""
    directory = ROOT / 'backups' / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + reason + '-' + secrets.token_hex(3))
    directory.mkdir(parents=True, mode=0o700)
    files = []
    for path in paths:
        path = Path(path)
        if not path.exists() or path.is_symlink(): continue
        try: relative = path.relative_to(ROOT)
        except ValueError: continue
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, target)
        files.append(str(relative))
    atomic(directory / 'manifest.json', json.dumps({'files': files, 'reason': reason}, ensure_ascii=False, indent=2) + '\n')
    return directory

def list_backups():
    """Return backup directories newest first, validated to live under ROOT/backups."""
    base = ROOT / 'backups'
    if not base.exists(): return []
    items = []
    for path in base.iterdir():
        if not path.is_dir() or path.is_symlink(): continue
        if path.resolve().parent != base.resolve(): continue
        manifest = path / 'manifest.json'
        info = {'path': path, 'time': path.name[:15], 'reason': 'unknown', 'files': []}
        try:
            if manifest.exists():
                data = read_json(manifest, {})
                info['reason'] = data.get('reason', 'unknown'); info['files'] = data.get('files', [])
        except (ValueError, OSError): pass
        items.append(info)
    return sorted(items, key=lambda x: x['path'].stat().st_mtime, reverse=True)

def restore_backup(directory):
    """Restore files listed in a validated backup manifest with atomic writes."""
    base = (ROOT / 'backups').resolve()
    directory = Path(directory)
    if directory.is_symlink() or directory.resolve().parent != base: raise ValueError('备份路径非法')
    manifest = directory / 'manifest.json'
    if not manifest.exists(): raise ValueError('备份缺少 manifest.json，拒绝恢复')
    files = read_json(manifest, {}).get('files') or []
    if not files: raise ValueError('备份清单为空')
    restored = []
    with lock(), transaction():
        for relative in files:
            relative = Path(relative)
            if relative.is_absolute() or '..' in relative.parts: raise ValueError('备份清单包含非法路径')
            source = directory / relative
            if not source.is_file() or source.is_symlink(): raise ValueError('备份文件缺失：' + str(relative))
            target = ROOT / relative
            if not str(target.resolve()).startswith(str(ROOT.resolve()) + '/'): raise ValueError('目标路径非法')
            atomic(target, source.read_text(encoding='utf-8'))
            restored.append(str(relative))
        rebuild_index(); audit('backup-restored')
    return restored

def backup_menu():
    items = list_backups()
    while True:
        clear_screen()
        print('\n[菜单] 配置备份与恢复')
        print('  ' + '─' * 38)
        if items:
            for n, item in enumerate(items[:20], 1):
                print(f'  {n}. {item["time"]}  {item["reason"]}  文件 {len(item["files"])} 个')
        else: print('  暂无备份')
        print('  0. 返回上一级')
        n = ask('请输入编号 > ')
        if not n.isdecimal(): result_error('请输入数字编号。'); continue
        n = int(n)
        if n == 0: return
        if not 1 <= n <= min(len(items), 20): result_error('编号超出范围。'); continue
        chosen = items[n-1]
        result_info('将恢复：' + str(chosen['path']))
        for relative in chosen['files'][:20]: print('    - ' + relative)
        if not confirm('确认用此备份覆盖当前配置？当前文件会先再次备份'):
            result_info('已取消恢复。'); continue
        try:
            backup(managed_paths(), 'pre-restore')
            restored = restore_backup(chosen['path'])
            result_ok(f'已恢复 {len(restored)} 个文件。')
            if running('frps'): result_info('frps 正在运行；请确认后重启以使用恢复的配置。')
        except (ValueError, OSError) as exc: result_error(str(exc))
        pause()

def diagnostics_report(role):
    """Machine-readable diagnostic snapshot; never includes secrets."""
    report = {'component': role, 'service_state': service(role, 'status').strip(), 'config_path': str(ROOT / (role + '.toml')), 'checks': []}
    config = ROOT / (role + '.toml')
    if not config.exists():
        report['checks'].append({'name': 'config', 'ok': False, 'detail': '配置不存在'})
        return report
    try:
        data = read_toml(config); verify(role, config.read_text(encoding='utf-8'))
        report['checks'].append({'name': 'config', 'ok': True, 'detail': '语法校验通过'})
    except (ValueError, OSError) as exc:
        report['checks'].append({'name': 'config', 'ok': False, 'detail': str(exc)}); return report
    if role == 'frps':
        web = data.get('webServer', {})
        if web.get('port'):
            host = web.get('addr', '127.0.0.1'); probe = '127.0.0.1' if host in ('0.0.0.0', '::') else host
            reachable = port_reachable(probe, web['port'])
            report['checks'].append({'name': 'dashboard-port', 'ok': reachable, 'detail': f'{probe}:{web["port"]}'})
            try:
                api(f'{api_scheme(web)}://{api_host(probe)}:{web["port"]}/api/serverinfo', web.get('user', ''), web.get('password', ''))
                report['checks'].append({'name': 'dashboard-auth', 'ok': True, 'detail': '认证成功'})
            except urllib.error.HTTPError as exc:
                report['checks'].append({'name': 'dashboard-auth', 'ok': False, 'detail': f'HTTP {exc.code}'})
            except (OSError, ValueError) as exc:
                report['checks'].append({'name': 'dashboard-auth', 'ok': False, 'detail': str(exc)})
        for key in ('bindPort', 'vhostHTTPPort', 'vhostHTTPSPort'):
            if data.get(key): report['checks'].append({'name': 'firewall-' + key, 'ok': not firewall_hint(data[key], 'tcp'), 'detail': '; '.join(firewall_hint(data[key], 'tcp'))})
    elif active():
        p = load_profile(active())
        report['active_profile'] = {'id': p['id'], 'name': p['name'], 'server': label(p)}
        reachable = port_reachable(p['server_addr'], p['server_port'])
        report['checks'].append({'name': 'server-port', 'ok': reachable, 'detail': f'{p["server_addr"]}:{p["server_port"]}'})
        result = connection(p['id'])
        report['checks'].append({'name': 'connection', 'ok': result['state'] == 'connected', 'detail': result['state']})
    report['ok'] = all(item['ok'] for item in report['checks'])
    return report

def diagnostics(role):
    """Produce an actionable connection diagnostic report without exposing secrets."""
    lines = []
    ok = True
    lines.append('[诊断] ' + ('frps 服务端' if role == 'frps' else 'frpc 客户端'))
    state_value = service(role, 'status').strip().lower()
    lines.append(f'  服务状态：{state_value or "未知"}')
    if state_value not in ('active', 'started', 'running'): ok = False; lines.append('  建议：先启动服务，再执行诊断。')
    config = ROOT / (role + '.toml')
    if not config.exists(): ok = False; lines.append('  配置：不存在，请先完成配置。')
    else:
        try:
            data = read_toml(config); verify(role, config.read_text(encoding='utf-8'))
            lines.append('  配置：语法校验通过。')
        except (ValueError, OSError) as exc:
            ok = False; lines.append('  配置：校验失败 — ' + str(exc)); data = {}
        if role == 'frps' and data:
            web = data.get('webServer', {})
            if web.get('port'):
                host = web.get('addr', '127.0.0.1')
                probe = '127.0.0.1' if host in ('0.0.0.0', '::') else host
                reachable = port_reachable(probe, web['port'])
                lines.append(f'  Dashboard 端口 {probe}:{web["port"]}：{"可达" if reachable else "不可达"}')
                if not reachable: ok = False; lines.append('  建议：确认 frps 正在运行且 Dashboard 监听地址正确。')
                try:
                    api(f'{api_scheme(web)}://{api_host(probe)}:{web["port"]}/api/serverinfo', web.get('user', ''), web.get('password', ''))
                    lines.append('  Dashboard 认证：成功')
                except urllib.error.HTTPError as exc:
                    ok = False; lines.append(f'  Dashboard 认证：失败（HTTP {exc.code}），请检查用户名和密码。')
                except (OSError, ValueError) as exc:
                    ok = False; lines.append('  Dashboard API：无法访问 — ' + str(exc))
            for key, proto in (('bindPort', 'tcp'), ('vhostHTTPPort', 'tcp'), ('vhostHTTPSPort', 'tcp')):
                if data.get(key):
                    hints = firewall_hint(data[key], proto)
                    if hints: ok = False; lines.extend('  ' + hint for hint in hints)
        if role == 'frpc' and data and active():
            pid = active(); p = load_profile(pid)
            lines.append('  当前服务端：' + label(p))
            reachable = port_reachable(p['server_addr'], p['server_port'])
            lines.append(f'  服务端端口 {p["server_addr"]}:{p["server_port"]}：{"可达" if reachable else "不可达"}')
            if not reachable: ok = False; lines.append('  建议：检查服务端防火墙、安全组和地址端口。')
            result = connection(pid)
            lines.append('  连接状态：' + result['state'])
            if result.get('detail'): lines.append('  详情：' + result['detail'])
            if result['state'] not in ('connected',): ok = False
            if state().get('pending'): lines.append('  注意：存在尚未重启生效的配置修改。')
            for m in mappings(pid):
                if m.get('type') in ('tcp', 'udp') and m.get('remote_port'):
                    if check_port_conflict('0.0.0.0', m['remote_port']): lines.append(f'  注意：远程端口 {m["remote_port"]} 在本机被占用（若服务端与本机不同可忽略）。')
    print('\n' + '\n'.join(lines))
    print('  ' + '─' * 38)
    if ok: result_ok('诊断通过。')
    else: result_error('诊断发现问题，请按建议处理。')
    return ok

def port_reachable(host, port_value, timeout=3):
    try:
        with socket.create_connection((host, port_value), timeout=timeout): return True
    except OSError: return False

def api_scheme(web): return 'https' if web.get('tls', {}).get('certFile') else 'http'
def api_host(host): return '[' + host + ']' if ':' in host else host

def save_mappings(pid, items):
    if pid != require_active(): raise ValueError('只能管理当前 profile 的映射')
    with lock(), transaction():
        if pid != active(): raise ValueError('当前服务端已在另一终端切换，请重新打开映射菜单')
        for m in items: validate_mapping(m)
        path = pdir(pid) / 'mappings.toml'; old = path.read_text(encoding='utf-8')
        backup([path]); atomic(path, toml({'schema_version': 1, 'mappings': items}))
        try: mappings(pid); apply(pid)
        except Exception: atomic(path, old); raise
        s = state(); s['pending'] = True; save_json(ROOT / 'state.json', s); audit('mappings-saved', pid)
    result_ok('端口映射配置已保存。' + ('当前 frpc 运行中，请按提示重启后生效。' if running('frpc') else '当前 frpc 未运行，下次启动时生效。'))
    if running('frpc') and confirm('配置已保存，立即重启 frpc 生效？'):
        service('frpc', 'restart'); wait_connection(pid)
        s = state(); s['pending'] = False; save_json(ROOT / 'state.json', s)

def mapping_wizard(old=None):
    m = dict(old or {})
    m['name'] = ask('映射名称', m.get('name'))
    m['type'] = ask('类型 tcp/udp/http/https', m.get('type', 'tcp'))
    m['local_ip'] = ask('本地地址', m.get('local_ip', '127.0.0.1'))
    m['local_port'] = port(ask('本地端口', str(m.get('local_port', ''))))
    if m['type'] in ('tcp', 'udp'): m['remote_port'] = port(ask('远程端口', str(m.get('remote_port', '')))); m.pop('domain', None)
    else: m['domain'] = ask('域名', m.get('domain')); m.pop('remote_port', None)
    m['enabled'] = ask('启用？[Y/n]', 'y' if m.get('enabled', True) else 'n').lower() != 'n'
    validate_mapping(m); return m

def show_mappings(items):
    if not items:
        print('当前服务端暂无端口映射。'); return
    print(f'{"序号":<4}{"名称":<16}{"类型":<6}{"本地":<22}{"远端":<20}{"状态"}')
    for n, m in enumerate(items, 1):
        local = f'{m.get("local_ip", "127.0.0.1")}:{m["local_port"]}'
        if m['type'] in ('tcp', 'udp'): remote = str(m.get('remote_port', '-'))
        else: remote = m.get('domain', '-')
        enabled = '启用' if m.get('enabled', True) else '停用'
        print(f'{n:<4}{m["name"]:<16}{m["type"]:<6}{local:<22}{remote:<20}{enabled}')

def mapping_menu():
    while True:
        clear_screen()
        pid = require_active(); print('\n当前 frps：' + label(load_profile(pid)))
        n = choose(['新增端口映射', '查看当前服务端映射', '修改端口映射', '启用/停用端口映射', '删除端口映射'])
        if not n: return
        try:
            items = mappings(pid)
            if n == 1: items.append(mapping_wizard()); save_mappings(pid, items)
            elif n == 2: show_mappings(items)
            else:
                index = choose([m['name'] + (' [启用]' if m.get('enabled', True) else ' [停用]') for m in items], '选择映射')
                if not index: continue
                if n == 3: items[index-1] = mapping_wizard(items[index-1])
                elif n == 4: items[index-1]['enabled'] = not items[index-1].get('enabled', True)
                elif not confirm('确认删除映射 ' + items[index-1]['name'] + '？'): result_info('已取消删除。'); continue
                else: items.pop(index-1)
                save_mappings(pid, items); result_ok('端口映射操作已完成。')
                pause()
        except (ValueError, OSError, KeyError) as e: result_error(str(e)); pause()

def profile_summary(p, s):
    """Readable, secret-free view of the active profile."""
    web_user = s.get('dashboard_username', '')
    print('\n  服务端名称：' + p['name'])
    print('  服务端地址：' + p['server_addr'])
    print('  业务端口：' + str(p['server_port']))
    print('  Dashboard：' + (('已启用，端口 ' + str(p.get('dashboard_port', 7500))) if web_user else '未启用'))
    print('  TLS：' + ('已启用' if p.get('tls_enable', True) else '未启用'))
    print('  token：' + ('已设置' if s.get('token') else '未设置'))
    print('  本地管理端口：' + str(p.get('admin_port', 7400)))
    print('  映射数量：' + str(len(mappings(p['id']))))

def edit_profile():
    clear_screen()
    pid = require_active(); p = load_profile(pid); s = secrets_for(pid)
    version = hashlib.sha256((pdir(pid) / 'profile.toml').read_bytes() + (pdir(pid) / 'secrets.toml').read_bytes()).hexdigest()
    while True:
        clear_screen()
        n = choose(['查看连接配置', '修改服务端名称', '修改服务器地址', '修改服务器端口', '修改 token', '修改 TLS 配置', '修改 Dashboard/API', '修改传输协议', '修改日志级别与保留天数', '保存并返回', '删除当前服务端及全部映射'], label(p))
        if n == 0: return
        if n == 1: profile_summary(p, s)
        elif n == 2: v = ask('新名称'); unique_name(v, pid); p['name'] = v
        elif n == 3: p['server_addr'] = address(ask('新地址'))
        elif n == 4: p['server_port'] = port(ask('业务端口'))
        elif n == 5: s['token'] = ask('新 token（留空清除）', secret=True)
        elif n == 6:
            p['tls_enable'] = ask('启用 TLS？[Y/n]', 'y').lower() != 'n'
            for key in ('certFile', 'keyFile', 'trustedCaFile'): s['tls_' + key] = ask('可选 ' + key + ' 路径', s.get('tls_' + key, ''))
        elif n == 7:
            p['dashboard_port'] = port(ask('Dashboard/API 端口', '7500')); p['dashboard_addr'] = ask('Dashboard 主机', p['server_addr'])
            s['dashboard_username'] = ask('Dashboard 用户名（留空关闭）'); s['dashboard_password'] = ask('密码', secret=True) if s['dashboard_username'] else ''
        elif n == 8:
            protocol = ask('传输协议 tcp/kcp/quic/websocket/wss', p.get('transport_protocol', 'tcp')).lower()
            if protocol not in ('tcp', 'kcp', 'quic', 'websocket', 'wss'): raise ValueError('不支持的传输协议：' + protocol)
            p['transport_protocol'] = protocol
            p['transport_use_encryption'] = ask('启用传输加密？[y/N]', 'y' if p.get('transport_use_encryption') else 'n').lower() == 'y'
            p['transport_use_compression'] = ask('启用传输压缩？[y/N]', 'y' if p.get('transport_use_compression') else 'n').lower() == 'y'
        elif n == 9:
            level = ask('日志级别 trace/debug/info/warn/error', p.get('log_level', 'info')).lower()
            if level not in ('trace', 'debug', 'info', 'warn', 'error'): raise ValueError('不支持的日志级别：' + level)
            p['log_level'] = level
            days = ask('日志保留天数（1-365）', str(p.get('log_max_days', 30)))
            if not days.isdigit() or not 1 <= int(days) <= 365: raise ValueError('日志保留天数必须为 1-365')
            p['log_max_days'] = int(days)
        elif n == 11:
            if confirm('删除此服务端及全部映射？不额外备份。'):
                with lock(), transaction():
                    if running('frpc'): service('frpc', 'stop')
                    folder = pdir(pid)
                    if folder.parent != ROOT / 'profiles': raise ValueError('删除路径错误')
                    shutil.rmtree(folder); set_current(''); (unlink_quiet(ROOT / 'frpc.toml')); rebuild_index()
                return
        else:
            validate_profile(p); unique_name(p['name'], pid)
            with lock(), transaction():
                if active() != pid: raise ValueError('当前服务端已改变，请重新进入配置菜单')
                current_version = hashlib.sha256((pdir(pid) / 'profile.toml').read_bytes() + (pdir(pid) / 'secrets.toml').read_bytes()).hexdigest()
                if current_version != version: raise ValueError('配置已被其他终端修改，请重新进入编辑')
                paths = [pdir(pid) / 'profile.toml', pdir(pid) / 'secrets.toml']; old = [x.read_text(encoding='utf-8') for x in paths]; backup(paths)
                try:
                    atomic(paths[0], toml(p)); atomic(paths[1], toml(s)); apply(pid); rebuild_index()
                except Exception:
                    for path, value in zip(paths, old): atomic(path, value)
                    raise
            if running('frpc') and confirm('立即重启生效？'): service('frpc', 'restart'); wait_connection(pid)
            return

def configure_server():
    data = {'bindAddr': address(ask('业务监听地址', '0.0.0.0')), 'bindPort': port(ask('业务端口', '7000')),
            'auth.method': 'token', 'auth.token': ask('token（留空不认证）', secret=True),
            'log.to': str(ROOT / 'logs' / 'frps.log'), 'log.maxDays': 30, 'log.disablePrintColor': True}
    if not data['auth.token'] and not confirm('未设置 token，确认允许无 token 认证？'): return
    user = ask('Dashboard 用户名（留空关闭）')
    if user:
        host = address(ask('Dashboard 监听地址', '127.0.0.1'))
        if host not in ('127.0.0.1', '::1') and not confirm('管理接口将对外监听，确认？'): return
        password = ask('Dashboard 密码', secret=True)
        if not password: raise ValueError('启用 Dashboard 时密码不能为空')
        data.update({'webServer.addr': host, 'webServer.port': port(ask('Dashboard 端口', '7500')), 'webServer.user': user, 'webServer.password': password})
    for key in ('vhostHTTPPort', 'vhostHTTPSPort'):
        value = ask(key + '（留空禁用）')
        if value: data[key] = port(value)
    content = toml(data); verify('frps', content)
    with lock(), transaction(): backup([ROOT / 'frps.toml']); atomic(ROOT / 'frps.toml', content); mark_hash('frps')
    if running('frps') and confirm('立即重启 frps？'):
        service_feedback('frps', 'restart')
    else:
        print('frps 配置已保存；当前运行服务将在下次重启后使用新配置。')

def check_port_conflict(host, port_value, exclude_pid=None):
    """Return True when another local process already listens on host:port."""
    if not shutil.which('ss'): return None
    result = run_cmd(['ss', '-lntuH'], capture_output=True, text=True, timeout=10)
    needle = f':{port_value}'
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5: continue
        local = fields[4]
        if not local.endswith(needle) and local != str(port_value): continue
        if host not in ('0.0.0.0', '::', '') and not local.startswith(host): continue
        if exclude_pid and f'pid={exclude_pid}' in line: continue
        return True
    return False

def firewall_hint(port_value, proto='tcp'):
    """Detect common firewall tools and return actionable hints; never modify rules."""
    hints = []
    if shutil.which('ufw'):
        result = run_cmd(['ufw', 'status'], capture_output=True, text=True, timeout=10)
        if 'Status: active' in result.stdout and str(port_value) not in result.stdout:
            hints.append(f'ufw 已启用，但未见 {port_value}/{proto} 放行规则')
    if shutil.which('firewall-cmd'):
        result = run_cmd(['firewall-cmd', '--state'], capture_output=True, text=True, timeout=10)
        if result.stdout.strip() == 'running':
            check = run_cmd(['firewall-cmd', f'--query-port={port_value}/{proto}'], capture_output=True, text=True, timeout=10)
            if check.returncode != 0: hints.append(f'firewalld 正在运行，{port_value}/{proto} 未放行')
    if os.name == 'nt': hints.append('Windows 防火墙请在“高级安全防火墙”中确认入站规则')
    return hints

def validate_frps_config(data):
    """Validate the subset of frps settings the manager itself can write."""
    if not isinstance(data.get('bindPort'), int): raise ValueError('bindPort 必须是整数')
    port(data['bindPort'])
    if data.get('bindAddr') is not None: address(data['bindAddr'])
    auth = data.get('auth', {})
    if auth.get('method', 'token') != 'token': raise ValueError('首期仅支持 token 认证')
    token_value = auth.get('token', '')
    if token_value and len(token_value) < 8: raise ValueError('token 长度至少 8 个字符')
    web = data.get('webServer', {})
    if web.get('port') is not None:
        port(web['port'])
        if not web.get('user') or not web.get('password'): raise ValueError('启用 Dashboard 时必须同时设置用户名和密码')
        address(web.get('addr', '127.0.0.1'))
    for key in ('vhostHTTPPort', 'vhostHTTPSPort', 'kcpBindPort', 'quicBindPort', 'tcpmuxHTTPConnectPort'):
        if data.get(key) is not None: port(data[key])
    return True

def read_frps_config():
    path = ROOT / 'frps.toml'
    if not path.exists(): raise ValueError('frps 尚未配置，请先执行“修改服务端配置”')
    return read_toml(path)

def save_frps_config(data, restart=True):
    validate_frps_config(data)
    content = toml(data); verify('frps', content)
    with lock(), transaction():
        backup([ROOT / 'frps.toml'], 'frps-change')
        atomic(ROOT / 'frps.toml', content); mark_hash('frps')
    if restart and running('frps') and confirm('配置已保存，立即重启 frps 生效？'):
        service_feedback('frps', 'restart')
        if not running('frps'):
            raise ValueError('frps 重启失败；已保留新配置，请查看日志并恢复备份')
        result_ok('frps 配置已保存并生效。')
    else:
        result_ok('frps 配置已保存；下次重启后生效。')

def frps_config_menu():
    while True:
        clear_screen()
        try: data = read_frps_config()
        except (ValueError, OSError) as exc: result_error(str(exc)); pause(); return
        web = data.get('webServer', {})
        print('\n[菜单] frps 服务端配置')
        print('  ' + '─' * 38)
        print('  1. 查看当前配置（脱敏）')
        print('  2. 修改业务监听地址/端口')
        print('  3. 修改 token')
        print('  4. 修改 Dashboard/API')
        print('  5. 修改 HTTP/HTTPS vhost 端口')
        print('  6. 修改 KCP/QUIC/TCPMUX 端口')
        print('  7. 校验配置并检测端口冲突')
        print('  8. 查看防火墙放行提示')
        print('  9. 备份与恢复配置')
        print('  0. 返回上一级')
        n = ask('请输入编号 > ')
        if not n.isdecimal(): result_error('请输入数字编号。'); continue
        n = int(n)
        if n == 0: return
        try:
            if n == 1:
                redacted = json.loads(json.dumps(data))
                if redacted.get('auth', {}).get('token'): redacted['auth']['token'] = '******'
                if redacted.get('webServer', {}).get('password'): redacted['webServer']['password'] = '******'
                print(json.dumps(redacted, ensure_ascii=False, indent=2))
            elif n == 2:
                data['bindAddr'] = address(ask('业务监听地址', data.get('bindAddr', '0.0.0.0')))
                data['bindPort'] = port(ask('业务端口', str(data.get('bindPort', 7000))))
                save_frps_config(data)
            elif n == 3:
                value = ask('新 token（留空表示不认证）', secret=True)
                if not value and not confirm('未设置 token，确认允许无 token 认证？'): continue
                data.setdefault('auth', {})['method'] = 'token'; data['auth']['token'] = value
                save_frps_config(data)
            elif n == 4:
                user = ask('Dashboard 用户名（留空关闭 Dashboard）')
                if not user:
                    data.pop('webServer', None)
                else:
                    host = address(ask('Dashboard 监听地址', web.get('addr', '127.0.0.1')))
                    if host not in ('127.0.0.1', '::1') and not confirm('管理接口将对外监听，确认？'): continue
                    password = ask('Dashboard 密码（留空保留原密码）', secret=True) or web.get('password')
                    if not password: raise ValueError('Dashboard 密码不能为空')
                    data['webServer'] = {'addr': host, 'port': port(ask('Dashboard 端口', str(web.get('port', 7500)))), 'user': user, 'password': password}
                save_frps_config(data)
            elif n == 5:
                for key in ('vhostHTTPPort', 'vhostHTTPSPort'):
                    value = ask(key + '（留空禁用）', str(data.get(key, '')))
                    if value: data[key] = port(value)
                    else: data.pop(key, None)
                save_frps_config(data)
            elif n == 6:
                for key in ('kcpBindPort', 'quicBindPort', 'tcpmuxHTTPConnectPort'):
                    value = ask(key + '（留空禁用）', str(data.get(key, '')))
                    if value: data[key] = port(value)
                    else: data.pop(key, None)
                save_frps_config(data)
            elif n == 7:
                validate_frps_config(data); verify('frps', toml(data))
                result_ok('配置语法和字段校验通过。')
                for key in ('bindPort', 'vhostHTTPPort', 'vhostHTTPSPort', 'kcpBindPort', 'quicBindPort', 'tcpmuxHTTPConnectPort'):
                    if data.get(key) and check_port_conflict('0.0.0.0', data[key]): result_error(f'{key}={data[key]} 已被其他进程监听。')
                if web.get('port') and check_port_conflict('127.0.0.1', web['port']): result_error(f'Dashboard 端口 {web["port"]} 已被占用。')
            elif n == 8:
                ports = [('bindPort', 'tcp')] + [('vhostHTTPPort', 'tcp'), ('vhostHTTPSPort', 'tcp')]
                found = False
                for key, proto in ports:
                    if data.get(key):
                        for hint in firewall_hint(data[key], proto): result_info(hint); found = True
                if not found: result_info('未发现需要处理的常见防火墙规则提示；云安全组请自行确认。')
            elif n == 9: backup_menu()
        except (ValueError, OSError, subprocess.SubprocessError) as exc: result_error(str(exc))
        if n not in (9,): pause()

def server_clients():
    p = read_toml(ROOT / 'frps.toml'); web = p.get('webServer', {})
    if not web.get('port'): raise ValueError('Dashboard/API 未启用，请先配置')
    host = web.get('addr', '127.0.0.1')
    if host in ('0.0.0.0', '::'): host = '127.0.0.1' if host == '0.0.0.0' else '::1'
    if ':' in host: host = '[' + host + ']'
    scheme = 'https' if web.get('tls', {}).get('certFile') else 'http'
    def paged(endpoint):
        page, rows = 1, []
        while True:
            data = api(f'{scheme}://{host}:{web["port"]}/api/v2/{endpoint}?page={page}&pageSize=200', web.get('user', ''), web.get('password', ''))
            rows.extend(data.get('items', []))
            if page * 200 >= data.get('total', 0): return rows
            page += 1
    clients, proxies = paged('clients'), paged('proxies')
    print('\n在线客户端（Dashboard/API）')
    if not clients: print('暂无客户端连接。')
    for n, item in enumerate(clients, 1):
        state = '在线' if item.get('online') else '离线'
        print(f'{n}. {item.get("hostname") or item.get("clientID") or item.get("key", "未知")} [{state}]')
        print(f'   clientID={item.get("clientID", "")} 版本={item.get("version", "")} IP={item.get("clientIP", "")}')
        print(f'   代理数量={item.get("status", {}).get("proxyCount", "未知")}')
    print('\n代理状态')
    if not proxies: print('暂无代理统计。')
    for n, item in enumerate(proxies, 1):
        status = item.get('status', {})
        print(f'{n}. {item.get("name", "未知")} [{status.get("phase", "未知")}] 类型={item.get("spec", {}).get("type", "未知")}')
        print(f'   今日流量：入站 {status.get("todayTrafficIn", 0)} / 出站 {status.get("todayTrafficOut", 0)}，连接数 {status.get("curConns", 0)}')
    print(f'客户端连接查询完成：{len(clients)} 个客户端，{len(proxies)} 个代理。')

def import_external(role):
    path = ROOT / (role + '.toml'); s = state()
    if not path.exists(): return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if s.get(role + '_hash') == digest: return
    print('检测到外部运行配置修改。')
    n = choose(['导入外部修改', '保留管理器配置', '查看变更字段（隐藏敏感值）'], '配置同步')
    if n == 0: raise ValueError('已取消；外部文件保持原样')
    config = read_toml(path) if n != 2 else {}
    if n == 3:
        print('字段：' + ', '.join(config)); return import_external(role)
    if role == 'frps':
        with lock(), transaction():
            if n == 2:
                canonical = ROOT / '.canonical-frps.toml'
                if not canonical.exists(): raise ValueError('没有可恢复的管理器配置，请选择导入或恢复备份')
                if not confirm('确认恢复上次已接受的服务端配置？'): return
                atomic(path, canonical.read_text(encoding='utf-8'))
            verify(role, path.read_text(encoding='utf-8-sig')); mark_hash(role)
        return
    pid = require_active()
    if n == 2:
        if confirm('确认覆盖外部运行配置？'): apply(pid)
        return
    # Never flatten include/store/template or advanced fields: explicit conflicts.
    permitted = {'serverAddr', 'serverPort', 'clientID', 'transport', 'auth', 'webServer', 'log', 'proxies', 'loginFailExit'}
    if set(config) - permitted or 'includes' in config or 'store' in config: raise ValueError('包含未支持字段/includes/store，不能自动导入；原文件保留')
    if config.get('clientID', pid) != pid: raise ValueError('来源 profile 不一致，拒绝跨服务端导入')
    p = load_profile(pid); sec = secrets_for(pid); rows = []
    canonical = _load_toml(render(pid))
    external_log = config.get('log', {})
    canonical_log = canonical.get('log', {})
    if set(external_log) - {'to', 'level', 'maxDays', 'disablePrintColor'}:
        raise ValueError('日志高级字段无法无损导入，原文件保留')
    if external_log.get('to', canonical_log.get('to')) != canonical_log.get('to') or external_log.get('disablePrintColor', True) is not True:
        raise ValueError('日志路径/颜色变更需手工协调，原文件保留')
    if config.get('loginFailExit', False) is not False:
        raise ValueError('登录失败退出策略变更尚不支持反向导入，原文件保留')
    transport = config.get('transport', {})
    if set(transport) - {'tls', 'protocol', 'useEncryption', 'useCompression'} or set(transport.get('tls', {})) - {'enable', 'certFile', 'keyFile', 'trustedCaFile', 'useSystemCa'}:
        raise ValueError('高级传输字段无法无损导入，原文件保留')
    if transport.get('protocol', 'tcp') not in ('tcp', 'kcp', 'quic', 'websocket', 'wss'):
        raise ValueError('不支持的传输协议：' + str(transport.get('protocol')))
    p_transport = {'transport_protocol': transport.get('protocol', 'tcp'),
                   'transport_use_encryption': bool(transport.get('useEncryption', False)),
                   'transport_use_compression': bool(transport.get('useCompression', False)),
                   'tls_use_system_ca': bool(transport.get('tls', {}).get('useSystemCa', False)),
                   'log_level': external_log.get('level', canonical_log.get('level', 'info')),
                   'log_max_days': int(external_log.get('maxDays', canonical_log.get('maxDays', 30)))}
    for key in ('certFile', 'keyFile', 'trustedCaFile'): sec['tls_' + key] = transport.get('tls', {}).get(key, '')
    web = config.get('webServer', {})
    if set(web) - {'addr', 'port', 'user', 'password'} or web.get('addr', '127.0.0.1') != '127.0.0.1' or web.get('user', 'frp-simple-bash') != 'frp-simple-bash':
        raise ValueError('客户端 Admin 地址/用户变更需手工协调，未自动覆盖')
    p['admin_port'] = port(web.get('port', 7400))
    if web.get('password'): sec['admin_password'] = web['password']
    for x in config.get('proxies', []):
        if set(x) - {'name', 'type', 'localIP', 'localPort', 'remotePort', 'customDomains', 'enabled'}: raise ValueError('高级映射字段无法无损导入，原文件保留')
        m = {'name': x['name'], 'type': x['type'], 'local_ip': x.get('localIP', '127.0.0.1'), 'local_port': x['localPort'], 'enabled': x.get('enabled', True)}
        if m['type'] in ('tcp', 'udp'): m['remote_port'] = x['remotePort']
        else:
            if len(x.get('customDomains', [])) != 1: raise ValueError('首期仅支持单域名映射导入')
            m['domain'] = x['customDomains'][0]
        validate_mapping(m); rows.append(m)
    p['server_addr'] = address(config['serverAddr']); p['server_port'] = port(config['serverPort'])
    p['tls_enable'] = config.get('transport', {}).get('tls', {}).get('enable', True)
    p.update(p_transport)
    auth = config.get('auth', {})
    if set(auth) - {'method', 'token'} or auth.get('method', 'token') != 'token': raise ValueError('认证方式不支持自动导入')
    sec['token'] = auth.get('token', '')
    # Preserve disabled mappings absent from generated runtime config.
    row_names = {m['name'] for m in rows}
    rows += [m for m in mappings(pid) if not m.get('enabled', True) and m['name'] not in row_names]
    with lock(), transaction():
        paths = [pdir(pid) / x for x in ('profile.toml', 'secrets.toml', 'mappings.toml')]; old = [x.read_text(encoding='utf-8') for x in paths]
        backup(paths)
        try:
            for dest, obj in zip(paths, (p, sec, {'schema_version': 1, 'mappings': rows})): atomic(dest, toml(obj))
            mappings(pid); apply(pid); rebuild_index()
        except Exception:
            for dest, value in zip(paths, old): atomic(dest, value)
            raise
    print('外部修改已导入当前 profile。')

def status(role):
    result = {'component': role, 'version': VERSION, 'service_state': service(role, 'status'), 'config_path': str(ROOT / (role + '.toml'))}
    if role == 'frpc' and active(): result.update({'active_profile': load_profile(active()), 'connection': connection(active()), 'pending': state().get('pending', False)})
    return result

def status_bar(role, data):
    """One-line compact status shown above every menu."""
    states = {'active': '运行中', 'running': '运行中', 'started': '运行中', 'stopped': '已停止', 'inactive': '已停止', 'failed': '异常', 'unknown': '未知'}
    state_text = states.get(str(data.get('service_state', '')).lower(), data.get('service_state', '未知'))
    parts = [role, state_text, str(data.get('version', ''))]
    if role == 'frps':
        try:
            config = read_toml(ROOT / 'frps.toml')
            parts.append('监听 ' + str(config.get('bindAddr', '0.0.0.0')) + ':' + str(config.get('bindPort', 7000)))
            web = config.get('webServer', {})
            if web.get('port'):
                parts.append('Dashboard ' + str(web.get('addr', '127.0.0.1')) + ':' + str(web['port']))
        except (ValueError, OSError):
            parts.append('配置未就绪')
    else:
        profile = data.get('active_profile')
        if profile:
            translations = {'connected': '已连接', 'connecting': '连接中', 'stopped': '已停止', 'unknown': '待确认', 'proxy-error': '映射异常', 'auth-failed': '认证失败', 'tls-failed': 'TLS 失败'}
            conn = data.get('connection', {}).get('state', '')
            parts += [label(profile), translations.get(conn, conn), str(len(mappings(profile['id']))) + ' 个映射']
        else:
            parts.append('未选择服务端')
    if data.get('pending'):
        parts.append('有待重启修改')
    print('  ' + ' | '.join(str(p) for p in parts if p))

def show_status(role):
    data = status(role)
    states = {'active': '运行中', 'running': '运行中', 'started': '运行中', 'stopped': '已停止', 'inactive': '已停止', 'failed': '异常', 'unknown': '未知', 'activating': '启动中'}
    section('frps 服务端管理' if role == 'frps' else 'frpc 客户端管理')
    print('  ' + '状态'.ljust(10) + states.get(data['service_state'].lower(), data['service_state']))
    print('  ' + '版本'.ljust(10) + data['version'])
    print('  ' + '配置文件'.ljust(10) + data['config_path'])
    if role == 'frps':
        path = ROOT / 'frps.toml'
        if path.exists():
            config = read_toml(path)
            host = config.get('bindAddr', '0.0.0.0')
            if ':' in host: host = '[' + host + ']'
            print('  ' + '监听'.ljust(10) + str(host) + ':' + str(config.get('bindPort', 7000)))
            web = config.get('webServer', {})
            if web.get('port'):
                addr = web.get('addr', '127.0.0.1')
                if ':' in addr: addr = '[' + addr + ']'
                scheme = 'https' if web.get('tls', {}).get('certFile') else 'http'
                print('  ' + 'Dashboard'.ljust(10) + scheme + '://' + str(addr) + ':' + str(web['port']))
                if web.get('addr', '127.0.0.1') in ('127.0.0.1', '::1'):
                    print('  ' + '管理访问'.ljust(10) + '仅本机')
            else: print('  ' + 'Dashboard'.ljust(10) + '未启用')
        else: print('  ' + '状态说明'.ljust(10) + '尚未生成配置，请先修改服务端配置')
    else:
        p = data.get('active_profile')
        print('  ' + '服务端'.ljust(10) + (label(p) if p else '未选择'))
        if p:
            translations = {'connected': '已连接', 'connecting': '连接中', 'stopped': '已停止', 'unknown': '待确认', 'proxy-error': '映射异常', 'auth-failed': '认证失败', 'tls-failed': 'TLS 失败', 'timeout': '连接超时'}
            connection_data = data['connection']
            print('  ' + '连接状态'.ljust(10) + translations.get(connection_data['state'], connection_data['state']))
            print('  ' + '映射数量'.ljust(10) + str(len(mappings(p['id']))))
            if data.get('pending'): print('  ' + '生效'.ljust(10) + '有修改等待重启')
    print(RULE)
    return data

def service_feedback(role, action):
    labels = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    state_value = service(role, 'status').strip().lower()
    expected = {'start': ('active', 'started', 'running'), 'stop': ('inactive', 'stopped', 'dead'), 'restart': ('active', 'started', 'running')}[action]
    if state_value in expected:
        result_ok(f'{labels[action]}成功：当前状态为 {"运行中" if action != "stop" else "已停止"}。')
    else:
        result_error(f'{labels[action]}请求已发送，但当前服务状态为：{state_value or "未知"}。请查看日志。')
    return state_value

def main_menu(role):
    while True:
        clear_screen()
        try:
            data = status(role)
            alias = data.get('active_profile', {}).get('name', '未选择')
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
            print('配置无法读取；请检查配置文件或从 backups 恢复，服务管理仍可使用。')
            alias = '配置错误'
        if role == 'frpc':
            items = ['查看当前状态', '启动 frpc', '停止 frpc', '重启 frpc', '新建 frps 服务端', '查看服务端列表', '切换当前 frps 服务端', '修改当前 frps 配置', f'修改当前 frps（{alias}）端口映射', '查看日志与连接诊断', '备份与恢复配置', '设置/取消开机自启动', '卸载 frpc', '升级 frpc']
        else: items = ['查看服务信息（状态、版本、路径）', '启动 frps', '停止 frps', '重启 frps', '查看客户端连接信息', '修改服务端配置', '查看日志', '连接诊断', '备份与恢复配置', '设置/取消开机自启动', '卸载 frps', '升级 frps']
        status_bar(role, data)
        n = choose(items, '请选择操作' if role == 'frps' else 'frpc 客户端操作', '退出')
        if not n: return
        try:
            if role == 'frpc':
                if n == 1: show_status(role)
                elif n in (2, 3, 4):
                    require_active()
                    if n == 2 or confirm(items[n-1] + '？'):
                        action = {2:'start', 3:'stop', 4:'restart'}[n]
                        service_feedback(role, action)
                        if n != 3: wait_connection(active())
                        else: print('frpc 已停止，当前配置和 profile 保留。')
                elif n == 5: profile_wizard()
                elif n == 6: select_profile()
                elif n == 7:
                    pid = select_profile()
                    if pid: switch(pid)
                elif n == 8: edit_profile()
                elif n == 9: mapping_menu()
                elif n == 10:
                    logs_menu(role); diagnostics(role)
                elif n == 11: backup_menu()
                elif n == 12: autostart(role)
                elif n == 13: uninstall(role)
                elif n == 14: upgrade(role)
            else:
                if n == 1:
                    result_info('正在刷新服务信息。')
                    show_status(role)
                elif n in (2,3,4):
                    if n == 2 or confirm(items[n-1] + '？'):
                        service_feedback(role, {2:'start',3:'stop',4:'restart'}[n])
                elif n == 5: server_clients()
                elif n == 6: frps_config_menu()
                elif n == 7: logs_menu(role)
                elif n == 8: diagnostics(role)
                elif n == 9: backup_menu()
                elif n == 10: autostart(role)
                elif n == 11: uninstall(role)
                elif n == 12: upgrade(role)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as e: result_error(str(e))
        if n != 1:
            pause()

def log_files(role):
    base = ROOT / 'logs'
    if not base.exists(): return []
    return sorted([p for p in base.iterdir() if p.is_file() and not p.is_symlink() and (p.name == role + '.log' or p.name.startswith(role + '.log.'))], key=lambda p: p.stat().st_mtime, reverse=True)

def show_logs(role, lines=50):
    path = ROOT / 'logs' / (role + '.log')
    if path.exists():
        content = path.read_text(encoding='utf-8', errors='replace').splitlines()
        print('\n'.join(content[-lines:]))
        print(f'[日志] {path} 共 {len(content)} 行，已显示最近 {min(lines, len(content))} 行。')
        return
    if os.name != 'nt' and shutil.which('journalctl'):
        run_cmd(['journalctl', '-u', role, '-n', str(lines), '--no-pager'])
        print(f'[日志] 已从 journalctl 读取最近 {lines} 行。')
        return
    print('暂无文件日志；请查看平台服务日志。')

def logs_menu(role):
    while True:
        clear_screen()
        files = log_files(role)
        print('\n[菜单] 日志管理')
        print('  ' + '─' * 38)
        if files:
            for n, item in enumerate(files[:10], 1):
                size = item.stat().st_size
                print(f'  {n}. {item.name}  {size} 字节')
        else: print('  暂无日志文件')
        print('  1. 查看最近 50 行')
        print('  2. 查看最近 200 行')
        print('  3. 清理轮转归档（保留当前日志）')
        print('  4. 查看日志目录')
        print('  0. 返回上一级')
        n = ask('请输入编号 > ')
        if not n.isdecimal(): result_error('请输入数字编号。'); continue
        n = int(n)
        if n == 0: return
        if n == 1: show_logs(role, 50); pause()
        elif n == 2: show_logs(role, 200); pause()
        elif n == 3:
            removed = 0
            for item in files:
                if item.name == role + '.log': continue
                unlink_quiet(item); removed += 1
            result_ok(f'已清理 {removed} 个轮转归档，当前日志保留。'); pause()
        elif n == 4:
            print(f'日志目录：{ROOT / "logs"}')
            print('轮转策略：单文件超过 50MB 自动归档；保留 30 天；总量上限 500MB。'); pause()
        else: result_error('编号超出范围。')

def autostart(role):
    n = choose(['启用开机自启动', '取消开机自启动'])
    if n:
        action = 'enable' if n == 1 else 'disable'
        service(role, action)
        current = service(role, 'status')
        print(('已启用' if n == 1 else '已取消') + f' {role} 开机自启动；当前运行状态：{current or "未知"}。')

def delete_profile_id(pid):
    p = load_profile(pid)
    if pid == active() and running('frpc'):
        if not confirm('删除当前服务端将停止 frpc，确认继续？'): return
        service('frpc', 'stop')
    if not confirm('删除 ' + label(p) + ' 及其全部端口映射？'): return
    with lock(), transaction():
        folder = pdir(pid)
        if folder.parent != ROOT / 'profiles': raise ValueError('删除路径错误')
        shutil.rmtree(folder)
        if pid == active():
            set_current(''); (unlink_quiet(ROOT / 'frpc.toml'))
        rebuild_index(); audit('profile-deleted')
    print('服务端 profile 及其映射已删除。')

def mapping_command(argv):
    pid = require_active(); items = mappings(pid); action = argv[0] if argv else 'menu'
    if action == 'list':
        print(json.dumps(items, ensure_ascii=False, indent=2)); return
    if action == 'add': items.append(mapping_wizard()); save_mappings(pid, items); return
    if action in ('edit', 'enable', 'disable', 'delete'):
        if len(argv) < 2: raise ValueError('需要映射名称：mapping ' + action + ' <name>')
        name_arg = argv[1]; index = next((i for i, item in enumerate(items) if item['name'] == name_arg), None)
        if index is None: raise ValueError('映射不存在：' + name_arg)
        if action == 'edit': items[index] = mapping_wizard(items[index])
        elif action == 'enable': items[index]['enabled'] = True
        elif action == 'disable': items[index]['enabled'] = False
        elif not confirm('确认删除映射 ' + name_arg + '？'): return
        else: items.pop(index)
        save_mappings(pid, items); return
    if action == 'menu': mapping_menu(); return
    raise ValueError('支持 mapping list/add/edit/enable/disable/delete')

def install(role):
    script = Path(__file__).resolve().parent.parent / ('install-' + role + ('.ps1' if os.name == 'nt' else '.sh'))
    if not script.exists(): raise ValueError('安装脚本未安装')
    args = ['powershell', '-File', str(script), '-Version', 'v' + VERSION] if os.name == 'nt' else ['bash', str(script), '--version', 'v' + VERSION]
    run_cmd(args, check=True)

def uninstall(role):
    script = Path(__file__).resolve().parent.parent / ('uninstall-windows.ps1' if os.name == 'nt' else 'uninstall.sh')
    if not script.exists(): raise ValueError('卸载脚本未安装')
    if confirm('卸载 ' + role + '？默认保留配置和日志。'):
        args = ['powershell', '-File', str(script), '-Component', role, '-Yes'] if os.name == 'nt' else ['bash', str(script), role]
        run_cmd(args, check=True)

SUPPORTED_VERSIONS = ('0.72.0',)

def installed_version(role):
    try:
        result = run_cmd([str(binary(role)), '--version'], capture_output=True, text=True, timeout=10)
        value = (result.stdout or result.stderr).strip().splitlines()
        return value[0].strip() if value else 'unknown'
    except (OSError, subprocess.SubprocessError): return 'unknown'

def upgrade(role, target=None):
    current = installed_version(role)
    print('[升级] 当前 frp 版本：' + current)
    print('[升级] 管理器支持版本：' + '、'.join(SUPPORTED_VERSIONS))
    if target and target.lstrip('v') not in SUPPORTED_VERSIONS:
        raise ValueError(f'版本 {target} 未经配置和 SHA256 验证，拒绝升级。已支持：{", ".join(SUPPORTED_VERSIONS)}')
    if not confirm(f'重新安装并校验 frp {SUPPORTED_VERSIONS[-1]}（保留配置和备份）？'): return
    script = Path(__file__).resolve().parent.parent / ('install-' + role + ('.ps1' if os.name == 'nt' else '.sh'))
    if not script.exists(): raise ValueError('安装脚本未安装，无法升级')
    before = installed_version(role)
    backup_dir = backup([ROOT / (role + '.toml')], 'pre-upgrade')
    args = ['powershell', '-File', str(script), '-Version', 'v' + SUPPORTED_VERSIONS[-1]] if os.name == 'nt' else ['bash', str(script), '--version', 'v' + SUPPORTED_VERSIONS[-1]]
    result = run_cmd(args, capture_output=True, text=True)
    after = installed_version(role)
    if result.returncode != 0:
        result_error('升级失败，正在恢复配置备份。')
        restore_backup(backup_dir)
        if running(role): service(role, 'start')
        raise ValueError('升级脚本失败；配置已从备份恢复。原始输出末尾：' + (result.stderr or result.stdout).strip()[-500:])
    result_ok(f'升级完成：{before} → {after}；配置备份：{backup_dir.name}')
    if running(role) and not service(role, 'status').lower() in ('active', 'started', 'running'):
        result_error('服务未恢复运行，请检查日志。')

def run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ('frps','frpc'): raise ValueError('需要指定 frps/frpc')
    role = argv.pop(0); initialize()
    cmd = argv.pop(0) if argv else 'menu'
    if cmd == 'status': print(json.dumps(status(role), ensure_ascii=False, indent=2)); return
    if cmd == 'version': print(json.dumps({'manager': VERSION, 'frp': installed_version(role), 'supported': list(SUPPORTED_VERSIONS)}, ensure_ascii=False)); return
    if cmd == 'path': print(f'{ROOT}\n{binary(role)}'); return
    if cmd in ('start','stop','restart'):
        if role == 'frpc': require_active()
        service(role, cmd); return
    if cmd == 'logs':
        lines = 50
        if argv and argv[0].isdigit(): lines = int(argv.pop(0))
        show_logs(role, lines); return
    if cmd == 'logs-clean':
        removed = 0
        for item in log_files(role):
            if item.name == role + '.log': continue
            unlink_quiet(item); removed += 1
        print(json.dumps({'removed': removed}, ensure_ascii=False)); return
    if cmd == 'diagnose': print(json.dumps(diagnostics_report(role), ensure_ascii=False, indent=2)); return
    if cmd == 'backups': print(json.dumps([{k: str(v) if k == 'path' else v for k, v in item.items()} for item in list_backups()], ensure_ascii=False, indent=2)); return
    if cmd == 'restore':
        if not argv: raise ValueError('用法：restore <备份目录名>')
        restored = restore_backup(ROOT / 'backups' / argv[0])
        print(json.dumps({'restored': restored}, ensure_ascii=False, indent=2)); return
    if cmd == 'config' and role == 'frps':
        sub = argv.pop(0) if argv else 'show'
        if sub == 'show':
            data = read_frps_config()
            if data.get('auth', {}).get('token'): data['auth']['token'] = '******'
            if data.get('webServer', {}).get('password'): data['webServer']['password'] = '******'
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif sub == 'validate':
            data = read_frps_config(); validate_frps_config(data); verify('frps', toml(data))
            print(json.dumps({'valid': True, 'path': str(ROOT / 'frps.toml')}, ensure_ascii=False)); return
        else: raise ValueError('支持 config show/validate')
        return
    if cmd == 'install': install(role); return
    if cmd == 'uninstall': uninstall(role); return
    if cmd in ('upgrade','update'): upgrade(role, argv[0] if argv else None); return
    if cmd == 'configure': (configure_server if role == 'frps' else profile_wizard)(); return
    if cmd == 'import': import_external(role); return
    if cmd == 'menu':
        try: import_external(role)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
            print('外部配置未同步，原文件已保留；请使用 import 命令排查或从备份恢复。')
        main_menu(role); return
    if cmd == 'profile' and role == 'frpc':
        sub = argv.pop(0) if argv else 'list'
        if sub == 'list': print(json.dumps(profiles(), ensure_ascii=False, indent=2))
        elif sub == 'add': profile_wizard()
        elif sub == 'use' and argv: switch(argv[0])
        elif sub in ('edit', 'rename'): edit_profile()
        elif sub == 'delete' and argv: delete_profile_id(argv[0])
        else: raise ValueError('支持 profile list/add/use <ID>/edit/rename/delete <ID>')
        return
    if cmd == 'mapping' and role == 'frpc':
        mapping_command(argv)
        return
    raise ValueError('未知命令；无参数打开数字菜单')

if __name__ == '__main__':
    try: run()
    except (EOFError, KeyboardInterrupt): print('\n已退出。')
    except Exception as exc: print('错误：' + str(exc), file=sys.stderr); sys.exit(1)
