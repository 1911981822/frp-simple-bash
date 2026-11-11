#!/usr/bin/env python3
"""frp-simple-bash shared management engine; requires Python >= 3.6."""
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
# Manager release version (the management scripts themselves, not the frp binary).
# Bumped on every published release; upgrade() pulls exactly this bundle.
MANAGER_RELEASE_VERSION = 'v0.1.25-dashboard.1'
ROOT = Path(os.environ.get('FRP_CONFIG_DIR', '/etc/frp' if os.name != 'nt' else os.environ.get('ProgramData', 'C:/ProgramData') + '/frp-simple-bash'))
BIN = Path(os.environ.get('FRP_BINARY_DIR', '/opt/frp-simple-bash/native' if os.name != 'nt' else os.environ.get('ProgramFiles', 'C:/Program Files') + '/frp-simple-bash/native'))
INSTALL_ROOT = Path(os.environ.get('FRP_INSTALL_DIR', '/opt/frp-simple-bash' if os.name != 'nt' else os.environ.get('ProgramFiles', 'C:/Program Files') + '/frp-simple-bash'))
TEST = os.environ.get('FRP_TEST_MODE') == '1'

# Single source of truth for state labels (详细设计 §10.2 / §10.3).
# Keys include both the documented enum values and the engine's own aliases.
SERVICE_STATES = {
    'not-installed': '未安装', 'stopped': '已停止', 'starting': '启动中',
    'running': '运行中', 'stopping': '停止中', 'failed': '异常', 'unknown': '未知',
    'active': '运行中', 'inactive': '已停止', 'started': '运行中',
    'activating': '启动中', 'deactivating': '停止中', 'dead': '已停止',
}
CONNECTION_STATES = {
    'not-configured': '未配置', 'connecting': '连接中', 'connected': '已连接',
    'disconnected': '已断开', 'auth-failed': '认证失败', 'unreachable': '不可达',
    'tls-failed': 'TLS 失败', 'config-error': '配置错误', 'timeout': '连接超时',
    'unknown': '待确认', 'stopped': '已停止', 'proxy-error': '映射异常',
}

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
    def scalar(v, key=''):
        if isinstance(v, bool): return str(v).lower()
        if isinstance(v, (int, float)): return str(v)
        if isinstance(v, str): return text(v)
        if isinstance(v, list):
            if any(isinstance(x, dict) for x in v):
                raise ValueError('配置字段 %s 的数组元素类型不一致（数组表需单独处理）' % (key or '?'))
            return '[' + ', '.join(scalar(x, key) for x in v) + ']'
        if v is None:
            raise ValueError('配置字段 %s 不允许为 None（取消操作不应写入配置）' % (key or '?'))
        raise ValueError('配置字段 %s 的类型不受支持：%s' % (key or '?', type(v).__name__))
    def table(obj, prefix=''):
        scalars, tables = [], []
        for k, v in obj.items():
            if isinstance(v, dict): tables.append((k, v))
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                if not all(isinstance(x, dict) for x in v):
                    raise ValueError('配置字段 %s 的数组元素类型不一致' % k)
                tables.append((k, v))
            else: scalars.append((k, v))
        for k, v in scalars: lines.append(f'{k} = {scalar(v, k)}')
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

def _created_dirs():
    """Directories under ROOT/profiles that did not exist at snapshot time."""
    base = ROOT / 'profiles'
    if not base.exists(): return set()
    return {d for d in base.iterdir() if d.is_dir() and not d.is_symlink()}

@contextlib.contextmanager
def transaction():
    """Restore model, generated config, index and state together on any failure.

    BaseException (Ctrl-C / SystemExit) is included so the lock is never
    released over a half-written state. Rollback failures are logged but do
    not mask the original exception.
    """
    originals = {p: p.read_bytes() for p in managed_paths()}
    dirs_before = _created_dirs()
    try:
        yield
    except BaseException:
        for p in managed_paths() - set(originals):
            try: unlink_quiet(p)
            except OSError: pass
        for d in _created_dirs() - dirs_before:
            try: shutil.rmtree(d)
            except OSError: pass
        for p, value in originals.items():
            try: atomic(p, value.decode('utf-8-sig'))
            except OSError as exc:
                print('  [失败] 回滚写入失败：' + str(p) + ' — ' + str(exc), file=sys.stderr)
        raise

def _mini_toml(text):
    """Dependency-free TOML subset parser.

    Mirrors tomllib for the subset we emit: dotted keys, [tables], [[array of
    tables]], basic/literal strings, inline tables, arrays, bools and numbers.
    Python 3.6 has no tomllib/tomli, so this path must be fully equivalent.
    """
    root = {}

    def split_array(inner):
        """Split an array body on top-level commas (quote/bracket aware)."""
        parts, buf, quote, depth = [], '', '', 0
        for ch in inner:
            if quote:
                buf += ch
                if ch == quote and (len(buf) < 2 or buf[-2] != '\\'):
                    quote = ''
                continue
            if ch in ('"', "'"):
                quote = ch; buf += ch; continue
            if ch in '[{': depth += 1; buf += ch; continue
            if ch in ']}': depth -= 1; buf += ch; continue
            if ch == ',' and depth == 0:
                parts.append(buf); buf = ''; continue
            buf += ch
        if quote or depth != 0: raise ValueError('数组或内联表未闭合')
        parts.append(buf)
        return parts

    def scalar(raw):
        raw = raw.strip()
        if not raw: raise ValueError('空值')
        if raw[0] == '"':
            # json.dumps produces TOML-compatible basic strings; json.loads
            # decodes \\, \n, \r, \t, \uXXXX, \" in one correct pass.
            try: return json.loads(raw)
            except ValueError: raise ValueError('字符串解析失败（未回显原值，避免泄露敏感字段）')
        if raw[0] == "'":
            if len(raw) < 2 or not raw.endswith("'"): raise ValueError('单引号字符串未闭合')
            return raw[1:-1]
        if raw in ('true', 'false'): return raw == 'true'
        if raw.startswith('['):
            if not raw.endswith(']'): raise ValueError('数组未闭合')
            inner = raw[1:-1].strip()
            return [scalar(x) for x in split_array(inner)] if inner else []
        if raw.startswith('{'):
            if not raw.endswith('}'): raise ValueError('内联表未闭合')
            result = {}
            inner = raw[1:-1].strip()
            for part in (split_array(inner) if inner else []):
                if '=' not in part: raise ValueError('内联表缺少 =')
                k, v = part.split('=', 1)
                _assign(result, k.strip().split('.'), scalar(v))
            return result
        cleaned = raw.replace('_', '')
        for prefix, base in (('0x', 16), ('0o', 8), ('0b', 2)):
            if cleaned.lower().startswith(prefix):
                try: return int(cleaned, base)
                except ValueError: pass
        try: return int(cleaned)
        except ValueError:
            try: return float(cleaned)
            except ValueError: raise ValueError('无法解析的值（未回显原值，避免泄露敏感字段）')

    def _assign(node, keys, value):
        """Assign into nested dicts following dotted key segments."""
        for key in keys[:-1]:
            nxt = node.setdefault(key, {})
            if isinstance(nxt, list): nxt = nxt[-1]
            if not isinstance(nxt, dict): raise ValueError('键路径冲突：' + key)
            node = nxt
        node[keys[-1]] = value

    def target():
        """Resolve current [table] / [[array of tables]] path to a dict."""
        node = root
        for key in path:
            nxt = node.setdefault(key, {})
            if isinstance(nxt, list): nxt = nxt[-1]
            node = nxt
        return node

    path = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('[['):
            if not line.endswith(']]'): raise ValueError('表数组未闭合（第 %d 行）' % number)
            parts = line[2:-2].strip().split('.')
            parent = root
            for part in parts[:-1]:
                nxt = parent.setdefault(part, {})
                if isinstance(nxt, list): nxt = nxt[-1]
                parent = nxt
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
        _assign(target(), key.strip().split('.'), scalar(value))
    return root

def _load_toml(text):
    if _toml_lib is not None: return _toml_lib.loads(text)
    return _mini_toml(text)

def read_toml(path):
    return _load_toml(Path(path).read_text(encoding='utf-8-sig'))

def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else ({} if default is None else default)

def save_json(path, data): atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')

def _pid_alive(pid):
    """Best-effort liveness check; conservative (assume alive) on Windows."""
    if not isinstance(pid, int) or pid <= 0: return False
    if os.name == 'nt': return True
    try: os.kill(pid, 0)
    except OSError: return False
    return True

def _clear_stale_lock(p):
    """Remove a lock left by a dead/expired process. Returns True if cleared."""
    owner = p / 'owner.json'
    try:
        info = read_json(owner, {}) if owner.exists() else {}
        pid = info.get('pid')
        age = time.time() - float(info.get('time', 0))
        if age > 300 or not _pid_alive(pid):
            shutil.rmtree(p)
            return True
    except (OSError, ValueError, TypeError):
        try: shutil.rmtree(p); return True
        except OSError: pass
    return False

@contextlib.contextmanager
def lock():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = ROOT / '.operation.lock'
    try:
        p.mkdir(mode=0o700)
    except FileExistsError:
        if not _clear_stale_lock(p):
            raise ValueError('另一个配置操作正在执行；若进程已异常退出，请检查后移除 .operation.lock')
        p.mkdir(mode=0o700)
    try:
        save_json(p / 'owner.json', {'pid': os.getpid(), 'time': time.time()})
        yield
    finally:
        try: unlink_quiet(p / 'owner.json')
        except OSError: pass
        try: p.rmdir()
        except OSError:
            # Last resort: never leave a stale lock behind.
            try: shutil.rmtree(p)
            except OSError: pass

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
            archive = p.with_name(p.name + '.' + str(int(time.time() * 1e9)))
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
    if isinstance(v, bool) or not re.fullmatch(r'[0-9]{1,5}', str(v)) or not 1 <= int(v) <= 65535: raise ValueError('端口必须是 1-65535 的 ASCII 数字')
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

def set_pending(value):
    """Track whether frpc.toml was rewritten while frpc is still running."""
    s = state(); s['pending'] = bool(value); save_json(ROOT / 'state.json', s)

def apply(pid):
    """Generate and install frpc.toml following 详细设计 §12.1.

    Callers are responsible for holding lock() (save_mappings/switch already
    do); calling lock() again here would deadlock. This adds the missing
    backup-old-file, write-temp, re-validate-from-disk and audit steps.
    """
    content = render(pid); verify('frpc', content)
    target = ROOT / 'frpc.toml'
    if target.exists(): backup([target], 'frpc-config')
    atomic(target, content)
    # Re-read from disk: catches encoding/truncation errors before commit.
    verify('frpc', target.read_text(encoding='utf-8'))
    mark_hash('frpc')
    if running('frpc'): set_pending(True)
    audit('frpc-config-written', pid)

def set_current(pid):
    s = state(); s['active_profile_id'] = pid; save_json(ROOT / 'state.json', s); audit('profile-selected', pid)

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
    if result.returncode: err('E007', '服务操作失败：' + result.stderr.strip())
    if action in ('start', 'stop', 'restart'): audit('service-' + action)
    if role == 'frpc' and action in ('start', 'restart'):
        set_pending(False)

def fail(message): raise ValueError(message)

