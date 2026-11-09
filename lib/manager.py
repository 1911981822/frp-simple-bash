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
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # Python 3.9/3.10: install distribution package python3-tomli.
import urllib.request
import urllib.error
import base64

VERSION = '0.72.0'
ROOT = Path(os.environ.get('FRP_CONFIG_DIR', '/etc/frp' if os.name != 'nt' else os.environ.get('ProgramData', 'C:/ProgramData') + '/frp-simple-bash'))
BIN = Path(os.environ.get('FRP_BINARY_DIR', '/opt/frp-simple-bash/native' if os.name != 'nt' else os.environ.get('ProgramFiles', 'C:/Program Files') + '/frp-simple-bash/native'))
TEST = os.environ.get('FRP_TEST_MODE') == '1'

def text(value):
    return json.dumps(value, ensure_ascii=False)

def toml(data):
    """Serialize only the manager schema (scalars and arrays of scalar tables)."""
    lines = []
    def scalar(v):
        if isinstance(v, bool): return str(v).lower()
        if isinstance(v, (int, float)): return str(v)
        if isinstance(v, str): return text(v)
        if isinstance(v, list): return '[' + ', '.join(scalar(x) for x in v) + ']'
        raise ValueError('不支持的配置字段类型')
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            for item in v:
                lines.append(f'\n[[{k}]]')
                for key, val in item.items(): lines.append(f'{key} = {scalar(val)}')
        else: lines.append(f'{k} = {scalar(v)}')
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
        tmp.unlink(missing_ok=True)

def managed_paths():
    return set(ROOT.glob('*.json')) | set(ROOT.glob('*.toml')) | set((ROOT / 'profiles').glob('*/*.toml'))

@contextlib.contextmanager
def transaction():
    """Restore model, generated config, index and state together on any write failure."""
    originals = {p: p.read_bytes() for p in managed_paths()}
    try: yield
    except Exception:
        for p in managed_paths() - set(originals): p.unlink(missing_ok=True)
        for p, value in originals.items(): atomic(p, value.decode('utf-8-sig'))
        raise

def read_toml(path):
    return tomllib.loads(Path(path).read_text(encoding='utf-8-sig'))

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
        if p.stat().st_mtime < cutoff and p.suffix != '.log': p.unlink(missing_ok=True)
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
    tomllib.loads(content)
    if TEST: return
    if not binary(role).exists(): raise ValueError('原生 frp 尚未安装；请先运行安装器')
    tmp = ROOT / ('.verify-' + secrets.token_hex(6) + '.toml')
    atomic(tmp, content)
    try:
        r = subprocess.run([str(binary(role)), 'verify', '-c', str(tmp)], capture_output=True, timeout=15)
        if r.returncode: raise ValueError('frp 配置校验失败；原配置已保留（输出可能含敏感字段，未回显）')
    finally: tmp.unlink(missing_ok=True)

def render(pid):
    p = load_profile(pid); s = secrets_for(pid); items = mappings(pid)
    lines = [f'serverAddr = {text(p["server_addr"])}', f'serverPort = {p["server_port"]}',
             f'transport.tls.enable = {str(p.get("tls_enable", True)).lower()}',
             'loginFailExit = false', f'clientID = {text(pid)}',
             f'log.to = {text(str(ROOT / "logs" / "frpc.log"))}', 'log.maxDays = 30', 'log.disablePrintColor = true']
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
            result = subprocess.run(['sc.exe', 'config', 'frp-simple-bash-' + role, 'start=', 'auto' if action == 'enable' else 'demand'], capture_output=True, text=True, timeout=30)
        else: result = subprocess.run([str(wrapper), action], capture_output=True, text=True, timeout=30)
    elif shutil.which('systemctl') and Path('/run/systemd/system').exists():
        args = ['systemctl', 'is-active' if action == 'status' else action, role]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    elif shutil.which('rc-service'):
        if action in ('enable', 'disable'):
            result = subprocess.run(['rc-update', 'add' if action == 'enable' else 'del', role, 'default'], capture_output=True, text=True, timeout=30)
        else: result = subprocess.run(['rc-service', role, action], capture_output=True, text=True, timeout=30)
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

def ask(prompt, default=None, secret=False):
    suffix = f'（直接回车使用 {default}）' if default is not None else ''
    if secret:
        import getpass
        v = getpass.getpass(prompt + suffix + '：')
    else: v = input(prompt + suffix + '：')
    return default if not v and default is not None else v

def confirm(prompt): return ask(prompt + ' [y/N]').lower() in ('y', 'yes')
def choose(items, title='请选择'):
    print('\n' + title)
    for n, item in enumerate(items, 1): print(f'{n}. {item}')
    print('0. 返回/退出')
    while True:
        v = ask('请输入数字')
        if v.isdecimal() and 0 <= int(v) <= len(items): return int(v)
        print('无效编号，请重新输入。')

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