# 详细设计 §15.1 错误码表。Codes are attached as a message prefix so all
# existing `except ValueError` handlers keep working unchanged.
ERROR_CODES = {
    'E001': '不支持的系统/架构',
    'E002': '权限不足（需要 root / 管理员）',
    'E003': '下载失败（检查网络和镜像）',
    'E004': '校验失败（保留旧版本）',
    'E005': '配置格式错误（已恢复旧配置）',
    'E006': '端口非法或被占用',
    'E007': '服务启动失败',
    'E008': '地址不可达',
    'E009': 'token / TLS 认证失败',
    'E010': 'Dashboard/API 不可用',
    'E011': 'profile 不存在',
    'E012': '映射不存在',
    'E013': '连接超时',
    'E014': '配置写入失败（检查权限和磁盘）',
    'E015': '用户取消',
    'E016': '锁被其他操作占用',
    'E017': '版本不兼容',
}

def err(code, message):
    """Raise a coded error: '<message>[Exxx]'. Never changes the exception type."""
    raise ValueError(str(message) + '[' + code + ']')

def error_log_path(role):
    """Log locations quoted in error hints (需求 §11 四要素)."""
    return str(ROOT / 'logs' / (role + '.log')) + ' 与 ' + str(ROOT / 'logs' / 'manager.log')

def running(role): return service(role, 'status').lower() in ('active', 'started', 'running')

def api(url, user, password, timeout=2):
    req = urllib.request.Request(url)
    if user or password: req.add_header('Authorization', 'Basic ' + base64.b64encode((user + ':' + password).encode()).decode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response: data = json.load(response)
    if '/api/v2/' in url:
        if data.get('code') != 200 or 'data' not in data: raise ValueError('Dashboard v2 API 返回错误')
        return data['data']
    return data

def _login_state_from_log():
    """Read the tail of frpc.log and decide login state from frp 0.72 messages.

    Works with zero proxies and does not depend on admin API shape or
    Dashboard credentials. Only the current process (last 'start frpc service'
    line) is considered so old sessions cannot produce a false 'connected'.
    """
    logfile = ROOT / 'logs' / 'frpc.log'
    if not logfile.exists(): return None
    try:
        with logfile.open('rb') as f:
            size = logfile.stat().st_size
            f.seek(max(0, size - 262144))
            logs = f.read().decode('utf-8', errors='replace')
    except OSError:
        return None
    start = logs.rfind('start frpc service')
    if start >= 0: logs = logs[start:]
    success = logs.rfind('login to server success')
    # frp 0.72 does NOT emit 'login to server failed'/'control writer is closing'.
    # Verified against the bundled 0.72.0 binary: real auth failures look like
    # 'User authentication failed' / 'Failed to authenticate'.
    failure_marks = ('try to reconnect', 'connect to server error',
                     'login to server failed', 'control writer is closing',
                     'user authentication failed', 'failed to authenticate',
                     'authentication failed', 'login to server error')
    failure = max(logs.rfind(x) for x in failure_marks)
    low = logs.lower()
    auth_error = max(low.rfind('token'), low.rfind('authentication'), low.rfind('authenticate'))
    tls_error = max(low.rfind('tls'), low.rfind('certificate'))
    if success >= 0 and success > failure:
        return {'state': 'connected', 'source': 'frpc-log'}
    # Only infer auth/TLS failure when there is no successful login this process.
    if success < 0:
        if auth_error >= 0:
            return {'state': 'auth-failed', 'detail': 'token 或认证参数不匹配'}
        if tls_error >= 0:
            return {'state': 'tls-failed', 'detail': 'TLS 证书或信任配置错误'}
    if failure > success:
        return {'state': 'connecting', 'source': 'frpc-log'}
    return None

_CONN_CACHE = {}

def connection(pid, fresh=False):
    """Resolve frpc connection state. A short-lived cache keeps menu redraws
    from repeating blocking API probes; wait_connection() passes fresh=True."""
    logfile = ROOT / 'logs' / 'frpc.log'
    try:
        info = logfile.stat()
        stamp = getattr(info, 'st_mtime_ns', int(info.st_mtime * 1e9))
    except OSError: stamp = 0
    key = (pid, stamp)
    now = time.monotonic()
    if not fresh:
        hit = _CONN_CACHE.get(key)
        if hit and now - hit[0] < 2.0:
            return dict(hit[1])
    result = _connection_uncached(pid)
    _CONN_CACHE[key] = (now, dict(result))
    return result

def _connection_uncached(pid):
    if not running('frpc'): return {'state': 'stopped'}
    p = load_profile(pid); s = secrets_for(pid)
    logged = _login_state_from_log()
    # 1) Explicit login failure from frp's own log wins immediately.
    if logged and logged['state'] in ('auth-failed', 'tls-failed'):
        return logged
    # 2) With no enabled proxies there is nothing for the admin API to report:
    #    a successful login line is conclusive and avoids all network probes.
    #    (Local disk read only; keeps the menu responsive when Dashboard is down.)
    try:
        enabled = [x for x in mappings(pid) if x.get('enabled', True)]
    except (ValueError, OSError, KeyError, TypeError):
        enabled = None  # unreadable mappings: fall back to full probing
    if enabled == [] and logged and logged['state'] == 'connected':
        return logged
    # 3) Per-proxy status from the local admin API is authoritative when
    #    proxies exist; it must NOT be masked by a successful login line.
    try:
        data = api(f'http://127.0.0.1:{p.get("admin_port", 7400)}/api/status', 'frp-simple-bash', s['admin_password'])
        rows = [x for group in data.values() if isinstance(group, list) for x in group if isinstance(x, dict)]
        if rows:
            ok = all(x.get('status') in ('online', 'running') for x in rows)
            return {'state': 'connected' if ok else 'proxy-error', 'proxies': rows}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403): return {'state': 'auth-failed', 'detail': f'管理 API 认证失败（HTTP {exc.code}）'}
    except (OSError, ValueError):
        pass
    # 5) Zero proxies: frp's login line proves the connection.
    if logged and logged['state'] == 'connected':
        return logged
    # 6) Optional Dashboard API as a last resort.
    if s.get('dashboard_username'):
        try:
            host = p.get('dashboard_addr') or p['server_addr']; host = '[' + host + ']' if ':' in host else host
            scheme = p.get('dashboard_scheme', 'http')
            data = api(f'{scheme}://{host}:{p.get("dashboard_port", 7500)}/api/v2/clients?pageSize=200',
                       s['dashboard_username'], s.get('dashboard_password', ''), timeout=3)
            for item in data.get('items', []):
                if item.get('clientID') == pid and item.get('online'):
                    return {'state': 'connected', 'source': 'dashboard'}
        except (OSError, ValueError, urllib.error.HTTPError):
            pass
    # 7) No positive signal yet: report the transient state, never mask as connected.
    if logged:
        return logged
    return {'state': 'unknown', 'detail': '无启用映射且服务端 API 不可访问，无法确认登录状态'}

def wait_connection(pid, timeout=30):
    """Poll connection state with a visible countdown and actionable next steps."""
    deadline = time.monotonic() + timeout
    last = ''
    while True:
        result = connection(pid, fresh=True)
        state = result['state']
        if state == 'connected':
            result_ok('已连接服务端。')
            return True
        if state in ('auth-failed', 'tls-failed'):
            result_error('连接失败：' + result.get('detail', state) + '；保留当前 profile。',
                         hint='排查：服务端的 token / TLS 配置；菜单「日志与连接诊断」→ 运行连接诊断。')
            return False
        # A stopped/failed service can never reach "connected": fail fast with
        # the real reason instead of waiting out the whole timeout.
        if state == 'stopped' and not running('frpc'):
            result_error('frpc 未在运行，连接检测无法继续；已保留当前 profile。',
                         hint='先通过「启动 frpc」启动服务；若启动失败请查看「日志与连接诊断」→ 最近 50 行日志。')
            return False
        if state == 'config-error':
            result_error('配置错误，无法建立连接；已保留当前 profile。',
                         hint='检查「修改当前 frps 配置」中的地址/端口/token/TLS；或从「备份与恢复配置」恢复。')
            return False
        remaining = max(0, int(deadline - time.monotonic()))
        if remaining == 0:
            result_error(f'连接检测超时（{timeout} 秒），保留新 profile。',
                         hint='下一步：菜单「日志与连接诊断」→ 运行连接诊断 / 查看最近 50 行日志；检查服务端防火墙、安全组与地址端口。')
            return False
        if state != last:
            print(f'  {_tag("[提示]")} 正在连接…（{CONNECTION_STATES.get(state, state)}），剩余 {remaining} 秒。')
            last = state
        time.sleep(min(2, max(0, deadline - time.monotonic())))

def switch(pid):
    """Switch the active profile.

    Only the config write is transactional. Interactive confirmation, service
    start/stop and the 30s connection probe stay OUTSIDE the lock so a long
    wait cannot block other terminals, and a failed start is compensated
    instead of leaving frpc stopped (详细设计 §15.2(4)).
    """
    previous = active()
    try: old_label = label(load_profile(previous)) if previous else '（无）'
    except (ValueError, OSError, KeyError, TypeError): old_label = '（无）'
    new_label = label(load_profile(pid))
    content = render(pid); verify('frpc', content)
    was_running = running('frpc')
    if was_running and not confirm('切换将中断当前连接，确认继续？'): return
    old = (ROOT / 'frpc.toml').read_bytes() if (ROOT / 'frpc.toml').exists() else None
    if was_running: service('frpc', 'stop')
    try:
        with lock(), transaction():
            atomic(ROOT / 'frpc.toml', content); set_current(pid); mark_hash('frpc')
    except Exception:
        # Restore the previous runtime config and bring the old service back.
        if old is not None: atomic(ROOT / 'frpc.toml', old.decode('utf-8'))
        if was_running:
            try: service('frpc', 'start')
            except (ValueError, OSError) as exc:
                result_error('切换失败且旧服务未能自动恢复：' + str(exc),
                             hint='请手动执行「启动 frpc」，或从「备份与恢复配置」恢复。')
        raise
    if was_running:
        try:
            service('frpc', 'start')
        except (ValueError, OSError) as exc:
            # Target profile stays active (per doc), but the service is down:
            # compensate by restoring the previous runtime config.
            if old is not None: atomic(ROOT / 'frpc.toml', old.decode('utf-8'))
            try: service('frpc', 'start')
            except (ValueError, OSError): pass
            result_error('切换到 ' + new_label + ' 后启动失败：' + str(exc),
                         hint='已回退运行配置并尝试恢复旧服务；当前 active 仍为 ' + new_label +
                              '，请检查其配置后重试，或重新切换到其他服务端。')
            return
        wait_connection(pid)
    else:
        set_pending(False)
        result_info('已切换当前配置，frpc 保持停止。', detail=f'当前服务端：{old_label} → {new_label}')

CANCEL_KEYS = ('q', 'Q')
BACK_KEYS = ('b', 'B')

def _is_tty():
    try: return sys.stdout.isatty()
    except (AttributeError, ValueError): return False

def _ui_width():
    """Frame width follows the terminal; keeps narrow SSH sessions from wrapping."""
    try: cols = shutil.get_terminal_size((80, 24)).columns
    except (AttributeError, ValueError, OSError): cols = 80
    cols = max(40, min(100, cols))
    return max(28, cols - 6)

RULE = '━' * _ui_width()
# Colors only on a real terminal, and never when NO_COLOR is set (pipes/redirects).
UI_COLOR = _is_tty() and not os.environ.get('NO_COLOR')
# Clear by default on a terminal; FRP_UI_CLEAR=0 disables it.
UI_CLEAR = _is_tty() and os.environ.get('FRP_UI_CLEAR') != '0'

_COLORS = {'[成功]': '32', '[提示]': '36', '[失败]': '31'}

def _tag(kind):
    if UI_COLOR and kind in _COLORS:
        return '\033[1;' + _COLORS[kind] + 'm' + kind + '\033[0m'
    return kind

def _state_color(text, good=None):
    """Color a short state label; no-op when colors are off."""
    if not UI_COLOR:
        return text
    if good is None:
        good = text in ('运行中', '已连接', '运行中')
    return '\033[1;' + ('32' if good else '31') + 'm' + text + '\033[0m'

def _display_width(text):
    """Approximate terminal width: CJK/fullwidth count as 2; ANSI escapes as 0."""
    width = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\033' and i + 1 < len(text) and text[i + 1] == '[':
            end = text.find('m', i + 2)
            if end >= 0:
                i = end + 1
                continue
        width += 2 if ord(ch) > 0x2E7F else 1
        i += 1
    return width

def _wrap_text(text, width):
    """Wrap by display width. Prefer breaking after whitespace/punctuation."""
    import unicodedata
    lines = []
    for raw in str(text).splitlines() or ['']:
        if not raw:
            lines.append('')
            continue
        buf = ''
        used = 0
        last_break = -1  # index in buf of the last good break point
        for ch in raw:
            w = 2 if ord(ch) > 0x2E7F else 1
            if used + w > width and buf:
                cut = last_break + 1 if last_break >= 0 else len(buf)
                lines.append(buf[:cut])
                buf = buf[cut:]
                used = _display_width(buf)
                last_break = -1
            buf += ch
            used += w
            if ch.isspace() or unicodedata.category(ch).startswith('P'):
                last_break = len(buf) - 1
        lines.append(buf)
    return lines

def _pad_line(text, width):
    return text + ' ' * max(0, width - _display_width(text))

def _box(kind, message, detail=None):
    """Render a result inside a frame; long text wraps instead of overflowing."""
    inner = _ui_width()
    print()
    print('┌' + '─' * inner + '┐')
    wrapped = _wrap_text(kind + ' ' + message, inner - 2)
    for pos, line in enumerate(wrapped):
        shown = _tag(kind) + line[len(kind):] if pos == 0 and line.startswith(kind) else line
        print('│' + _pad_line(' ' + shown, inner) + '│')
    if detail:
        for line in _wrap_text(str(detail), inner - 4):
            print('│' + _pad_line('   ' + line, inner) + '│')
    print('└' + '─' * inner + '┘')

def ask(prompt, default=None, secret=False, example=None, allow_back=False, allow_empty=False):
    """Prompt once. 'q' cancels the operation; 'b' (when allowed) goes one step back.

    allow_empty=True returns '' for a blank line (used for "leave empty to clear
    / disable / close"). A required field (no default, allow_empty=False) still
    refuses blank input. 'q' always yields None, which callers must not persist.
    """
    hint = ''
    if example: hint += f'（例：{example}）'
    if default: hint += f'（回车使用 {default}）'
    elif allow_empty: hint += '（留空请直接回车）'
    if secret: hint += '（输入不回显）'
    if allow_back: hint += '（b 上一步）'
    hint += '（q 取消）'
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
            print('  ' + _tag('[提示]') + ' 已取消当前操作。')
            return None
        if allow_back and v in BACK_KEYS:
            return '__BACK__'
        if not v:
            if allow_empty: return ''
            if default is not None: return default
            print('  ' + _tag('[提示]') + ' 此项不能为空，请重新输入。')
            continue
        return v

def confirm(prompt, danger=False):
    if danger:
        answer = ask(f'{prompt}（确认请输入 YES，其他内容或回车取消）')
        return (answer or '').strip() == 'YES'
    return (ask(prompt + ' [y/N]（回车＝否）') or '').lower() in ('y', 'yes')

def clear_screen():
    if UI_CLEAR and _is_tty(): print('\033[2J\033[H', end='')

def section(title):
    print()
    print(RULE)
    print(' ' + title)
    print(RULE)

def pause(message='按回车返回上一级菜单'):
    if sys.stdin.isatty():
        try: input(f'  {message} … ')
        except EOFError: pass

def _report(title, lines):
    """Frame a multi-line report; keeps every original line intact."""
    inner = _ui_width()
    print()
    print('┌' + '─' * inner + '┐')
    print('│' + _pad_line(' ' + _tag('[提示]') + ' ' + title, inner) + '│')
    for raw in lines:
        for line in _wrap_text(str(raw), inner - 2):
            print('│' + _pad_line(' ' + line, inner) + '│')
    print('└' + '─' * inner + '┘')

def result_ok(message, detail=None):
    _box('[成功]', message, detail)

def result_info(message, detail=None):
    _box('[提示]', message, detail)

def result_error(message, hint=None):
    """Render a failure with the four elements of 需求 §11.

    reason (message) + suggestion (hint) + log path; impact is conveyed by the
    wording of each call site. The log path is appended once, automatically.
    """
    lines = []
    if hint: lines.append('建议：' + str(hint))
    try: lines.append('日志：' + error_log_path('frpc' if active() else 'frps'))
    except (ValueError, OSError): pass
    _box('[失败]', message, '\n'.join(lines) if lines else None)

def choose(items, title='请选择', back_label='返回上一级', groups=None):
    """Numbered menu with optional visual group headers (numbering stays continuous)."""
    section(title)
    idx = 0
    for n, item in enumerate(items, 1):
        if groups and (n - 1) in groups:
            print()
            print('  ' + groups[n - 1])
        print(f'  {n}) {item}')
        idx = n
    print(f'  0) {back_label}')
    while True:
        v = ask(f'请选择 [0-{idx}]')
        if v is None: return None
        if v.isdecimal() and 0 <= int(v) <= idx: return int(v)
        result_error('无效编号', f'请输入 0 到 {idx} 之间的数字，输入 q 取消。')

def require_active():
    pid = active()
    if not pid: raise ValueError('请先新建或选择 frps 服务端')
    load_profile(pid); return pid

def select_profile():
    ps = profiles()
    n = choose([label(p) + (' [当前]' if p['id'] == active() else '') + f' [{len(mappings(p["id"]))} 个映射]' for p in ps], '服务端列表')
    return ps[n-1]['id'] if n else None

def list_profiles():
    """Read-only view of all configured servers; no selection forced."""
    ps = profiles()
    section('服务端列表')
    if not ps:
        result_info('暂无服务端，请从主菜单「新建 frps 服务端」添加。')
    else:
        for p in ps:
            mark = ' [当前]' if p['id'] == active() else ''
            print(f'  • {label(p)}{mark}  [{len(mappings(p["id"]))} 个映射]')
    print(RULE)

def profile_wizard():
    """Step-by-step wizard with 'b' to go back and a review screen before commit."""
    p = {'admin_port': 7400}
    s = {'admin_password': secrets.token_urlsafe(32)}
    # (key, prompt, default, secret, post-processor)
    def checked_name(v):
        unique_name(v)
        return v
    steps = [
        ('name', '服务端名称', None, False, checked_name),
        ('server_addr', '服务端 IP/IPv6/域名', None, False, address),
        ('server_port', 'frps 业务端口', '7000', False, port),
        ('dashboard_port', 'Dashboard/API 管理端口', '7500', False, port),
        ('tls_enable', '启用 TLS？[Y/n]', 'y', False, lambda v: v.lower() != 'n'),
        ('token', 'token（不使用时留空）', '', True, lambda v: v),
        ('dashboard_username', 'Dashboard 用户名（留空关闭远程查询）', '', False, lambda v: v),
    ]
    i = 0
    while i < len(steps):
        key, prompt, default, secret, convert = steps[i]
        value = ask(prompt, default=default, secret=secret, allow_back=(i > 0))
        if value is None: return
        if value == '__BACK__':
            i -= 1
            continue
        try:
            if key == 'token': s[key] = convert(value)
            elif key == 'dashboard_username': s[key] = convert(value)
            else: p[key] = convert(value)
        except ValueError as exc:
            result_error(str(exc))
            continue
        i += 1
    # Dashboard sub-steps (only when a username was provided), also back-navigable.
    dash_steps = [
        ('dashboard_password', 'Dashboard 密码', '', True, lambda v: v),
        ('dashboard_addr', 'Dashboard 主机地址', p['server_addr'], False, lambda v: v),
        ('dashboard_scheme', 'Dashboard 协议 http/https', 'https', False, lambda v: v),
    ]
    if s['dashboard_username']:
        i = 0
        while i < len(dash_steps):
            key, prompt, default, secret, convert = dash_steps[i]
            value = ask(prompt, default=default, secret=secret, allow_back=True)
            if value is None: return
            if value == '__BACK__':
                if i == 0:
                    # Step back into the main wizard's last field.
                    back = ask('Dashboard 用户名（留空关闭远程查询）', default=s['dashboard_username'], allow_back=True)
                    if back is None: return
                    s['dashboard_username'] = back
                    if not s['dashboard_username']:
                        p.pop('dashboard_addr', None); p.pop('dashboard_scheme', None)
                        break
                    continue
                i -= 1
                continue
            if key == 'dashboard_password': s[key] = convert(value)
            else: p[key] = convert(value)
            i += 1
        if p.get('dashboard_scheme') == 'http' and not confirm('HTTP 会明文传输管理凭据，请使用可信网络或隧道，仍继续？'):
            return
    else:
        p['dashboard_addr'] = ''
        s['dashboard_password'] = ''
    validate_profile(p)
    section('确认创建')
    profile_summary(p, s)
    print(RULE)
    result_info('确认无误请输入 y 创建；直接回车或输入其他内容则取消。')
    if not confirm('以上信息确认无误，创建服务端？'):
        result_info('已取消，未创建服务端。')
        return
    pid = create_profile(p, s)
    if not active() or confirm('设为当前服务端？'): switch(pid)
    result_ok('服务端已创建。', detail=f'名称：{p["name"]}；地址：{p["server_addr"]}:{p["server_port"]}；端口映射请从主菜单「修改端口映射」单独设置。')
    if active() == pid and not running('frpc') and confirm('立即启动 frpc 并检查连接？'):
        service('frpc', 'start'); wait_connection(pid)

BACKUP_KEEP = 10

def backup(paths, reason='change'):
    """Snapshot managed files atomically, then prune to the newest BACKUP_KEEP.

    Writes into a staging directory and renames only after every file is
    copied and its size verified, so a failure never leaves a half backup
    visible to list_backups() (详细设计 §12.1 / §12.2).
    """
    base = ROOT / 'backups'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + reason + '-' + secrets.token_hex(3)
    staging = base / ('.staging-' + name)
    staging.mkdir(parents=True, mode=0o700)
    try:
        files, digests = [], {}
        for path in paths:
            path = Path(path)
            if not path.exists() or path.is_symlink(): continue
            try: relative = path.relative_to(ROOT)
            except ValueError: continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, target)
            if target.stat().st_size != path.stat().st_size:
                raise ValueError('备份文件大小不一致：' + str(relative))
            digests[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
            files.append(str(relative))
        atomic(staging / 'manifest.json', json.dumps(
            {'files': files, 'reason': reason, 'sha256': digests}, ensure_ascii=False, indent=2) + '\n')
        final = base / name
        os.replace(str(staging), str(final))
    except BaseException:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise
    _prune_backups(base)
    return final

def _prune_backups(base):
    """Keep only the newest BACKUP_KEEP non-upgrade backups (详细设计 §12.2)."""
    try:
        items = [p for p in base.iterdir()
                 if p.is_dir() and not p.is_symlink() and not p.name.startswith('.')]
    except OSError:
        return
    keep, drop = [], []
    for path in sorted(items, key=lambda p: p.stat().st_mtime, reverse=True):
        # Upgrade snapshots are independent high-risk backups (§12.4): never prune.
        if '-upgrade-' in path.name: continue
        (keep if len(keep) < BACKUP_KEEP else drop).append(path)
    for path in drop:
        shutil.rmtree(str(path), ignore_errors=True)

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
            if not _within(target, ROOT): raise ValueError('目标路径非法')
            atomic(target, source.read_text(encoding='utf-8'))
            restored.append(str(relative))
        rebuild_index(); audit('backup-restored')
    return restored

def backup_menu():
    clear_screen()
    while True:
        items = list_backups()[:20]
        labels = [f'{item["time"]}  {item["reason"]}  文件 {len(item["files"])} 个' for item in items]
        if not items:
            result_info('暂无备份可恢复。')
            pause()
            return
        n = choose(labels, '配置备份与恢复')
        if not n: return
        chosen = items[n-1]
        result_info('将恢复：' + str(chosen['path']), detail='\n'.join('• ' + r for r in chosen['files'][:20]))
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
        result = connection(p['id'], fresh=True)
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
            result = connection(pid, fresh=True)
            lines.append('  连接状态：' + result['state'])
            if result.get('detail'): lines.append('  详情：' + result['detail'])
            if result['state'] not in ('connected',): ok = False
            if state().get('pending'): lines.append('  注意：存在尚未重启生效的配置修改。')
            for m in mappings(pid):
                if m.get('type') in ('tcp', 'udp') and m.get('remote_port'):
                    if check_port_conflict('0.0.0.0', m['remote_port'], proto=m['type']): lines.append(f'  注意：远程端口 {m["remote_port"]} 在本机被占用（若服务端与本机不同可忽略）。')
    _report('连接诊断报告' if role == 'frpc' else '服务端诊断报告', lines[1:])
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
    before_count = len(mappings(pid))
    with lock(), transaction():
        if pid != active(): raise ValueError('当前服务端已在另一终端切换，请重新打开映射菜单')
        for m in items: validate_mapping(m)
        path = pdir(pid) / 'mappings.toml'; old = path.read_text(encoding='utf-8')
        backup([path]); atomic(path, toml({'schema_version': 1, 'mappings': items}))
        try: mappings(pid); apply(pid)
        except Exception: atomic(path, old); raise
        audit('mappings-saved', pid)
    result_ok('端口映射配置已保存。',
              detail=(f'映射数量：{before_count} → {len(items)}；'
                      + ('当前 frpc 运行中，请按提示重启后生效。' if running('frpc') else '当前 frpc 未运行，下次启动时生效。')))
    if running('frpc') and confirm('配置已保存，立即重启 frpc 生效？'):
        service('frpc', 'restart'); wait_connection(pid)

def mapping_wizard(old=None):
    """Step-by-step mapping wizard; returns None when cancelled with q.

    A single invalid field re-prompts that field instead of aborting the whole
    wizard (需求 §13.1 / 详细 §15.2(1)).
    """
    m = dict(old or {})
    fields = [
        ('name', '映射名称', None, lambda v: v),
        ('type', '类型 tcp/udp/http/https', 'tcp', lambda v: v.lower()),
        ('local_ip', '本地地址', '127.0.0.1', lambda v: v),
        ('local_port', '本地端口', None, port),
        ('remote_or_domain', '远程端口/域名', None, lambda v: v),
        ('enabled', '启用？[Y/n]', 'y', lambda v: v.lower() != 'n'),
    ]
    while True:
        i = 0
        while i < len(fields):
            key, prompt, default, convert = fields[i]
            value = ask(prompt, default=default if default is not None else m.get(key),
                        allow_back=(i > 0))
            if value is None: return None
            if value == '__BACK__':
                i = max(0, i - 1) if i > 0 else 0
                continue
            try:
                if key == 'remote_or_domain':
                    if m.get('type') in ('tcp', 'udp'):
                        m['remote_port'] = port(value); m.pop('domain', None)
                    else:
                        if not value: raise ValueError('HTTP/HTTPS 必须填写域名')
                        m['domain'] = value; m.pop('remote_port', None)
                else:
                    m[key] = convert(value)
            except ValueError as exc:
                result_error(str(exc)); continue   # re-ask this field only
            i += 1
        try:
            validate_mapping(m)
            return m
        except ValueError as exc:
            # Whole-object validation failed: report and restart the wizard
            # rather than silently discarding the user's input.
            result_error(str(exc), hint='请重新填写该映射。')
            continue

def show_mappings(items):
    if not items:
        result_info('当前服务端暂无端口映射。'); return
    section('当前服务端端口映射')
    print('  ' + f'{"序号":<4}{"名称":<16}{"类型":<6}{"本地":<22}{"远端":<20}{"状态"}')
    for n, m in enumerate(items, 1):
        local = f'{m.get("local_ip", "127.0.0.1")}:{m["local_port"]}'
        if m['type'] in ('tcp', 'udp'): remote = str(m.get('remote_port', '-'))
        else: remote = m.get('domain', '-')
        enabled = '启用' if m.get('enabled', True) else '停用'
        print('  ' + f'{n:<4}{m["name"]:<16}{m["type"]:<6}{local:<22}{remote:<20}{enabled}')
    print(RULE)

def mapping_menu():
    clear_screen()
    while True:
        pid = require_active(); result_info('当前 frps：' + label(load_profile(pid)))
        n = choose(['新增端口映射', '查看当前服务端映射', '修改端口映射', '启用/停用端口映射', '删除端口映射'])
        if not n: return
        try:
            items = mappings(pid)
            before_count = len(items)
            if n == 1:
                created = mapping_wizard()
                if created is None: continue
                items.append(created); save_mappings(pid, items); pause(); continue
            elif n == 2: show_mappings(items); pause(); continue
            else:
                index = choose([m['name'] + (' [启用]' if m.get('enabled', True) else ' [停用]') for m in items], '选择映射')
                if not index: continue
                if n == 3:
                    edited = mapping_wizard(items[index-1])
                    if edited is None: continue
                    items[index-1] = edited
                elif n == 4:
                    old_state = items[index-1].get('enabled', True)
                    items[index-1]['enabled'] = not old_state
                    result_info('已切换映射状态：' + ('启用 → 停用' if old_state else '停用 → 启用') + '。')
                elif n == 5:
                    name = items[index-1]['name']
                    if not confirm('确认删除映射 ' + name + '？此操作不可撤销', danger=True):
                        result_info('已取消删除。'); continue
                    items.pop(index-1)
                    result_info(f'已删除映射 {name}；映射数量：{before_count} → {len(items)}。')
                save_mappings(pid, items); result_ok('端口映射操作已完成。', detail=f'映射数量：{before_count} → {len(items)}')
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
    print('  映射数量：' + str(len(mappings(p['id'])) if p.get('id') else 0))

def edit_profile():
    clear_screen()
    pid = require_active(); p = load_profile(pid); s = secrets_for(pid)
    version = hashlib.sha256((pdir(pid) / 'profile.toml').read_bytes() + (pdir(pid) / 'secrets.toml').read_bytes()).hexdigest()

    def store(mapping, key, value):
        """Skip writes when the user cancelled (q -> None); never persist None."""
        if value is not None: mapping[key] = value

    def field(action):
        """Run one field edit; a validation error must not end the session."""
        try:
            action()
            return True
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result_error(str(exc), hint='该项未修改，其他已修改内容仍保留；可重新选择。')
            return False

    while True:
        n = choose(['查看连接配置', '修改服务端名称', '修改服务器地址', '修改服务器端口', '修改 token', '修改 TLS 配置', '修改 Dashboard/API', '修改传输协议', '修改日志级别与保留天数', '保存并返回', '删除当前服务端及全部映射'], label(p))
        if not n: return  # 0 = back, None = q cancel
        if n == 1:
            section('连接配置')
            profile_summary(p, s)
            print(RULE)
            pause()
        elif n == 2:
            v = ask('新名称')
            if v is not None: field(lambda: (unique_name(v, pid), p.__setitem__('name', v)))
        elif n == 3:
            v = ask('新地址')
            if v is not None: field(lambda: p.__setitem__('server_addr', address(v)))
        elif n == 4:
            v = ask('业务端口')
            if v is not None: field(lambda: p.__setitem__('server_port', port(v)))
        elif n == 5:
            store(s, 'token', ask('新 token（留空清除）', secret=True, allow_empty=True))
        elif n == 6:
            v = ask('启用 TLS？[Y/n]', 'y')
            if v is not None:
                p['tls_enable'] = v.lower() != 'n'
                for key in ('certFile', 'keyFile', 'trustedCaFile'):
                    store(s, 'tls_' + key, ask('可选 ' + key + ' 路径', s.get('tls_' + key, ''), allow_empty=True))
        elif n == 7:
            v = ask('Dashboard/API 端口', '7500')
            if v is not None:
                field(lambda: p.__setitem__('dashboard_port', port(v)))
                store(p, 'dashboard_addr', ask('Dashboard 主机', p['server_addr'], allow_empty=True))
                store(s, 'dashboard_username', ask('Dashboard 用户名（留空关闭）', allow_empty=True))
                if s.get('dashboard_username'):
                    store(s, 'dashboard_password', ask('密码', secret=True, allow_empty=True))
                else:
                    s['dashboard_password'] = ''
        elif n == 8:
            v = ask('传输协议 tcp/kcp/quic/websocket/wss', p.get('transport_protocol', 'tcp'))
            if v is not None:
                protocol = v.lower()
                if protocol not in ('tcp', 'kcp', 'quic', 'websocket', 'wss'): raise ValueError('不支持的传输协议：' + protocol)
                p['transport_protocol'] = protocol
                v2 = ask('启用传输加密？[y/N]', 'y' if p.get('transport_use_encryption') else 'n')
                if v2 is not None: p['transport_use_encryption'] = v2.lower() == 'y'
                v3 = ask('启用传输压缩？[y/N]', 'y' if p.get('transport_use_compression') else 'n')
                if v3 is not None: p['transport_use_compression'] = v3.lower() == 'y'
        elif n == 9:
            v = ask('日志级别 trace/debug/info/warn/error', p.get('log_level', 'info'))
            if v is not None:
                level = v.lower()
                if level not in ('trace', 'debug', 'info', 'warn', 'error'): raise ValueError('不支持的日志级别：' + level)
                p['log_level'] = level
                days = ask('日志保留天数（1-365）', str(p.get('log_max_days', 30)))
                if days is not None:
                    if not days.isdigit() or not 1 <= int(days) <= 365: raise ValueError('日志保留天数必须为 1-365')
                    p['log_max_days'] = int(days)
        elif n == 11:
            # Reuse the single delete implementation (danger confirm + audit +
            # post-delete guidance) instead of duplicating it here.
            delete_profile_id(pid)
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
    addr = ask('业务监听地址', '0.0.0.0')
    if addr is None: return
    bind_port = ask('业务端口', '7000')
    if bind_port is None: return
    suggested = secrets.token_urlsafe(24)
    token = ask('token（留空自动生成强 token；无认证请确认后留空并输入 none）', secret=True, allow_empty=True)
    if token is None: return
    if token == '':
        token = suggested
        result_info('已自动生成随机 token（长度 %d）。请在 frpc 侧填写同一 token。' % len(token))
    elif token.lower() == 'none':
        if not confirm('确认在无 token 模式下运行 frps？任何客户端都可接入。', danger=True): return
        token = ''
    data = {'bindAddr': address(addr), 'bindPort': port(bind_port),
            'auth.method': 'token', 'auth.token': token,
            'log.to': str(ROOT / 'logs' / 'frps.log'), 'log.maxDays': 30, 'log.disablePrintColor': True}
    if data['auth.token'] and len(data['auth.token']) < 8:
        raise ValueError('token 长度至少 8 个字符（需求 §7.3 强制最小长度）')
    if address(addr) not in ('127.0.0.1', '::1'):
        result_info('业务监听 ' + addr + ' 将对公网/局域网开放，请确认防火墙与安全组规则。')
    user = ask('Dashboard 用户名（留空关闭）', allow_empty=True)
    if user is None: return
    if user:
        host_raw = ask('Dashboard 监听地址', '0.0.0.0')
        if host_raw is None: return
        host = address(host_raw)
        if host not in ('127.0.0.1', '::1') and not confirm('管理接口将对外监听，确认？'): return
        password = ask('Dashboard 密码', secret=True)
        if password is None: return
        if not password: raise ValueError('启用 Dashboard 时密码不能为空')
        port_raw = ask('Dashboard 端口', '7500')
        if port_raw is None: return
        if port(port_raw) == data['bindPort']: raise ValueError('Dashboard 端口不能与业务端口相同')
        data.update({'webServer.addr': host, 'webServer.port': port(port_raw), 'webServer.user': user, 'webServer.password': password})
    if confirm('启用 TLS（transport.tls）？生产环境建议启用'):
        cert = ask('TLS 证书路径', '', allow_empty=True)
        if cert is None: return
        if cert:
            key = ask('TLS 私钥路径', '', allow_empty=True)
            if key is None: return
            data['transport.tls.certFile'] = cert
            data['transport.tls.keyFile'] = key
    for key in ('vhostHTTPPort', 'vhostHTTPSPort'):
        value = ask(key + '（留空禁用）', allow_empty=True)
        if value is None: return
        if value: data[key] = port(value)
    # Validate BEFORE writing so the wizard cannot persist an invalid config (R7).
    validate_frps_config(expand_keys(data))
    content = toml(data); verify('frps', content)
    with lock(), transaction(): backup([ROOT / 'frps.toml']); atomic(ROOT / 'frps.toml', content); mark_hash('frps')
    if running('frps') and confirm('立即重启 frps？'):
        service_feedback('frps', 'restart')
    else:
        result_info('frps 配置已保存；当前运行服务将在下次重启后使用新配置。')

def check_port_conflict(host, port_value, proto='tcp', exclude_pid=None):
    """Return True when another local process already listens on host:port/protocol."""
    if not shutil.which('ss'): return None
    result = run_cmd(['ss', '-lntuH'], capture_output=True, text=True, timeout=10)
    needle = ':' + str(port_value)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5: continue
        netid = fields[0].lower()
        if proto and not netid.startswith(proto): continue
        local = fields[4]
        if not local.endswith(needle): continue
        if host not in ('0.0.0.0', '::', '') and not local.startswith(host): continue
        if exclude_pid and ('pid=' + str(exclude_pid)) in line: continue
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

def expand_keys(data):
    """Expand flat dotted keys into nested dicts (auth.token -> {auth:{token:...}})."""
    result = {}
    for key, value in data.items():
        if isinstance(key, str) and '.' in key:
            node = result
            parts = key.split('.')
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict): break
            else:
                node[parts[-1]] = value
            continue
        result[key] = value
    return result

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
        # Dashboard 与业务端口必须分离（需求 §7.3）。
        if int(web['port']) == int(data['bindPort']): raise ValueError('Dashboard 端口不能与业务端口相同')
    # TLS（需求 §7.1-6 / §15.2）：启用时必须能读到证书与私钥。
    tls = data.get('transport', {}).get('tls', {}) if isinstance(data.get('transport'), dict) else {}
    if tls.get('certFile') or tls.get('keyFile'):
        if not (tls.get('certFile') and tls.get('keyFile')): raise ValueError('启用 TLS 时必须同时提供证书与私钥路径')
        for label, value in (('证书', tls.get('certFile')), ('私钥', tls.get('keyFile'))):
            if not Path(value).is_file(): raise ValueError('TLS ' + label + '文件不存在或不可读')
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
        audit('frps-config-saved')
        result_ok('frps 配置已保存；下次重启后生效。')