def backup(paths):
    directory = ROOT / 'backups' / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3))
    directory.mkdir(mode=0o700)
    for path in paths:
        if path.exists(): shutil.copy2(path, directory / path.name)
    return directory

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
    print('端口映射配置已保存。' + ('当前 frpc 运行中，请按提示重启后生效。' if running('frpc') else '当前 frpc 未运行，下次启动时生效。'))
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

def mapping_menu():
    while True:
        pid = require_active(); print('\n当前 frps：' + label(load_profile(pid)))
        n = choose(['新增端口映射', '查看当前服务端映射', '修改端口映射', '启用/停用端口映射', '删除端口映射'])
        if not n: return
        try:
            items = mappings(pid)
            if n == 1: items.append(mapping_wizard()); save_mappings(pid, items)
            elif n == 2:
                for i, m in enumerate(items, 1): print(f'{i}. ' + json.dumps(m, ensure_ascii=False))
                if not items: print('暂无映射')
            else:
                index = choose([m['name'] + (' [启用]' if m.get('enabled', True) else ' [停用]') for m in items], '选择映射')
                if not index: continue
                if n == 3: items[index-1] = mapping_wizard(items[index-1])
                elif n == 4: items[index-1]['enabled'] = not items[index-1].get('enabled', True)
                elif not confirm('确认删除映射 ' + items[index-1]['name'] + '？'): continue
                else: items.pop(index-1)
                save_mappings(pid, items)
        except (ValueError, OSError, KeyError) as e: print('操作失败：' + str(e))

def edit_profile():
    pid = require_active(); p = load_profile(pid); s = secrets_for(pid)
    version = hashlib.sha256((pdir(pid) / 'profile.toml').read_bytes() + (pdir(pid) / 'secrets.toml').read_bytes()).hexdigest()
    while True:
        n = choose(['查看连接配置', '修改服务端名称', '修改服务器地址', '修改服务器端口', '修改 token', '修改 TLS 配置', '修改 Dashboard/API', '保存并返回', '删除当前服务端及全部映射'], label(p))
        if n == 0: return
        if n == 1: print(json.dumps(p, ensure_ascii=False, indent=2))
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
        elif n == 9:
            if confirm('删除此服务端及全部映射？不额外备份。'):
                with lock(), transaction():
                    if running('frpc'): service('frpc', 'stop')
                    folder = pdir(pid)
                    if folder.parent != ROOT / 'profiles': raise ValueError('删除路径错误')
                    shutil.rmtree(folder); set_current(''); (ROOT / 'frpc.toml').unlink(missing_ok=True); rebuild_index()
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
    canonical = tomllib.loads(render(pid))
    if config.get('log', {}) != canonical.get('log', {}) or config.get('loginFailExit', False) is not False:
        raise ValueError('日志/登录重试策略变更尚不支持反向导入，原文件保留')
    transport = config.get('transport', {})
    if set(transport) - {'tls'} or set(transport.get('tls', {})) - {'enable', 'certFile', 'keyFile', 'trustedCaFile'}:
        raise ValueError('高级传输字段无法无损导入，原文件保留')
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

def show_status(role):
    data = status(role)
    states = {'active': '运行中', 'running': '运行中', 'started': '运行中', 'stopped': '已停止', 'inactive': '已停止', 'failed': '异常', 'unknown': '未知', 'activating': '启动中'}
    print('\n' + ('frps 服务端管理' if role == 'frps' else 'frpc 客户端管理'))
    print('─' * 42)
    print('运行状态：' + states.get(data['service_state'].lower(), data['service_state']))
    print('frp 版本：' + data['version'])
    print('配置路径：' + data['config_path'])
    if role == 'frps':
        path = ROOT / 'frps.toml'
        if path.exists():
            config = read_toml(path)
            host = config.get('bindAddr', '0.0.0.0')
            if ':' in host: host = '[' + host + ']'
            print(f'业务监听：{host}:{config.get("bindPort", 7000)}')
            web = config.get('webServer', {})
            if web.get('port'):
                addr = web.get('addr', '127.0.0.1')
                if ':' in addr: addr = '[' + addr + ']'
                scheme = 'https' if web.get('tls', {}).get('certFile') else 'http'
                print(f'Dashboard：{scheme}://{addr}:{web["port"]}')
                if web.get('addr', '127.0.0.1') in ('127.0.0.1', '::1'):
                    print('管理访问：仅本机；终端菜单可正常查询客户端信息')
            else: print('Dashboard：未启用')
        else: print('配置状态：未配置')
    else:
        p = data.get('active_profile')
        print('当前服务端：' + (label(p) if p else '未选择'))
        if p:
            translations = {'connected': '已连接', 'connecting': '连接中', 'stopped': '已停止', 'unknown': '待确认', 'proxy-error': '映射异常', 'auth-failed': '认证失败', 'tls-failed': 'TLS 失败', 'timeout': '连接超时'}
            connection_data = data['connection']
            print('连接状态：' + translations.get(connection_data['state'], connection_data['state']))
            print('映射数量：' + str(len(mappings(p['id']))))
            if data.get('pending'): print('配置生效：有修改等待重启')
    print('─' * 42)
    return data