def redact_secrets(obj):
    """Redact secret fields by name, regardless of nesting or dotted keys."""
    if isinstance(obj, dict):
        return {k: ('******' if k.lower() in ('token', 'password') and isinstance(v, str) and v
                    else redact_secrets(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_secrets(x) for x in obj]
    return obj

def frps_config_menu():
    items = ['查看当前配置（脱敏）', '修改业务监听地址/端口', '修改 token', '修改 Dashboard/API',
             '修改 HTTP/HTTPS vhost 端口', '修改 KCP/QUIC/TCPMUX 端口', '修改 TLS 证书',
             '校验配置并检测端口冲突', '查看防火墙放行提示', '备份与恢复配置']
    clear_screen()
    while True:
        try: data = read_frps_config()
        except (ValueError, OSError) as exc: result_error(str(exc)); pause(); return
        web = data.get('webServer', {})
        n = choose(items, 'frps 服务端配置')
        if not n: return
        try:
            if n == 1:
                section('当前配置（脱敏）')
                print(json.dumps(redact_secrets(data), ensure_ascii=False, indent=2))
                print(RULE)
                pause()
            elif n == 2:
                v1 = ask('业务监听地址', data.get('bindAddr', '0.0.0.0'))
                if v1 is None: continue
                v2 = ask('业务端口', str(data.get('bindPort', 7000)))
                if v2 is None: continue
                data['bindAddr'] = address(v1); data['bindPort'] = port(v2)
                save_frps_config(data)
            elif n == 3:
                value = ask('新 token（留空自动生成强 token；输入 none 表示无认证）', secret=True, allow_empty=True)
                if value is None: continue
                if value == '':
                    value = secrets.token_urlsafe(24)
                    result_info('已自动生成随机 token（长度 %d）。' % len(value))
                elif value.lower() == 'none':
                    if not confirm('确认在无 token 模式下运行 frps？任何客户端都可接入。', danger=True): continue
                    value = ''
                elif len(value) < 8:
                    raise ValueError('token 长度至少 8 个字符')
                auth = data.setdefault('auth', {}); auth['method'] = 'token'; auth['token'] = value
                save_frps_config(data)
            elif n == 4:
                user = ask('Dashboard 用户名（留空关闭 Dashboard）', allow_empty=True)
                if user is None: continue
                if not user:
                    data.pop('webServer', None)
                else:
                    host_raw = ask('Dashboard 监听地址', web.get('addr', '0.0.0.0'))
                    if host_raw is None: continue
                    host = address(host_raw)
                    if host not in ('127.0.0.1', '::1') and not confirm('管理接口将对外监听，确认？'): continue
                    password = ask('Dashboard 密码（留空保留原密码）', secret=True, allow_empty=True)
                    if password is None: continue
                    password = password or web.get('password', '')
                    if not password: raise ValueError('Dashboard 密码不能为空')
                    port_raw = ask('Dashboard 端口', str(web.get('port', 7500)))
                    if port_raw is None: continue
                    # Preserve unknown webServer sub-tables (tls, pprof, ...).
                    web_new = data.setdefault('webServer', {})
                    if not isinstance(web_new, dict): web_new = {}; data['webServer'] = web_new
                    web_new.update({'addr': host, 'port': port(port_raw), 'user': user, 'password': password})
                save_frps_config(data)
            elif n == 5:
                for key in ('vhostHTTPPort', 'vhostHTTPSPort'):
                    value = ask(key + '（留空禁用）', str(data.get(key, '')), allow_empty=True)
                    if value is None: break
                    if value: data[key] = port(value)
                    else: data.pop(key, None)
                else:
                    save_frps_config(data)
            elif n == 6:
                for key in ('kcpBindPort', 'quicBindPort', 'tcpmuxHTTPConnectPort'):
                    value = ask(key + '（留空禁用）', str(data.get(key, '')), allow_empty=True)
                    if value is None: break
                    if value: data[key] = port(value)
                    else: data.pop(key, None)
                else:
                    save_frps_config(data)
            elif n == 7:
                tls = data.setdefault('transport', {}).setdefault('tls', {})
                if tls.get('certFile'):
                    if not confirm('当前已启用 TLS，是否清除证书配置？'): continue
                    for key in ('certFile', 'keyFile', 'trustedCaFile'): tls.pop(key, None)
                    if not tls: data.pop('transport', None)
                else:
                    cert = ask('TLS 证书路径（PEM）', '', allow_empty=True)
                    if cert is None: continue
                    if not cert:
                        result_info('未填写证书，跳过 TLS 配置。'); continue
                    key = ask('TLS 私钥路径', '', allow_empty=True)
                    if key is None: continue
                    tls['certFile'] = cert; tls['keyFile'] = key
                save_frps_config(data)
            elif n == 8:
                validate_frps_config(data); verify('frps', toml(data))
                result_ok('配置语法和字段校验通过。')
                for key in ('bindPort', 'vhostHTTPPort', 'vhostHTTPSPort', 'kcpBindPort', 'quicBindPort', 'tcpmuxHTTPConnectPort'):
                    if data.get(key) and check_port_conflict('0.0.0.0', data[key]): result_error(f'{key}={data[key]} 已被其他进程监听。')
                if web.get('port') and check_port_conflict('127.0.0.1', web['port']): result_error(f'Dashboard 端口 {web["port"]} 已被占用。')
                pause()
            elif n == 9:
                ports = [('bindPort', 'tcp')] + [('vhostHTTPPort', 'tcp'), ('vhostHTTPSPort', 'tcp')]
                found = False
                for key, proto in ports:
                    if data.get(key):
                        for hint in firewall_hint(data[key], proto): result_info(hint); found = True
                if not found: result_info('未发现需要处理的常见防火墙规则提示；云安全组请自行确认。')
                pause()
            elif n == 10: backup_menu()
        except (ValueError, OSError, subprocess.SubprocessError) as exc: result_error(str(exc)); pause()

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
    lines = ['在线客户端（Dashboard/API）']
    if not clients: lines.append('暂无客户端连接。')
    for n, item in enumerate(clients, 1):
        state = '在线' if item.get('online') else '离线'
        lines.append(f'{n}. {item.get("hostname") or item.get("clientID") or item.get("key", "未知")} [{state}]')
        lines.append(f'   clientID={item.get("clientID", "")} 版本={item.get("version", "")} IP={item.get("clientIP", "")}')
        lines.append(f'   代理数量={item.get("status", {}).get("proxyCount", "未知")}')
    lines.append('代理状态')
    if not proxies: lines.append('暂无代理统计。')
    for n, item in enumerate(proxies, 1):
        status = item.get('status', {})
        lines.append(f'{n}. {item.get("name", "未知")} [{status.get("phase", "未知")}] 类型={item.get("spec", {}).get("type", "未知")}')
        lines.append(f'   今日流量：入站 {status.get("todayTrafficIn", 0)} / 出站 {status.get("todayTrafficOut", 0)}，连接数 {status.get("curConns", 0)}')
    _report('客户端连接信息', lines)
    result_info(f'客户端连接查询完成：{len(clients)} 个客户端，{len(proxies)} 个代理。')

def import_external(role):
    path = ROOT / (role + '.toml'); s = state()
    if not path.exists(): return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if s.get(role + '_hash') == digest: return
    result_info('检测到外部运行配置修改。')
    n = choose(['导入外部修改', '保留管理器配置', '查看变更字段（隐藏敏感值）'], '配置同步')
    if n is None: raise ValueError('已取消；外部文件保持原样')
    if n == 0: return
    config = read_toml(path) if n != 2 else {}
    if n == 3:
        result_info('外部配置字段（仅字段名）：' + ', '.join(config)); return import_external(role)
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
        if confirm('确认覆盖外部运行配置？'):
            with lock(), transaction():
                if pid != active(): raise ValueError('当前服务端已切换，请重新进入')
                apply(pid)
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
        for required in ('name', 'type', 'localPort'):
            if required not in x: raise ValueError('外部配置映射缺少字段 %s，原文件保留' % required)
        m = {'name': x['name'], 'type': x['type'], 'local_ip': x.get('localIP', '127.0.0.1'), 'local_port': x['localPort'], 'enabled': x.get('enabled', True)}
        if m['type'] in ('tcp', 'udp'):
            if 'remotePort' not in x: raise ValueError('外部配置 TCP/UDP 映射缺少 remotePort，原文件保留')
            m['remote_port'] = x['remotePort']
        else:
            if len(x.get('customDomains', [])) != 1: raise ValueError('首期仅支持单域名映射导入，原文件保留')
            m['domain'] = x['customDomains'][0]
        validate_mapping(m); rows.append(m)
    for required in ('serverAddr', 'serverPort'):
        if required not in config: raise ValueError('外部配置缺少 %s，原文件保留' % required)
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
    result_ok('外部修改已导入当前 profile。')

def status(role):
    result = {'component': role, 'version': VERSION, 'service_state': service(role, 'status'), 'config_path': str(ROOT / (role + '.toml'))}
    if role == 'frpc' and active():
        try:
            result.update({'active_profile': load_profile(active()), 'connection': connection(active()), 'pending': state().get('pending', False)})
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            # Distinguish "no profile selected" from "profile is broken": the
            # latter must surface as config-error with a diagnostic, not silently.
            result.update({'active_profile': None, 'profile_error': str(exc),
                           'connection': {'state': 'config-error', 'detail': str(exc)}, 'pending': False})
    return result

def status_bar(role, data):
    """One-line compact status shown above every menu."""
    state_text = SERVICE_STATES.get(str(data.get('service_state', '')).lower(), data.get('service_state', '未知'))
    parts = [role, _state_color(state_text, state_text == '运行中'), str(data.get('version', ''))]
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
            conn = data.get('connection', {}).get('state', '')
            translated = CONNECTION_STATES.get(conn, conn)
            try: count = str(len(mappings(profile['id']))) + ' 个映射'
            except (ValueError, OSError, KeyError, TypeError): count = '映射数未知'
            parts += [label(profile), _state_color(translated, conn == 'connected'), count]
        else:
            parts.append('未选择服务端' if not data.get('profile_error') else '配置异常')
    if data.get('pending'):
        parts.append('有待重启修改')
    print('  ' + ' | '.join(str(p) for p in parts if p))

def show_status(role, data=None):
    """Render a status screen. Accepts a precomputed status dict so the menu
    does not pay for status() (and its API probes) twice."""
    if data is None: data = status(role)
    section('frps 服务端管理' if role == 'frps' else 'frpc 客户端管理')
    print('  ' + '状态'.ljust(10) + SERVICE_STATES.get(str(data['service_state']).lower(), data['service_state']))
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
        if p:
            print('  ' + '服务端'.ljust(10) + label(p))
            connection_data = data['connection']
            print('  ' + '连接状态'.ljust(10) + CONNECTION_STATES.get(connection_data['state'], connection_data['state']))
            try: print('  ' + '映射数量'.ljust(10) + str(len(mappings(p['id']))))
            except (ValueError, OSError, KeyError, TypeError): print('  ' + '映射数量'.ljust(10) + '不可读')
            if data.get('pending'): print('  ' + '生效'.ljust(10) + '有修改等待重启')
    # Surface only what needs attention; normal details stay compact.
    concerns = []
    state = str(data['service_state']).lower()
    if state in ('failed', 'inactive', 'stopped'):
        concerns.append('服务未运行，可先用「启动」或查看日志排查')
    if role == 'frpc':
        conn = data.get('connection', {}).get('state', '')
        if conn in ('auth-failed', 'tls-failed', 'proxy-error', 'timeout', 'connecting', 'config-error', 'unreachable', 'disconnected'):
            concerns.append('连接未就绪（' + CONNECTION_STATES.get(conn, conn) + '）：检查 token / TLS / 服务端可达性')
        if data.get('profile_error'):
            concerns.append('当前服务端配置损坏：' + str(data['profile_error']) + '；可从「备份与恢复配置」恢复或重新选择服务端')
        if data.get('pending'):
            concerns.append('有配置修改等待重启后生效')
    if concerns:
        print()
        print('  ' + _tag('[提示]') + ' 需关注：')
        for c in concerns: print('    - ' + c)
    print(RULE)
    return data

def service_feedback(role, action):
    """Actually perform the service action, then report the resulting state."""
    labels = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    service(role, action)
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
            profile = data.get('active_profile') or {}
            alias = profile.get('name', '未选择') if isinstance(profile, dict) else '未选择'
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
            result_error('配置无法读取；请检查配置文件或从 backups 恢复，服务管理仍可使用。')
            alias = '配置错误'
            data = {'component': role, 'version': VERSION, 'service_state': 'unknown', 'config_path': str(ROOT / (role + '.toml')), 'pending': False}
        if role == 'frpc':
            items = ['查看当前状态', '启动 frpc', '停止 frpc', '重启 frpc', '新建 frps 服务端', '查看服务端列表', '切换服务端', '修改当前 frps 配置', f'修改当前 frps（{alias}）端口映射', '查看日志与连接诊断', '备份与恢复配置', '设置/取消开机自启动', '卸载 frpc', '升级 frpc']
            groups = {0: '── 服务控制 ──', 4: '── 服务端与映射 ──', 9: '── 运维 ──', 12: '── 危险操作 ──'}
        else:
            items = ['查看服务信息（状态、版本、路径）', '启动 frps', '停止 frps', '重启 frps', '查看客户端连接信息', '修改服务端配置', '查看日志与连接诊断', '备份与恢复配置', '设置/取消开机自启动', '卸载 frps', '升级 frps']
            groups = {0: '── 服务控制 ──', 4: '── 配置与连接 ──', 6: '── 运维 ──', 9: '── 危险操作 ──'}
        status_bar(role, data)
        n = choose(items, '请选择操作' if role == 'frps' else 'frpc 客户端操作', '退出', groups=groups)
        if not n: return
        # Sub-menus pause themselves; every other result gets exactly one
        # pause so the user can actually read it before the menu redraws.
        # Terminal operations (uninstall/upgrade) leave the menu entirely.
        needs_pause = True
        terminal = False
        try:
            if role == 'frpc':
                if n == 1: show_status(role, data)
                elif n in (2, 3, 4):
                    require_active()
                    if n == 2 or confirm(items[n-1] + '？'):
                        action = {2:'start', 3:'stop', 4:'restart'}[n]
                        service_feedback(role, action)
                        if n != 3: wait_connection(active())
                        else: result_info('frpc 已停止，当前配置和 profile 保留。')
                elif n == 5: profile_wizard()
                elif n == 6: list_profiles()
                elif n == 7:
                    pid = select_profile()
                    if pid: switch(pid)
                    else: needs_pause = False  # user pressed 0 to go back; don't ask again
                elif n == 8: edit_profile(); needs_pause = False
                elif n == 9: mapping_menu(); needs_pause = False
                elif n == 10: logs_and_diagnostics_menu(role); needs_pause = False
                elif n == 11: backup_menu(); needs_pause = False
                elif n == 12:
                    if not autostart(role): needs_pause = False
                elif n == 13: uninstall_menu(role); terminal = True
                elif n == 14: upgrade(role); terminal = True
            else:
                if n == 1:
                    result_info('正在刷新服务信息。')
                    show_status(role, data)
                elif n in (2,3,4):
                    if n == 2 or confirm(items[n-1] + '？'):
                        service_feedback(role, {2:'start',3:'stop',4:'restart'}[n])
                elif n == 5: server_clients()
                elif n == 6: frps_config_menu(); needs_pause = False
                elif n == 7: logs_and_diagnostics_menu(role); needs_pause = False
                elif n == 8: backup_menu(); needs_pause = False
                elif n == 9:
                    if not autostart(role): needs_pause = False
                elif n == 10: uninstall_menu(role); terminal = True
                elif n == 11: upgrade(role); terminal = True
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as e: result_error(str(e))
        if terminal:
            return
        if needs_pause:
            pause()

def log_files(role):
    base = ROOT / 'logs'
    if not base.exists(): return []
    return sorted([p for p in base.iterdir() if p.is_file() and not p.is_symlink() and (p.name == role + '.log' or p.name.startswith(role + '.log.'))], key=lambda p: p.stat().st_mtime, reverse=True)

def show_logs(role, lines=50):
    path = ROOT / 'logs' / (role + '.log')
    if path.exists():
        content = path.read_text(encoding='utf-8', errors='replace').splitlines()
        section(f'{role} 日志（最近 {min(lines, len(content))} 行）')
        print('\n'.join(content[-lines:]))
        print(RULE)
        result_info(f'{path} 共 {len(content)} 行，已显示最近 {min(lines, len(content))} 行。')
        return
    if os.name != 'nt' and shutil.which('journalctl'):
        run_cmd(['journalctl', '-u', role, '-n', str(lines), '--no-pager'])
        result_info(f'已从 journalctl 读取最近 {lines} 行。')
        return
    result_info('暂无文件日志；请查看平台服务日志。')

def follow_log(role, lines=50):
    """Tail the log until Ctrl-C returns (需求 §7.1-7 持续跟踪)."""
    path = ROOT / 'logs' / (role + '.log')
    if not path.exists():
        result_info('暂无文件日志；请查看平台服务日志。')
        return
    content = path.read_text(encoding='utf-8', errors='replace').splitlines()
    if content: print('\n'.join(content[-lines:]))
    result_info('持续跟踪中，按 Ctrl-C 返回菜单。')
    try:
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            handle.seek(0, os.SEEK_END)
            while True:
                chunk = handle.readline()
                if chunk: print(chunk, end='')
                else: time.sleep(0.5)
    except KeyboardInterrupt:
        result_info('已停止跟踪。')

def logs_and_diagnostics_menu(role):
    """Combined, single sub-menu for logs and connection diagnostics."""
    clear_screen()
    while True:
        files = log_files(role)
        n = choose(['查看最近 50 行日志', '查看最近 200 行日志', '持续跟踪日志（Ctrl-C 返回）',
                    '查看日志目录与轮转策略', '清理轮转归档（保留当前日志）', '运行连接诊断',
                    '导出诊断报告（JSON）'], '日志与连接诊断', '返回上一级')
        if not n: return
        if n == 1: show_logs(role, 50); pause()
        elif n == 2: show_logs(role, 200); pause()
        elif n == 3: follow_log(role, 50); pause()
        elif n == 4:
            result_info('日志目录：' + str(ROOT / 'logs'), detail='轮转策略：单文件超过 50MB 自动归档；保留 30 天；总量上限 500MB。'); pause()
        elif n == 5:
            removed = 0
            for item in files:
                if item.name == role + '.log': continue
                unlink_quiet(item); removed += 1
            result_ok(f'已清理 {removed} 个轮转归档，当前日志保留。'); pause()
        elif n == 6: diagnostics(role); pause()
        elif n == 7:
            report = diagnostics_report(role)
            section('诊断报告（JSON）')
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print(RULE)
            pause()

def autostart(role):
    n = choose(['启用开机自启动', '取消开机自启动'])
    if not n: return False
    action = 'enable' if n == 1 else 'disable'
    service(role, action)
    current = service(role, 'status')
    result_ok(('已启用' if n == 1 else '已取消') + f' {role} 开机自启动；当前运行状态：{current or "未知"}。')
    return True

def _clear_runtime_state():
    """Drop stale runtime markers after the active profile disappears (F6)."""
    s = state()
    for key in ('pending', 'frpc_hash', 'frpc_log_offset'):
        s.pop(key, None)
    save_json(ROOT / 'state.json', s)

def _after_profile_removed(was_active):
    """Guide the user after deleting a profile (概要 §6.6 / 详细 §12.3 step 6)."""
    remaining = profiles()
    if not was_active:
        result_ok('服务端 profile 及其映射已删除。')
        return
    if remaining:
        names = '、'.join(p['name'] for p in remaining)
        result_info('已删除当前服务端。剩余服务端：' + names,
                    detail='请从主菜单「切换服务端」选择一个，或「新建 frps 服务端」。')
    else:
        result_info('已删除当前服务端，当前没有可用服务端。',
                    detail='请从主菜单「新建 frps 服务端」创建后再启动 frpc。')

def delete_profile_id(pid):
    p = load_profile(pid)
    was_active = pid == active()
    if was_active and running('frpc'):
        if not confirm('删除当前服务端将停止 frpc，确认继续？', danger=True): return
        service('frpc', 'stop')
    # Two-step danger confirmation (需求 §6.2): mention mappings + no backup.
    if not confirm('删除 ' + label(p) + ' 及其全部端口映射？此操作不额外备份，不可撤销', danger=True): return
    with lock(), transaction():
        folder = pdir(pid)
        if folder.parent != ROOT / 'profiles': raise ValueError('删除路径错误')
        shutil.rmtree(folder)
        if was_active:
            set_current(''); unlink_quiet(ROOT / 'frpc.toml')
            _clear_runtime_state()
        rebuild_index(); audit('profile-deleted')
    _after_profile_removed(was_active)

def mapping_command(argv):
    pid = require_active(); items = mappings(pid); action = argv[0] if argv else 'menu'
    if action == 'list':
        print(json.dumps(items, ensure_ascii=False, indent=2)); return
    if action == 'add':
        created = mapping_wizard()
        if created is None: return
        items.append(created); save_mappings(pid, items); return
    if action in ('edit', 'enable', 'disable', 'delete'):
        if len(argv) < 2: raise ValueError('需要映射名称：mapping ' + action + ' <name>')
        name_arg = argv[1]; index = next((i for i, item in enumerate(items) if item['name'] == name_arg), None)
        if index is None: raise ValueError('映射不存在：' + name_arg)
        if action == 'edit':
            edited = mapping_wizard(items[index])
            if edited is None: return
            items[index] = edited
        elif action == 'enable': items[index]['enabled'] = True
        elif action == 'disable': items[index]['enabled'] = False
        elif not confirm('确认删除映射 ' + name_arg + '？'): return
        else: items.pop(index)
        save_mappings(pid, items); return
    if action == 'menu': mapping_menu(); return
    raise ValueError('支持 mapping list/add/edit/enable/disable/delete')

def install(role, yes=False, no_start=False):
    script = Path(__file__).resolve().parent.parent / ('install-' + role + ('.ps1' if os.name == 'nt' else '.sh'))
    if not script.exists(): raise ValueError('安装脚本未安装')
    if os.name == 'nt':
        args = ['powershell', '-File', str(script), '-Version', 'v' + VERSION]
        if no_start: args += ['-NoStart']
    else:
        args = ['bash', str(script), '--version', VERSION]
        if yes: args.append('--yes')
        if no_start: args.append('--no-start')
    run_cmd(args, check=True); audit('installed')

def uninstall(role, purge=False, yes=False):
    script = Path(__file__).resolve().parent.parent / ('uninstall-windows.ps1' if os.name == 'nt' else 'uninstall.sh')
    if not script.exists(): raise ValueError('卸载脚本未安装')
    if not yes:
        message = '卸载 ' + role + '？' + ('将删除配置、映射、日志和备份（彻底清理）' if purge else '默认保留配置和日志。')
        if not confirm(message, danger=purge): return
    if os.name == 'nt':
        args = ['powershell', '-File', str(script), '-Component', role, '-Yes']
        if purge: args.append('-Purge')
    else:
        args = ['bash', str(script), role, '--yes']
        if purge: args.append('--purge')
    run_cmd(args, check=True); audit('uninstalled' + ('-purge' if purge else ''))

def uninstall_menu(role):
    """Let the user choose keep-config vs purge (需求 §7.1-8)."""
    choice = choose(['保留配置和日志（推荐）', '彻底清理（删除配置、映射、日志和备份）'], '卸载 ' + role)
    if not choice: return
    if choice == 1:
        uninstall(role, purge=False)
    else:
        if not confirm('彻底清理将删除 /etc/frp 下全部配置、映射、日志与备份，且不可恢复。确认继续？', danger=True):
            result_info('已取消。'); return
        uninstall(role, purge=True, yes=True)

SUPPORTED_VERSIONS = ('0.72.0',)

def installed_frp_version(role):
    """Version of the frp native binary."""
    try:
        result = run_cmd([str(binary(role)), '--version'], capture_output=True, text=True, timeout=10)
        value = (result.stdout or result.stderr).strip().splitlines()
        return value[0].strip() if value else 'unknown'
    except (OSError, subprocess.SubprocessError): return 'unknown'

def installed_manager_version():
    """Version of the management scripts, read from the installed bootstrap."""
    marker = INSTALL_ROOT / ('install-' + ('frpc' if os.name != 'nt' else 'frpc') + ('.sh' if os.name != 'nt' else '.ps1'))
    try:
        text = marker.read_text(encoding='utf-8')
        for line in text.splitlines():
            if 'FRP_MANAGER_VERSION' in line or 'ManagerVersion' in line:
                parts = line.split("'")
                if len(parts) >= 2 and parts[1].startswith('v'):
                    return parts[1]
    except (OSError, ValueError):
        pass
    return 'unknown'

def _within(path, base):
    """Pure-Path containment check; works on Windows (no '/' string prefix)."""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False

def _https_get(url, timeout=60):
    if not url.startswith('https://'):
        raise ValueError('拒绝非 HTTPS 下载：' + url)
    req = urllib.request.Request(url, headers={'User-Agent': 'frp-simple-bash/' + MANAGER_RELEASE_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

UPGRADE_FILES = (
    'lib/manager.py', 'lib/download.sh', 'install-common.sh',
    'install-frps.sh', 'install-frpc.sh', 'install-windows.ps1',
    'install-frps.ps1', 'install-frpc.ps1', 'uninstall.sh',
    'uninstall-frps.ps1', 'uninstall-frpc.ps1', 'uninstall-windows.ps1',
)

def upgrade_snapshot():
    """Persist the current manager scripts before upgrade (详细设计 §12.4)."""
    base = ROOT / 'backups'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-upgrade-' + secrets.token_hex(3)
    staging = base / ('.staging-' + name)
    staging.mkdir(parents=True, mode=0o700)
    try:
        copied, digests = [], {}
        for rel in UPGRADE_FILES:
            src = INSTALL_ROOT / rel
            if not src.is_file() or src.is_symlink(): continue
            dst = staging / 'install-root' / rel
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(str(src), str(dst))
            digests[rel] = hashlib.sha256(dst.read_bytes()).hexdigest()
            copied.append(rel)
        if not copied: raise ValueError('没有可备份的管理器文件：' + str(INSTALL_ROOT))
        atomic(staging / 'manifest.json', json.dumps(
            {'files': copied, 'reason': 'upgrade', 'sha256': digests,
             'manager_version': installed_manager_version()}, ensure_ascii=False, indent=2) + '\n')
        final = base / name
        os.replace(str(staging), str(final))
    except BaseException:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise
    return final

def restore_upgrade_snapshot(directory):
    """Restore manager scripts from an upgrade snapshot."""
    directory = Path(directory)
    manifest = directory / 'manifest.json'
    if not manifest.exists(): raise ValueError('升级快照缺少 manifest.json')
    data = read_json(manifest, {})
    restored = []
    for rel in data.get('files', []):
        relative = Path(rel)
        if relative.is_absolute() or '..' in relative.parts: raise ValueError('快照路径非法：' + rel)
        src = directory / 'install-root' / relative
        if not src.is_file(): raise ValueError('快照文件缺失：' + rel)
        expected = (data.get('sha256') or {}).get(rel)
        if expected and hashlib.sha256(src.read_bytes()).hexdigest() != expected:
            raise ValueError('快照文件校验失败：' + rel)
        dest = INSTALL_ROOT / relative
        if not _within(dest, INSTALL_ROOT): raise ValueError('目标路径非法：' + rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + '.restore')
        tmp.write_bytes(src.read_bytes())
        os.replace(str(tmp), str(dest))
        os.chmod(str(dest), 0o755 if (rel.endswith('.sh') or rel.endswith('.ps1')) else 0o644)
        restored.append(rel)
    return restored

def _service_manager():
    """Return 'systemd' / 'openrc' / '' for the running host."""
    if os.name == 'nt': return ''
    if shutil.which('systemctl') and Path('/run/systemd/system').exists(): return 'systemd'
    if shutil.which('rc-service') and shutil.which('rc-update'): return 'openrc'
    return ''

def installed_unit_path(role):
    """Where the service unit for this component lives on this host."""
    manager = _service_manager()
    if manager == 'systemd': return Path('/etc/systemd/system') / (role + '.service')
    if manager == 'openrc': return Path('/etc/init.d') / role
    return None

def template_path(role):
    manager = _service_manager()
    if manager == 'systemd': return INSTALL_ROOT / 'templates' / (role + '.service')
    if manager == 'openrc': return INSTALL_ROOT / 'templates' / (role + '.openrc')
    return None

def unit_differs(role):
    """True when the registered unit content differs from the current template."""
    unit, template = installed_unit_path(role), template_path(role)
    if not unit or not template: return False
    if not unit.exists() or not template.exists(): return False
    try: return unit.read_bytes() != template.read_bytes()
    except OSError: return False

def refresh_units(roles=None, yes=False):
    """Offer to re-install registered units from the bundled templates.

    Upgrading manager scripts replaces templates/ but historically left already
    registered systemd/OpenRC units untouched, so template fixes never reached a
    running host. This closes that gap without silently touching service files:
    it only refreshes when content actually differs, and requires confirmation
    unless --yes was passed.
    """
    manager = _service_manager()
    if not manager: return []
    stale = [r for r in (roles or ('frps', 'frpc')) if unit_differs(r)]
    if not stale: return []
    names = '、'.join(stale)
    result_info('检测到服务单元模板已更新：' + names,
                detail='已注册的 ' + manager + ' 单元与当前模板不一致，可刷新使新配置生效。')
    if not yes and not confirm('用新模板刷新 ' + names + ' 的服务单元并 daemon-reload？'):
        result_info('已跳过服务单元刷新；现有服务注册保持不变。')
        return []
    refreshed, failed = [], []
    for role in stale:
        unit, template = installed_unit_path(role), template_path(role)
        try:
            old = unit.read_bytes() if unit.exists() else None
            atomic(unit, template.read_text(encoding='utf-8'))
            os.chmod(str(unit), 0o644 if manager == 'systemd' else 0o755)
        except (OSError, ValueError) as exc:
            failed.append(role + '（' + str(exc) + '）')
            continue
        refreshed.append(role)
        # Keep a rollback copy next to the backups so a bad template is recoverable.
        try:
            directory = ROOT / 'backups' / (dt.datetime.now().strftime('%Y%m%d-%H%M%S')
                                            + '-unit-' + role + '-' + secrets.token_hex(3))
            directory.mkdir(parents=True, mode=0o700)
            if old is not None:
                (directory / (role + '.service.old')).write_bytes(old)
            atomic(directory / 'manifest.json', json.dumps(
                {'files': [role], 'reason': 'unit-refresh'}, ensure_ascii=False, indent=2) + '\n')
        except OSError: pass
    if refreshed and manager == 'systemd':
        try: run_cmd(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError): pass
    if refreshed: audit('units-refreshed')
    if refreshed: result_ok('已刷新服务单元：' + '、'.join(refreshed) + '（已 daemon-reload）')
    if failed: result_error('部分服务单元刷新失败：' + '、'.join(failed),
                            hint='服务注册可能仍是旧内容，请检查上方错误或重新运行安装脚本。')
    return refreshed

def upgrade(role, target=None, yes=False):
    """Update the management scripts (manager itself) from a published release bundle.

    Preserves the frp native binary and the user configuration. Only manager files
    are overwritten. The frp binary version stays at SUPPORTED_VERSIONS[-1].
    """
    before = installed_manager_version()
    result_info('准备更新管理器', detail='当前管理器版本：' + before + '\n当前 frp 二进制版本：' + installed_frp_version(role))
    target = target or MANAGER_RELEASE_VERSION
    if not target.startswith('v') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in target):
        raise ValueError('非法的管理器版本号：' + str(target))
    result_info('将更新管理器至：' + target)
    if not yes and not confirm('更新管理器脚本（保留 frp 二进制与配置）？'): return
    snapshot = None
    try: snapshot = upgrade_snapshot()
    except (OSError, ValueError) as exc:
        if not yes and not confirm('升级前备份失败（' + str(exc) + '），仍要继续？'): return
    mirror = os.environ.get('FRP_MIRROR_URL', 'https://download.sueiyuan.top').rstrip('/')
    if mirror in ('', 'off', 'OFF'):
        raise ValueError('升级需要 HTTPS 镜像；FRP_MIRROR_URL=off 时无法下载管理器发布包。')
    if not mirror.startswith('https://') or any(c.isspace() for c in mirror):
        raise ValueError('FRP_MIRROR_URL 必须是合法 HTTPS 地址。')
    try:
        bundle_url = f'{mirror}/frp-simple-bash/releases/{target}/source.tar.gz'
        sha_url = f'{mirror}/frp-simple-bash/releases/{target}/SHA256SUMS.txt'
        bundle = _https_get(bundle_url)
        manifest = _https_get(sha_url).decode('utf-8', 'replace')
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ValueError('无法下载管理器发布包：' + str(exc))
    expected = ''
    for line in manifest.splitlines():
        cols = line.split()
        if len(cols) == 2 and cols[1] in ('source.tar.gz', '*source.tar.gz'):
            expected = cols[0]
            break
    if not expected or len(expected) != 64:
        raise ValueError('发布包清单中没有有效的 source.tar.gz SHA256。')
    if hashlib.sha256(bundle).hexdigest() != expected:
        raise ValueError('管理器发布包 SHA256 不匹配，已拒绝更新。')
    import tarfile, io as _io, tempfile as _tempfile
    manager_files = (
        'lib/manager.py', 'lib/download.sh', 'install-common.sh',
        'install-frps.sh', 'install-frpc.sh', 'install-windows.ps1',
        'install-frps.ps1', 'install-frpc.ps1', 'uninstall.sh',
        'uninstall-frps.ps1', 'uninstall-frpc.ps1', 'uninstall-windows.ps1')
    # Stage ALL files first; only commit when every member is valid (transactional).
    staged = []
    with tarfile.open(fileobj=_io.BytesIO(bundle), mode='r:gz') as tar:
        members = tar.getmembers()
        if not any(x.name == 'frp-simple-bash/lib/manager.py' for x in members):
            raise ValueError('发布包结构异常，未找到 lib/manager.py。')
        seen = set()
        for x in members:
            rel = x.name[len('frp-simple-bash/'):] if x.name.startswith('frp-simple-bash/') else x.name
            if not (rel in manager_files or rel.startswith('bin/') or rel.startswith('templates/')):
                continue
            dest = INSTALL_ROOT / rel
            if not _within(dest, INSTALL_ROOT):
                raise ValueError('发布包路径越界：' + rel)
            if not x.isfile():
                raise ValueError('发布包含非文件成员：' + rel)
            f = tar.extractfile(x)
            if f is None: raise ValueError('发布包成员读取失败：' + rel)
            mode = 0o755 if (rel.startswith('bin/') or rel.endswith('.sh') or rel.endswith('.ps1')
                             or rel.endswith('.openrc')) else 0o644
            staged.append((dest, f.read(), mode))
            seen.add(rel)
        missing = set(manager_files) - seen
        if missing:
            raise ValueError('发布包缺少必需文件：' + ', '.join(sorted(missing)))
    stage_dir = Path(_tempfile.mkdtemp(prefix='frp-upgrade-'))
    backups = {}
    try:
        for i, (dest, data, mode) in enumerate(staged):
            (stage_dir / str(i)).write_bytes(data)
        # Commit with rollback: back up each original, then replace atomically.
        for i, (dest, data, mode) in enumerate(staged):
            if dest.exists():
                backups[dest] = dest.read_bytes()
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + '.' + secrets.token_hex(5) + '.new')
            tmp.write_bytes(data)
            os.replace(str(tmp), str(dest))
            os.chmod(str(dest), mode)
    except Exception:
        for dest, old in backups.items():
            try:
                tmp = dest.with_name(dest.name + '.rollback')
                tmp.write_bytes(old)
                os.replace(str(tmp), str(dest))
            except OSError: pass
        raise
    finally:
        shutil.rmtree(str(stage_dir), ignore_errors=True)
    after = installed_manager_version()
    audit('upgraded')
    rollback_hint = ('升级前快照：' + snapshot.name + '（可用 restore 恢复）') if snapshot else ''
    result_ok(f'管理器已更新：{before} → {after}。配置与 frp 二进制「{installed_frp_version(role)}」保持不变；下次运行 {role} 即生效。',
              detail=rollback_hint or None)
    # Template fixes do not reach a running host unless the registered unit is
    # refreshed; do it here (only when content differs, and with confirmation).
    refresh_units(('frps', 'frpc'), yes=yes)


CLI_FLAGS = ('--json', '--yes', '--purge', '--follow', '--non-interactive')

def _parse_flags(argv):
    """Split argv into positional args and recognised long flags (详细设计 §16)."""
    args, flags, i = [], {}, 0
    while i < len(argv):
        item = argv[i]
        if item == '--lines' and i + 1 < len(argv):
            flags['lines'] = argv[i + 1]; i += 2; continue
        if item == '--version' and i + 1 < len(argv) and not argv[i + 1].startswith('-'):
            flags['version'] = argv[i + 1]; i += 2; continue
        if item == '--profile' and i + 1 < len(argv):
            flags['profile'] = argv[i + 1]; i += 2; continue
        if item in CLI_FLAGS:
            flags[item.lstrip('-')] = True; i += 1; continue
        args.append(item); i += 1
    return args, flags

def api_envelope(role, data=None, error=None):
    """Stable JSON envelope for scripts (详细设计 §16.3). Never leaks secrets."""
    payload = dict(data or {})
    conn = payload.get('connection') or {}
    profile = payload.get('active_profile') or {}
    return {
        'ok': error is None,
        'code': 0 if error is None else 1,
        'component': role,
        'version': payload.get('version', VERSION),
        'service_state': payload.get('service_state', 'unknown'),
        'connection_state': conn.get('state', 'unknown'),
        'active_profile': ({'id': profile.get('id'), 'name': profile.get('name'),
                            'address': profile.get('server_addr'),
                            'port': profile.get('server_port')} if profile else None),
        'pending': bool(payload.get('pending')),
        'error': error,
        'detail': payload,
    }

def run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ('frps','frpc'): raise ValueError('需要指定 frps/frpc')
    role = argv.pop(0); initialize()
    cmd = argv.pop(0) if argv else 'menu'
    if cmd == 'status':
        _args, flags = _parse_flags(argv)
        data = status(role)
        print(json.dumps(api_envelope(role, data) if flags.get('json') else data, ensure_ascii=False, indent=2))
        return
    if cmd == 'version': print(json.dumps({'ok': True, 'code': 0, 'component': role, 'manager': installed_manager_version(), 'frp': installed_frp_version(role), 'supported_frp': list(SUPPORTED_VERSIONS), 'error': None}, ensure_ascii=False)); return
    if cmd == 'path': print(f'{ROOT}\n{binary(role)}'); return
    if cmd in ('start','stop','restart'):
        if role == 'frpc': require_active()
        service(role, cmd); return
    if cmd == 'logs':
        args, flags = _parse_flags(argv)
        lines = 50
        if flags.get('lines', '').isdigit(): lines = int(flags['lines'])
        elif args and args[0].isdigit(): lines = int(args[0])
        if flags.get('follow'): follow_log(role, lines); return
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
            print(json.dumps(redact_secrets(read_frps_config()), ensure_ascii=False, indent=2))
        elif sub == 'validate':
            data = read_frps_config(); validate_frps_config(data); verify('frps', toml(data))
            print(json.dumps({'valid': True, 'path': str(ROOT / 'frps.toml')}, ensure_ascii=False)); return
        else: raise ValueError('支持 config show/validate')
        return
    if cmd == 'install':
        _a, flags = _parse_flags(argv)
        install(role, yes=bool(flags.get('yes')), no_start=bool(flags.get('nostart')))
        return
    if cmd == 'uninstall':
        _a, flags = _parse_flags(argv)
        uninstall(role, purge=bool(flags.get('purge')), yes=bool(flags.get('yes')))
        return
    if cmd in ('upgrade','update'):
        _a, flags = _parse_flags(argv)
        upgrade(role, flags.get('version'), yes=bool(flags.get('yes')))
        return
    if cmd in ('refresh-units', 'refresh_units'):
        _a, flags = _parse_flags(argv)
        done = refresh_units(('frps', 'frpc'), yes=bool(flags.get('yes')))
        print(json.dumps({'ok': True, 'code': 0, 'refreshed': done, 'error': None},
                         ensure_ascii=False, indent=2))
        return
    if cmd == 'configure': (configure_server if role == 'frps' else profile_wizard)(); return
    if cmd == 'import': import_external(role); return
    if cmd == 'menu':
        try: import_external(role)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
            result_error('外部配置未同步，原文件已保留；请使用 import 命令排查或从备份恢复。')
        main_menu(role); return
    if cmd == 'profile' and role == 'frpc':
        sub = argv.pop(0) if argv else 'list'
        if sub == 'list': print(json.dumps(profiles(), ensure_ascii=False, indent=2))
        elif sub == 'add': profile_wizard()
        elif sub == 'use' and argv:
            target = argv[0]
            if target.isdigit():
                ps = profiles()
                idx = int(target)
                if not 1 <= idx <= len(ps): raise ValueError('编号超出范围：' + target)
                target = ps[idx - 1]['id']
            switch(target)
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