def service_feedback(role, action):
    labels = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    state_value = service(role, 'status').strip().lower()
    expected = {'start': ('active', 'started', 'running'), 'stop': ('inactive', 'stopped', 'dead'), 'restart': ('active', 'started', 'running')}[action]
    if state_value in expected:
        print(f'{labels[action]}成功：当前状态为 {"运行中" if action != "stop" else "已停止"}。')
    else:
        print(f'{labels[action]}请求已发送，但当前服务状态为：{state_value or "未知"}。请查看日志。')
    return state_value

def main_menu(role):
    while True:
        try:
            data = show_status(role)
            alias = data.get('active_profile', {}).get('name', '未选择')
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
            print('配置无法读取；请检查配置文件或从 backups 恢复，服务管理仍可使用。')
            alias = '配置错误'
        if role == 'frpc':
            items = ['查看当前状态', '启动 frpc', '停止 frpc', '重启 frpc', '新建 frps 服务端', '查看服务端列表', '切换当前 frps 服务端', '修改当前 frps 配置', f'修改当前 frps（{alias}）端口映射', '查看日志与连接诊断', '设置/取消开机自启动', '卸载 frpc', '升级 frpc']
        else: items = ['查看运行状态', '查看版本信息', '查看安装路径', '启动 frps', '停止 frps', '重启 frps', '查看客户端连接信息', '修改服务端配置', '查看日志', '设置/取消开机自启动', '卸载 frps', '升级 frps']
        n = choose(items)
        if not n: return
        try:
            if role == 'frpc':
                if n == 1: continue  # The next menu frame refreshes status once.
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
                elif n == 10: show_logs(role)
                elif n == 11: autostart(role)
                elif n == 12: uninstall(role)
                elif n == 13: upgrade(role)
            else:
                if n == 1: continue  # The next menu frame refreshes status once.
                elif n == 2: print(VERSION)
                elif n == 3: print(f'原生程序：{binary(role)}\n配置目录：{ROOT}')
                elif n in (4,5,6):
                    if n == 4 or confirm(items[n-1] + '？'):
                        service_feedback(role, {4:'start',5:'stop',6:'restart'}[n])
                elif n == 7: server_clients()
                elif n == 8: configure_server()
                elif n == 9: show_logs(role)
                elif n == 10: autostart(role)
                elif n == 11: uninstall(role)
                elif n == 12: upgrade(role)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as e: print('操作失败：' + str(e))

def show_logs(role):
    path = ROOT / 'logs' / (role + '.log')
    if path.exists(): print('\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[-50:]))
    elif os.name != 'nt' and shutil.which('journalctl'): subprocess.run(['journalctl','-u',role,'-n','50','--no-pager'])
    else: print('暂无文件日志；请查看平台服务日志。')
    print(f'{role} 日志读取完成；以上为最近 50 行。')

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
            set_current(''); (ROOT / 'frpc.toml').unlink(missing_ok=True)
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
    subprocess.run(args, check=True)

def uninstall(role):
    script = Path(__file__).resolve().parent.parent / ('uninstall-windows.ps1' if os.name == 'nt' else 'uninstall.sh')
    if not script.exists(): raise ValueError('卸载脚本未安装')
    if confirm('卸载 ' + role + '？默认保留配置和日志。'):
        args = ['powershell', '-File', str(script), '-Component', role, '-Yes'] if os.name == 'nt' else ['bash', str(script), role]
        subprocess.run(args, check=True)

def upgrade(role):
    print('当前版本：' + VERSION)
    if confirm('重新下载并校验固定版本 0.72.0（保留配置）？'):
        script = Path(__file__).resolve().parent.parent / ('install-' + role + ('.ps1' if os.name == 'nt' else '.sh'))
        subprocess.run((['powershell','-File'] if os.name == 'nt' else ['bash']) + [str(script)], check=True)

def run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ('frps','frpc'): raise ValueError('需要指定 frps/frpc')
    role = argv.pop(0); initialize()
    cmd = argv.pop(0) if argv else 'menu'
    if cmd == 'status': print(json.dumps(status(role), ensure_ascii=False, indent=2)); return
    if cmd == 'version': print(VERSION); return
    if cmd == 'path': print(f'{ROOT}\n{binary(role)}'); return
    if cmd in ('start','stop','restart'):
        if role == 'frpc': require_active()
        service(role, cmd); return
    if cmd == 'logs': show_logs(role); return
    if cmd == 'install': install(role); return
    if cmd == 'uninstall': uninstall(role); return
    if cmd in ('upgrade','update'): upgrade(role); return
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
