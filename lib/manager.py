#!/usr/bin/env python3
"""frp-simple-bash shared management engine; requires Python >= 3.6."""
import contextlib
import datetime as dt
import hashlib
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
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
import urllib.parse
import base64
import socket

VERSION = '0.72.0'
# Manager release version (the management scripts themselves, not the frp binary).
# Bumped on every published release; upgrade() pulls exactly this bundle.
MANAGER_RELEASE_VERSION = 'v1.0.0-rc.26'
ROOT = Path(os.environ.get('FRP_CONFIG_DIR', '/etc/frp' if os.name != 'nt' else os.environ.get('ProgramData', 'C:/ProgramData') + '/frp-simple-bash'))
BIN = Path(os.environ.get('FRP_BINARY_DIR', '/opt/frp-simple-bash/native' if os.name != 'nt' else os.environ.get('ProgramFiles', 'C:/Program Files') + '/frp-simple-bash/native'))
INSTALL_ROOT = Path(os.environ.get('FRP_INSTALL_DIR', '/opt/frp-simple-bash' if os.name != 'nt' else os.environ.get('ProgramFiles', 'C:/Program Files') + '/frp-simple-bash'))
TEST = os.environ.get('FRP_TEST_MODE') == '1'
# One consistent catch-set for menu handlers: a menu must never crash the
# whole program because of a single unexpected exception type.
MENU_ERRORS = (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError)
# Current interactive menu role ('frps'/'frpc'); set at main_menu entry so
# result_error can quote the correct log even in frps sub-menus.
_CURRENT_ROLE = 'frps'

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
                print('  ' + _tag('[失败]') + ' 回滚写入失败：' + str(p) + ' — ' + str(exc),
                      file=sys.stderr)
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

class ConfigCorruptError(ValueError):
    """配置文件存在但无法解析（区别于用户输入校验失败）。

    Web 端据此返回 5xx + 具体文件路径，而不是把解析器英文原文当 400 返回。
    """

    def __init__(self, path, detail):
        ValueError.__init__(self, '%s 无法解析：%s' % (path, detail))
        self.path = str(path)
        self.detail = str(detail)


def read_toml(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    try:
        return _load_toml(text)
    except (ValueError, TypeError) as exc:
        raise ConfigCorruptError(path, exc)

def read_json(path, default=None):
    if not path.exists(): return {} if default is None else default
    text = path.read_text(encoding='utf-8')
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ConfigCorruptError(path, exc)

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
        # 只在持有者进程确实不存活时回收；否则慢操作（大备份/慢盘）会被
        # 另一终端强抢锁，出现两个写者（BUG-4-3）。Windows 无法探活时用年龄兜底。
        dead = not _pid_alive(pid)
        if dead or (os.name == 'nt' and age > 1800):
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
    if not re.fullmatch(r'[a-f0-9]{24}', pid): err('E011', '非法的服务端配置 ID')
    return ROOT / 'profiles' / pid

def load_profile(pid):
    p = read_toml(pdir(pid) / 'profile.toml')
    if p.get('id') != pid: raise ValueError('服务端配置 ID 与目录不一致')
    validate_profile(p, pid)
    return p

def profiles():
    result = []
    for path in sorted((ROOT / 'profiles').glob('*/profile.toml')):
        result.append(load_profile(path.parent.name))
    return sorted(result, key=lambda p: (p['name'].casefold(), p['id']))

def port(v):
    if isinstance(v, bool) or not re.fullmatch(r'[0-9]{1,5}', str(v)) or not 1 <= int(v) <= 65535: err('E006', '端口必须是 1-65535 的 ASCII 数字')
    return int(v)

def address(v):
    if not isinstance(v, str) or not v or any(c.isspace() for c in v): raise ValueError('地址不能为空或包含空白')
    try: ipaddress.ip_address(v); return v
    except ValueError: pass
    if len(v) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part) for part in v.rstrip('.').split('.')): raise ValueError('请填写合法 IPv4、IPv6 或域名，不包含协议或端口')
    return v

def local_address(v):
    """localIP 校验：合法 IP 或看起来像域名的主机名。

    "999.1.1.1" 这种"长得像 IPv4 但非法"的必须拒绝——frpc 不会替你校验，
    只会在运行期表现为连不上，用户很难定位。
    """
    if not isinstance(v, str) or not v or any(c.isspace() for c in v):
        raise ValueError('本地地址不能为空或包含空白')
    try:
        ipaddress.ip_address(v); return v
    except ValueError:
        pass
    parts = v.split('.')
    if len(parts) > 1 and all(part.isdigit() for part in parts):
        # 1.2.3.4.5 / 999.1.1.1 这类"长得像 IPv4 但非法"的一律拒绝
        raise ValueError('本地地址不是合法 IPv4：' + v)
    if len(v) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                               for part in v.rstrip('.').split('.')):
        raise ValueError('请填写合法 IPv4、IPv6 或主机名，不包含协议或端口')
    return v

def name(v):
    if not isinstance(v, str) or not 1 <= len(v) <= 64 or not re.fullmatch(r'[A-Za-z0-9_\-\u3400-\u9fff]+', v): raise ValueError('名称只允许中文、字母、数字、-、_，长度 1-64，不含空格')
    return v

def label(p):
    addr = '[' + p['server_addr'] + ']' if ':' in p['server_addr'] else p['server_addr']
    return f"{p['name']}（{addr}:{p['server_port']}）"

def validate_profile(p, pid=''):
    name(p['name']); address(p['server_addr']); port(p['server_port'])
    port(p.get('dashboard_port', 7500))
    # admin_port 是本机管理 API 端口；不校验会让用户把 frpc 配成 0/70000，
    # 还会把 Basic 凭据发给 127.0.0.1 上任意端口
    port(p.get('admin_port', 7400))
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

def validate_mappings(items):
    """Validate a whole mapping set: per-item rules + cross-item conflicts.

    frps 会拒绝重复的 remote_port，但那是"注册失败"层面的错误，
    用户看到的是连接异常而不是配置错误。这里提前拦截并给出明确提示。
    """
    for item in items: validate_mapping(item)
    names, remote_ports = {}, {}
    for item in items:
        name = item['name']
        if name in names: raise ValueError('映射名称重复：' + name)
        names[name] = True
        # 停用的映射不会写入运行配置，因此不参与端口查重
        if item['type'] in ('tcp', 'udp') and item.get('enabled', True):
            key = (int(item['remote_port']), item['type'])
            if key in remote_ports:
                raise ValueError('远程端口重复：%d/%s（映射 %s 与 %s 冲突）'
                                 % (key[0], key[1], remote_ports[key], name))
            remote_ports[key] = name

def validate_mapping(m):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', m['name']): raise ValueError('映射名称允许 1-64 个 ASCII 字母、数字、-、_')
    if m['type'] not in ('tcp', 'udp', 'http', 'https'): raise ValueError('仅支持 TCP/UDP/HTTP/HTTPS')
    local_address(m.get('local_ip', '127.0.0.1')); port(m['local_port'])
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

def update_profile(pid, profile, secret):
    """非交互地保存服务端配置（Web 端复用）。

    与 edit_profile() 的"保存并返回"走同一套校验与原子写入，
    但完全不依赖终端交互。
    """
    current = load_profile(pid)
    merged = dict(current)
    merged.update(profile)
    merged['id'] = pid
    unique_name(merged.get('name', ''), pid)
    validate_profile(merged, pid)
    with lock(), transaction():
        atomic(pdir(pid) / 'profile.toml', toml(merged))
        atomic(pdir(pid) / 'secrets.toml', toml(secret))
        rebuild_index()
        if pid == active(): apply(pid)
        audit('profile-updated', pid)
    return merged

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

def _parse_etime(text):
    """Parse `ps -o etime` output: MM:SS / HH:MM:SS / DD-HH:MM:SS → seconds."""
    s = str(text or '').strip()
    if not s: return None
    days = 0
    if '-' in s:
        head, s = s.split('-', 1)
        try: days = int(head)
        except ValueError: return None
    parts = s.split(':')
    try: nums = [int(x) for x in parts]
    except ValueError: return None
    if len(nums) == 2: h, m, sec = 0, nums[0], nums[1]
    elif len(nums) == 3: h, m, sec = nums
    else: return None
    return days * 86400 + h * 3600 + m * 60 + sec

def _service_pid(role):
    """Best-effort service PID for the current platform."""
    try:
        if os.name == 'nt': return None
        if shutil.which('systemctl') and Path('/run/systemd/system').exists():
            out = run_cmd(['systemctl', 'show', '-p', 'MainPID', '--value', role],
                          capture_output=True, text=True, timeout=10)
            pid = (out.stdout or '').strip()
            return pid if pid.isdigit() and pid != '0' else None
        if sys.platform == 'darwin':
            out = run_cmd(['launchctl', 'print', 'system/frp-simple-bash.' + role],
                          capture_output=True, text=True, timeout=10)
            found = re.search(r'\bpid = (\d+)', out.stdout or '')
            return found.group(1) if found else None
        # OpenRC / 其他：pidof 或 pidfile
        if shutil.which('pidof'):
            out = run_cmd(['pidof', role], capture_output=True, text=True, timeout=10)
            pid = (out.stdout or '').strip().split()[0] if (out.stdout or '').strip() else ''
            if pid.isdigit(): return pid
        for candidate in (Path('/run') / (role + '.pid'), Path('/var/run') / (role + '.pid')):
            if candidate.is_file():
                try:
                    pid = candidate.read_text().strip()
                    if pid.isdigit(): return pid
                except OSError: pass
        return None
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None

def service_uptime(role):
    """Seconds the service process has been running, or None if unknown.

    `ps -o etimes` is GNU-only (busybox/macOS lack it), so fall back to parsing
    `ps -o etime`. Without this fallback refresh_pending() silently never worked
    on macOS/Windows/OpenRC (BUG-3-2).
    """
    pid = _service_pid(role)
    if not pid: return None
    try:
        out = run_cmd(['ps', '-o', 'etimes=', '-p', pid], capture_output=True, text=True, timeout=10)
        value = (out.stdout or '').strip()
        if value.isdigit(): return int(value)
        out = run_cmd(['ps', '-o', 'etime=', '-p', pid], capture_output=True, text=True, timeout=10)
        return _parse_etime(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None

def refresh_pending(role='frpc'):
    """Clear a stale pending flag when the running process already reloaded the config.

    `pending` used to be a plain boolean that only service() cleared, so a restart
    done outside this menu (systemctl, a reboot) left 「有修改等待重启」 forever.
    Inference: if the process started AFTER frpc.toml was last modified, it has
    necessarily loaded that config — so the flag is stale.
    """
    if role != 'frpc': return
    try:
        s = state()
        if not s.get('pending'): return
        cfg = ROOT / 'frpc.toml'
        if not cfg.exists(): return
        uptime = service_uptime('frpc')
        if uptime is None: return
        age = time.time() - cfg.stat().st_mtime
        if age > uptime + 1:      # 进程启动时刻晚于配置修改时刻（留 1 秒容差）
            set_pending(False)
            audit('pending-auto-cleared')
    except (ValueError, OSError, KeyError, TypeError):
        pass

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
        if not wrapper.exists():
            if action == 'status': return 'not-installed'
            raise ValueError('Windows 服务包装器未安装')
        # Three-state start type (需求 §10.2): enable=auto, manual=demand,
        # disable=disabled. sc.exe start= value must NOT have a space.
        if action in ('enable', 'manual', 'disable'):
            start_type = {'enable': 'auto', 'manual': 'demand', 'disable': 'disabled'}[action]
            result = run_cmd(['sc.exe', 'config', 'frp-simple-bash-' + role, 'start=', start_type], capture_output=True, text=True, timeout=30)
        else: result = run_cmd([str(wrapper), action], capture_output=True, text=True, timeout=30)
    elif shutil.which('systemctl') and Path('/run/systemd/system').exists():
        args = ['systemctl', 'is-active' if action == 'status' else action, role]
        result = run_cmd(args, capture_output=True, text=True, timeout=30)
    elif shutil.which('rc-service'):
        if action in ('enable', 'disable'):
            result = run_cmd(['rc-update', 'add' if action == 'enable' else 'del', role, 'default'], capture_output=True, text=True, timeout=30)
        elif action == 'status':
            # rc-service 打印人类可读文本（如 " * status: started"），必须归一化，
            # 否则 running() 永远为 False，会连锁破坏启动反馈/pending/切换/删除（BUG-3-1）。
            result = run_cmd(['rc-service', role, 'status'], capture_output=True, text=True, timeout=30)
            text = (result.stdout or '').lower()
            if 'status: started' in text or 'status: starting' in text: return 'started'
            if 'status: stopped' in text or 'status: crashed' in text: return 'stopped'
            return 'started' if result.returncode == 0 else 'stopped'
        else: result = run_cmd(['rc-service', role, action], capture_output=True, text=True, timeout=30)
    elif sys.platform == 'darwin' and shutil.which('launchctl'):
        # launchd: label frp-simple-bash.<role>, plist in /Library/LaunchDaemons.
        #   start/stop  -> kickstart / kill (process control)
        #   enable/disable -> launchctl enable/disable (persistent boot policy)
        label = 'frp-simple-bash.' + role
        plist = Path('/Library/LaunchDaemons') / (label + '.plist')
        target = 'system/' + label
        if action == 'status':
            result = run_cmd(['launchctl', 'print', target], capture_output=True, text=True, timeout=30)
            if result.returncode != 0: return 'stopped'
            # launchctl print exits 0 when loaded; "state = running" means active.
            if 'state = running' in result.stdout: return 'running'
            return 'stopped'
        if action in ('enable', 'manual', 'disable'):
            # Persist the boot policy. `enable`/`disable` toggle auto-start at
            # boot (RunAtLoad is ignored when disabled). `manual` is aliased to
            # disable on launchd (no separate "manual" concept: disabled = not
            # auto-started). Using load/unload here would NOT survive a reboot.
            if action == 'disable' or action == 'manual':
                result = run_cmd(['launchctl', 'disable', target], capture_output=True, text=True, timeout=30)
            else:
                result = run_cmd(['launchctl', 'enable', target], capture_output=True, text=True, timeout=30)
        else:
            if action == 'start':
                # If the service was never bootstrapped (e.g. a failed install),
                # bootstrap it now so kickstart can find it instead of erroring.
                probe = run_cmd(['launchctl', 'print', target], capture_output=True, text=True, timeout=30)
                if probe.returncode != 0 and plist.exists():
                    run_cmd(['launchctl', 'bootstrap', 'system', str(plist)], capture_output=True, text=True, timeout=30)
                result = run_cmd(['launchctl', 'kickstart', target], capture_output=True, text=True, timeout=30)
            elif action == 'stop':
                result = run_cmd(['launchctl', 'kill', 'SIGTERM', target], capture_output=True, text=True, timeout=30)
            else:  # restart
                result = run_cmd(['launchctl', 'kickstart', '-k', target], capture_output=True, text=True, timeout=30)
    else: return 'unknown' if action == 'status' else fail('无支持的服务管理器，无法保证后台运行与自启动')
    if action == 'status': return result.stdout.strip() or 'stopped'
    if result.returncode: err('E007', '服务操作失败：' + result.stderr.strip())
    if action in ('start', 'stop', 'restart'): audit('service-' + action)
    # 只有 restart 会重新加载配置；`start` 对已在运行的单元是 no-op，
    # 无条件清 pending 会把"未生效"误标成"已生效"（BUG-2-9）。
    # 启动一个原本停止的服务时，由 refresh_pending() 依据进程启动时间自动校正。
    if role == 'frpc' and action == 'restart':
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
    'E011': '服务端配置不存在',
    'E012': '映射不存在',
    'E013': '连接超时',
    'E014': '配置写入失败（检查权限和磁盘）',
    'E015': '用户取消',
    'E016': '锁被其他操作占用',
    'E017': '版本不兼容',
}

def err(code, message):
    if _quiet():
        raise ValueError('[%s] %s' % (code, message))
    """Raise a coded error: '<message>[Exxx]'. Never changes the exception type.

    The code must exist in ERROR_CODES (详细设计 §15.1); this makes the table
    authoritative and prevents typo'd/unknown codes from slipping through.
    """
    if code not in ERROR_CODES:
        raise ValueError('内部错误：未知错误码 ' + code + '（' + str(message) + '）')
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
    # Match concrete auth/TLS failure phrases, not the loose substrings
    # 'token'/'tls'/'certificate' which also appear in normal connect logs and
    # caused false auth-failed/tls-failed verdicts.
    auth_marks = ('user authentication failed', 'failed to authenticate',
                  'authentication failed', 'token is incorrect', 'token mismatch')
    tls_marks = ('tls handshake failed', 'certificate verify failed',
                 'x509:', 'tls: failed to verify', 'no certificates')
    auth_error = max(low.rfind(x) for x in auth_marks)
    tls_error = max(low.rfind(x) for x in tls_marks)
    # 认证/TLS 失败若发生在最后一次成功登录之后，必须优先报失败；否则服务端
    # 改 token 后界面会一直显示"连接中"，要等 30 秒超时（BUG-4-2）。
    if auth_error > success:
        return {'state': 'auth-failed', 'detail': 'token 或认证参数不匹配'}
    if tls_error > success:
        return {'state': 'tls-failed', 'detail': 'TLS 证书或信任配置错误'}
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
_CONN_CACHE_MAX = 8   # 长时间开着菜单时避免字典无界增长（BUG-4-4）

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
    if len(_CONN_CACHE) > _CONN_CACHE_MAX:
        for stale in sorted(_CONN_CACHE, key=lambda k: _CONN_CACHE[k][0])[:len(_CONN_CACHE) - _CONN_CACHE_MAX]:
            _CONN_CACHE.pop(stale, None)
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
            enabled = [m for m in mappings(pid) if m.get('enabled', True)]
            if enabled:
                result_ok('已连接服务端，' + str(len(enabled)) + ' 条映射已生效。')
            else:
                result_info('已连接服务端，但当前没有启用的端口映射，外部仍无法访问。',
                            detail='下一步：主菜单 9「管理端口映射」→ 新增端口映射。')
            return True
        if state in ('auth-failed', 'tls-failed'):
            result_error('连接失败：' + result.get('detail', state) + '；已保留当前服务端配置。',
                         hint='排查：服务端的 token / TLS 配置；菜单「日志与连接诊断」→ 运行连接诊断。')
            return False
        # A stopped/failed service can never reach "connected": fail fast with
        # the real reason instead of waiting out the whole timeout.
        if state == 'stopped' and not running('frpc'):
            result_error('frpc 未在运行，连接检测无法继续；已保留当前服务端配置。',
                         hint='先在主菜单按 1「启动」；若启动失败请看「查看日志与连接诊断」（主菜单 11）。')
            return False
        if state == 'proxy-error':
            result_error('frpc 已登录，但有映射未就绪；已保留当前服务端配置。',
                         hint='检查映射的本地服务是否在运行、远程端口是否被占用；用主菜单 9「管理端口映射」查看。')
            return False
        if state == 'config-error':
            result_error('配置错误，无法建立连接；已保留当前服务端配置。',
                         hint='检查主菜单 7「修改服务端配置」中的地址/端口/token/TLS；或从「备份与恢复配置」恢复。')
            return False
        remaining = max(0, int(deadline - time.monotonic()))
        if remaining == 0:
            result_error(f'连接检测超时（{timeout} 秒），保留新 profile。[E013]',
                         hint='下一步：菜单「日志与连接诊断」→ 运行连接诊断 / 查看最近 50 行日志；检查服务端防火墙、安全组与地址端口。')
            return False
        if state != last:
            print(f'  {_tag("[提示]")} 正在连接…（{CONNECTION_STATES.get(state, state)}），剩余 {remaining} 秒。')
            last = state
        time.sleep(min(2, max(0, deadline - time.monotonic())))

def switch(pid):
    """Switch the active profile.

    关键改动（修复「切换后连接不上、并且当前连接被切断」）：
    - 切换前先探测新服务端可达性，不可达时明确告警，避免切完才发现连不上；
    - 运行中的 frpc 不再同步 stop→start，而是走 restart_safely()，这样即使
      用户正通过映射端口 SSH 登录，也不会被自己的切换动作切断；
    - 用户放弃重启时回退运行配置与 active，保持「配置=运行」一致。
    """
    previous = active()
    try: old_label = label(load_profile(previous)) if previous else '（无）'
    except (ValueError, OSError, KeyError, TypeError): old_label = '（无）'
    new_profile = load_profile(pid)
    new_label = label(new_profile)
    content = render(pid); verify('frpc', content)
    was_running = running('frpc')
    host = new_profile.get('server_addr', '')
    port_value = int(new_profile.get('server_port', 7000))
    if host and not port_reachable(host, port_value):
        if not confirm(f'新服务端 {host}:{port_value} 当前不可达（地址/端口错误或被防火墙拦截）。'
                       '仍要切换？', danger=True):
            result_info('已取消切换，当前配置保持不变。'); return
    if was_running and not confirm('切换将中断当前连接，确认继续？'): return
    old = (ROOT / 'frpc.toml').read_bytes() if (ROOT / 'frpc.toml').exists() else None
    try:
        with lock(), transaction():
            atomic(ROOT / 'frpc.toml', content); set_current(pid); mark_hash('frpc')
    except Exception:
        if old is not None: atomic(ROOT / 'frpc.toml', old.decode('utf-8'))
        mark_hash('frpc')
        if was_running:
            try: service('frpc', 'start')
            except (ValueError, OSError) as exc:
                result_error('切换失败且旧服务未能自动恢复：' + str(exc),
                             hint='请手动执行「启动」，或从「备份与恢复配置」恢复。')
        raise
    if not was_running:
        set_pending(False)
        result_info('已切换当前配置，frpc 保持停止。',
                    detail=f'当前服务端：{old_label} → {new_label}')
        return
    outcome = restart_safely('frpc')
    if outcome == 'cancelled':
        # 没重启就等于没生效：回退运行配置与 active，避免"配置指向新、进程跑旧"
        if old is not None: atomic(ROOT / 'frpc.toml', old.decode('utf-8'))
        mark_hash('frpc'); set_pending(False)
        if previous: set_current(previous)
        result_info('已取消切换，运行配置与当前服务端保持不变。')
        return
    if outcome == 'scheduled':
        result_info('已切换到「' + new_label + '」，将在后台重启生效。',
                    detail='若你正通过映射端口 SSH 登录，本连接会断开；重连后新服务端已生效。')
        return
    if not wait_connection(pid):
        # 新服务端连不上：把「运行配置 + active」一起回退到旧服务端，
        # 避免出现"active 指向 B、进程跑着 A"的假象（BUG-2-8）。
        if old is not None: atomic(ROOT / 'frpc.toml', old.decode('utf-8'))
        set_current(previous if previous else '')
        mark_hash('frpc'); set_pending(False)
        restarted = True
        try: service('frpc', 'restart')
        except (ValueError, OSError) as exc:
            restarted = False
            result_error('切换失败且旧服务未能重启：' + str(exc),
                         hint='请手动执行「启动」，或从「备份与恢复配置」恢复。')
        result_error('切换到「' + new_label + '」后连接失败，已回退到「' + old_label + '」。',
                     hint='请检查该服务端的地址/端口/token 与防火墙后重试。'
                          + ('' if restarted else '（旧服务重启失败，请手动启动）'))
        return
    result_ok(f'已切换到「{new_label}」。',
              detail=f'当前服务端：{old_label} → {new_label}；连接正常。')

CANCEL_KEYS = ('q', 'Q')
BACK_KEYS = ('b', 'B')

class QuitProgram(BaseException):
    """Raised when the user types q in a menu to quit the whole program.

    Inherits BaseException (not Exception) so generic `except Exception`
    rollback handlers do not swallow it; `transaction()` still rolls back and
    re-raises, which is the desired behaviour.
    """

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

_COLORS = {'[成功]': '32', '[提示]': '36', '[失败]': '31', '[注意]': '33'}

def _tag(kind):
    if UI_COLOR and kind in _COLORS:
        return '\033[1;' + _COLORS[kind] + 'm' + kind + '\033[0m'
    return kind

def _shorten(text, width):
    """Truncate to a display width with an ellipsis (CJK counts as 2)."""
    s = str(text)
    if _display_width(s) <= width: return s
    out = ''
    for ch in s:
        if _display_width(out + ch) > width - 1: break
        out += ch
    return out + '…'

def _dim(text):
    """De-emphasise hint text (no-op when colors are off)."""
    return '\033[2m' + str(text) + '\033[0m' if UI_COLOR else str(text)

# 状态词 → 颜色：绿=正常/成功，红=异常/停止，黄=过渡/未知。
_GOOD_WORDS = ('已连接', '已启用', '已设置', '已保存', '已生效', '已放行', '已开启',
               '已加入', '已添加', '已移除', '已撤销', '已配置', '已回收', '已删除',
               '运行中', '启用', '成功', '可达', '正常', '在线', '通过', '当前')
# 注意：否定式必须比肯定式长，color_status_words 用「最长优先」匹配，
# 否则「不正常」会被「正常」染成绿色（假绿）。
_BAD_WORDS = ('无法访问', '未连接', '未启用', '未设置', '未放行', '未开启', '未加入',
              '未运行', '已断开', '已停止', '不通过', '不可达', '不正常', '不会放行',
              '离线', '停用', '失败', '异常', '错误', '拒绝', '拦下')
_WARN_WORDS = ('连接超时', '尚未生成', '待确认', '待重启', '待手工放行', '连接中', '启动中',
               '停止中', '重启中', '未选择', '未就绪', '未安装', '未配置', '未检测到',
               '已关闭', '手动', '未知', '部分', '等待', '需在面板')

def color_state(text):
    """Colour a status word by meaning; returns text unchanged when colour is off.

    Handles compound labels such as '已停止 · 未连接' by colouring each token.
    """
    if not UI_COLOR or text is None: return text
    s = str(text)
    # Order matters: abnormal/negative words win, and each list is matched
    # longest-first. Otherwise substring hits like 启用/可达/通过 would colour
    # 未启用/不可达/不通过 GREEN — i.e. report a fault as healthy.
    for words, code in ((_BAD_WORDS, '31'), (_WARN_WORDS, '33'), (_GOOD_WORDS, '32')):
        for w in sorted(words, key=len, reverse=True):
            if w in s: return '\033[1;' + code + 'm' + s + '\033[0m'
    return s

_ANSI_SGR = re.compile(r'\033\[[0-9;]*[A-Za-z]')
_STATUS_RE = [None]

def _tone(text, code):
    """按语义给任意文本上色（词表覆盖不到的状态词用这个，如「开/关/自动」）。"""
    if not UI_COLOR: return str(text)
    return '\033[1;' + code + 'm' + str(text) + '\033[0m'


def col_ok(text):
    """绿色：正常 / 成功 / 已启用 / 在线 / 已放行。"""
    return _tone(text, '32')


def col_warn(text):
    """黄色：过渡 / 未配置 / 已关闭 / 待处理 / 需要关注但不致命。"""
    return _tone(text, '33')


def col_bad(text):
    """红色：失败 / 异常 / 未启用 / 离线 / 未放行。"""
    return _tone(text, '31')


def color_status_words(text):
    """Colour every recognised status word inside a sentence.

    与 color_state() 的区别：color_state 是「整行按语义染色」，本函数是
    「句子内关键词上色」，用于结果框正文，让「已保存；下次重启后生效」
    这类句子里的关键状态一眼可见。

    单次正则替换 + 最长优先，避免「未启用」被「启用」二次包裹。
    """
    if not UI_COLOR or text is None: return text
    s = str(text)
    if '\033[' in s: return s
    if _STATUS_RE[0] is None:
        mapping = {}
        for words, code in ((_BAD_WORDS, '31'), (_WARN_WORDS, '33'), (_GOOD_WORDS, '32')):
            for word in words: mapping.setdefault(word, code)
        ordered = sorted(mapping.items(), key=lambda kv: -len(kv[0]))
        pattern = re.compile('|'.join(re.escape(w) for w, _ in ordered))
        _STATUS_RE[0] = (pattern, dict(ordered))
    pattern, table = _STATUS_RE[0]
    return pattern.sub(lambda m: '\033[1;' + table[m.group(0)] + 'm' + m.group(0) + '\033[0m', s)

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
        i = 0
        while i < len(raw):
            ch = raw[i]
            # ANSI 转义序列不占显示宽度，整体原样带进 buf（着色后换行才不会错位）
            if ch == '\033' and i + 1 < len(raw) and raw[i + 1] == '[':
                match = _ANSI_SGR.match(raw, i)
                if match:
                    buf += match.group(0)
                    i = match.end()
                    continue
                # 非 SGR 转义（如 \033[2J 清屏）不做宽度处理，按普通字符继续
                buf += ch
                i += 1
                used += 1
                continue
            w = 2 if ord(ch) > 0x2E7F else 1
            if used + w > width and buf:
                cut = last_break + 1 if last_break >= 0 else len(buf)
                chunk = buf[:cut]
                # 换行处补 reset，否则颜色会溢出到框线和下一行
                if '\033[' in chunk and not chunk.endswith('\033[0m'):
                    chunk += '\033[0m'
                lines.append(chunk)
                buf = buf[cut:]
                used = _display_width(buf)
                last_break = -1
            buf += ch
            used += w
            if ch.isspace() or unicodedata.category(ch).startswith('P'):
                last_break = len(buf) - 1
            i += 1
        lines.append(buf)
    return lines

def _pad_line(text, width):
    return text + ' ' * max(0, width - _display_width(text))

def _kv(label_text, value, width=12):
    """Align a CJK label by DISPLAY width (ljust counts codepoints, not columns)."""
    return '  ' + _pad_line(label_text, width) + str(value)

def _box(kind, message, detail=None):
    """Render a result inside a frame; long text wraps instead of overflowing."""
    if _quiet(): return
    inner = _ui_width()
    print()
    print('┌' + '─' * inner + '┐')
    wrapped = _wrap_text(kind + ' ' + color_status_words(message), inner - 2)
    for pos, line in enumerate(wrapped):
        shown = _tag(kind) + line[len(kind):] if pos == 0 and line.startswith(kind) else line
        print('│' + _pad_line(' ' + shown, inner) + '│')
    if detail:
        for line in _wrap_text(color_status_words(detail), inner - 4):
            print('│' + _pad_line('   ' + line, inner) + '│')
    print('└' + '─' * inner + '┘')

def ask(prompt, default=None, secret=False, example=None, allow_back=False, allow_empty=False):
    """Prompt once. 'q' cancels the operation; 'b' (when allowed) goes one step back.

    allow_empty=True returns '' for a blank line (used for "leave empty to clear
    / disable / close"). A required field (no default, allow_empty=False) still
    refuses blank input. 'q' always yields None, which callers must not persist.
    """
    if _quiet():
        raise ValueError('该操作目前需要在终端完成（Web 端暂未实现此交互）')
    hint = ''
    if example: hint += f'（例：{example}）'
    if default:
        hint += f'（回车使用 {default}）'
        if allow_empty: hint += '（输入 - 清除）'
    elif allow_empty: hint += '（留空请直接回车）'
    if secret: hint += '（输入不回显）'
    if allow_back: hint += '（b 上一步）'
    hint += '（q 取消本次操作）'
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
            # 默认值优先：提示里写着「回车使用 X」时，空行必须返回 X，
            # 否则会出现「回车＝清空/禁用」的静默数据丢失（BUG-2-2）。
            if default is not None: return default
            if allow_empty: return ''
            print('  ' + _tag('[提示]') + ' 此项不能为空，请重新输入。')
            continue
        if allow_empty and v in ('-', 'clear', 'CLEAR'):
            return ''
        return v

_NI_LOCAL = threading.local()

@contextlib.contextmanager
def non_interactive(enabled=True):
    """非交互上下文：供 Web 端调用带 confirm() 的业务函数。

    Web 端自己负责确认（前端二次确认 + 输入确认词），所以进入本上下文后
    confirm() 直接返回 True；而 choose()/ask() 这类必须人来输入的交互
    会抛错，暴露"该流程还没做 Web 版本"，避免静默卡住或选错项。
    """
    previous = getattr(_NI_LOCAL, 'active', False)
    _NI_LOCAL.active = enabled
    try:
        yield
    finally:
        _NI_LOCAL.active = previous


def _quiet():
    """当前线程是否处于"不要往终端输出"的 Web 调用上下文。

    用线程局部标志而不是重定向全局 sys.stdout：redirect_stdout 是进程级的，
    两个并发 Web 请求交错进出会把全局 stdout 永久指向废弃的 StringIO，
    之后 journal 里再也看不到任何输出。
    """
    return bool(getattr(_NI_LOCAL, 'active', False))


def confirm(prompt, danger=False):
    if _quiet():
        return True
    if danger:
        answer = ask(f'{prompt}（确认请输入 YES，其他内容或回车取消）')
        return (answer or '').strip() == 'YES'
    return (ask(prompt + ' [y/N]（回车＝否）') or '').lower() in ('y', 'yes')

def clear_screen():
    if UI_CLEAR and _is_tty(): print('\033[2J\033[H', end='')

def section(title):
    if _quiet(): return
    print()
    print(RULE)
    print(' ' + title)
    print(RULE)

def pause(message='按回车继续'):
    if _quiet(): return
    if sys.stdin.isatty():
        try: input(f'  {message} … ')
        except EOFError: pass

def _report(title, lines, kind=None):
    """Frame a multi-line report; keeps every original line intact.

    kind 缺省时按内容自动升级标签：有失败行 → [失败]，有"需你动手"的行 → [注意]，
    否则 [提示]。避免"报告里全是 ✗、标题还是青色 [提示]"的割裂感。
    """
    if _quiet(): return
    if kind is None:
        text = '\n'.join(str(x) for x in lines)
        if any(w in text for w in _BAD_WORDS) or '✗' in text:
            kind = '[失败]'
        elif any(w in text for w in ('待手工放行', '需在面板', '跳过', '未配置')):
            kind = '[注意]'
        else:
            kind = '[提示]'
    inner = _ui_width()
    print()
    print('┌' + '─' * inner + '┐')
    print('│' + _pad_line(' ' + _tag(kind) + ' ' + title, inner) + '│')
    for raw in lines:
        text = str(raw)
        low = text
        # 前缀符号沿用"整行判定"，正文关键词交给 color_status_words，
        # 这样「已放行 3 个，未放行 1 个」里的两个词都能各自着色。
        if any(w in low for w in _BAD_WORDS):
            mark = '✗ '
        elif any(w in low for w in _GOOD_WORDS):
            mark = '✓ '
        elif any(w in low for w in ('建议', '注意', '警告', '未检测')):
            mark = '! '
        else:
            mark = ''
        body = color_status_words(text)
        if mark and UI_COLOR:
            code = {'✗ ': '31', '✓ ': '32', '! ': '33'}[mark]
            mark = '\033[1;' + code + 'm' + mark + '\033[0m'
        text = mark + body
        for line in _wrap_text(text, inner - 2):
            print('│' + _pad_line(' ' + line, inner) + '│')
    print('└' + '─' * inner + '┘')

def result_warn(message, detail=None):
    """黄色 [注意]：需要用户动手，但不是失败（如"需要你手工去面板添加"）。"""
    _box('[注意]', message, detail)


def result_ok(message, detail=None):
    _box('[成功]', message, detail)

def result_info(message, detail=None):
    _box('[提示]', message, detail)

def result_error(message, hint=None, role=None):
    """Render a failure with the four elements of 需求 §11.

    reason (message) + suggestion (hint) + log path; impact is conveyed by the
    wording of each call site. The log path is appended once, automatically.
    role selects the log file to quote; when omitted it is derived from the
    current menu context (active() only concerns frpc, never frps).
    """
    lines = []
    if hint: lines.append('建议：' + str(hint))
    if role is None:
        # active() is frpc-only; frps menus must never quote frpc.log.
        role = 'frps' if _CURRENT_ROLE == 'frps' else ('frpc' if active() else 'frps')
    try: lines.append('日志：' + error_log_path(role))
    except (ValueError, OSError): pass
    _box('[失败]', message, '\n'.join(lines) if lines else None)

def choose(items, title='请选择', back_label='返回上一级', groups=None, prelude=None):
    """Numbered menu.

    统一交互（全程序唯一规则，无冗余）：
      数字 → 执行对应操作
      0    → 返回上一级（主菜单中即为退出程序）
      其他 → 重新提示

    只有一条"离开"语义：0 = 退一层。没有"连按两次""b 别名""回车也能返回"这些
    额外规则，用户只需要记住一个键。
    """
    if _quiet():
        raise ValueError('该操作目前需要在终端完成（Web 端暂未实现此交互）')
    idx = len(items)
    width = len(str(idx))

    def _draw():
        section(title)
        if prelude is not None:
            prelude()
        for n, item in enumerate(items, 1):
            if groups and (n - 1) in groups:
                print()
                print('  ' + _dim(groups[n - 1]))
            print(f'  {n:>{width}}) {item}')
        print(f'  {0:>{width}}) {back_label}')

    _draw()
    while True:
        try:
            v = input(f'\n  请选择 [0-{idx}]：').strip()
        except EOFError:
            raise QuitProgram()
        if v == '0':
            return 0
        if v.isdecimal() and 0 <= int(v) <= idx:
            return int(v)
        if not v:
            print('  ' + _dim(f'请输入 1-{idx} 的数字，0 返回'))
        else:
            print('  ' + _dim(f'无效输入 "{v}"：请输入 0-{idx}'))

def require_active():
    pid = active()
    if not pid: err('E011', '请先新建或选择 frps 服务端')
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
        result_info('暂无服务端，请从主菜单 4「新建服务端」添加。')
    else:
        for p in ps:
            is_active = p['id'] == active()
            mark = ' ' + color_state('[当前]') if is_active else ''
            # 非当前 profile 没有独立进程，显示"运行中"是假象（BUG-2-14）
            if not is_active:
                conn = '未运行'
            else:
                try: conn = CONNECTION_STATES.get(connection(p['id'])['state'], '待确认')
                except (ValueError, OSError, KeyError, TypeError): conn = '待确认'
            print(f'  • {label(p)}{mark}  [{len(mappings(p["id"]))} 个映射]  ' + color_state(conn))
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
        ('token', 'token（必须与 frps 的 auth.token 一致；服务端未启用认证才留空）', '', True, lambda v: v),
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
                    if back == '__BACK__':
                        return   # 最外层再按 b：退出 Dashboard 子步骤，不能把哨兵值写进配置
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
    result_ok('服务端已创建。', detail=f'名称：{p["name"]}；地址：{p["server_addr"]}:{p["server_port"]}；端口映射请用主菜单 9「管理端口映射」添加。')
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

_BACKUP_REASONS = {
    'change': '自动保存前快照',
    'frpc-config': '写入客户端配置前',
    'frps-change': '改动服务端配置前',
    'pre-restore': '恢复配置前',
    'pre-delete': '删除服务端前',
    'upgrade': '升级前快照',
}

def backup_reason_text(reason):
    """Human-readable backup reason (falls back to the raw code)."""
    return _BACKUP_REASONS.get(str(reason), str(reason))

def backup_time_text(raw):
    """'20261115-230102' -> '2026-11-15 23:01'."""
    s = str(raw)
    if len(s) >= 13 and s[8:9] == '-' and s[:8].isdigit():
        return f'{s[:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}'
    return s

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
    manifest_data = read_json(manifest, {})
    files = manifest_data.get('files') or []
    digests = manifest_data.get('sha256') or {}
    if not files: raise ValueError('备份清单为空')
    restored = []
    with lock(), transaction():
        for relative in files:
            relative = Path(relative)
            if relative.is_absolute() or '..' in relative.parts: raise ValueError('备份清单包含非法路径')
            source = directory / relative
            if not source.is_file() or source.is_symlink(): raise ValueError('备份文件缺失：' + str(relative))
            # 备份完整性校验：摘要不符说明文件被截断/篡改，拒绝写入（BUG-5-1）
            expected = digests.get(str(relative)) or digests.get(relative.as_posix())
            if expected:
                actual = hashlib.sha256(source.read_bytes()).hexdigest()
                if actual != expected:
                    raise ValueError('备份文件校验失败（可能已损坏）：' + str(relative))
            target = ROOT / relative
            if not _within(target, ROOT): raise ValueError('目标路径非法')
            atomic(target, source.read_text(encoding='utf-8'))
            restored.append(str(relative))
        # 恢复后同步哈希，否则下次启动会误报「检测到外部运行配置修改」
        for role in ('frps', 'frpc'):
            if (ROOT / (role + '.toml')).exists(): mark_hash(role)
        if running('frpc'): set_pending(True)
        rebuild_index(); audit('backup-restored')
    return restored

def backup_menu():
    clear_screen()
    while True:
        items = list_backups()[:20]
        labels = [f'{backup_time_text(item["time"])}  {backup_reason_text(item["reason"])}  文件 {len(item["files"])} 个'
                  for item in items]
        labels.append('【立即创建一份手动备份】')
        n = choose(labels, '配置备份与恢复')
        if not n: return
        if n == len(labels):
            try:
                directory = backup(managed_paths(), 'manual')
                result_ok('已创建手动备份：', detail=str(directory))
            except MENU_ERRORS as exc:
                result_error('创建备份失败：' + str(exc))
            pause(); continue
        if not items:
            result_info('暂无备份可恢复；可先创建一份手动备份。'); pause(); continue
        chosen = items[n-1]
        result_info('将恢复：' + str(chosen['path']), detail='\n'.join('• ' + r for r in chosen['files'][:20]))
        if not confirm('确认用此备份覆盖当前配置？当前文件会先再次备份'):
            result_info('已取消恢复。'); continue
        try:
            backup(managed_paths(), 'pre-restore')
            restored = restore_backup(chosen['path'])
            result_ok(f'已恢复 {len(restored)} 个文件。')
            if running('frps'): result_info('frps ' + color_state('正在运行') + '；请确认后重启以使用恢复的配置。')
        except MENU_ERRORS as exc: result_error(str(exc))
        pause()

_DIAG_ADVICE = {
    'config': '检查配置文件语法；从「备份与恢复」还原，或重新运行配置向导',
    'dashboard-port': 'Dashboard 端口不可达：检查 frps 的 webServer 配置与防火墙/安全组',
    'dashboard-auth': '认证失败：核对 Dashboard 用户名与密码',
    'bindPort': '业务端口不通：检查云安全组与服务器防火墙是否放行',
    'vhostHTTPPort': 'HTTP 域名端口不通：检查安全组是否放行该端口',
    'vhostHTTPSPort': 'HTTPS 域名端口不通：检查安全组是否放行该端口',
    'connection': '连接未就绪：核对 token、TLS、服务端地址与端口',
    'server-port': '服务端端口不可达：确认 frps 已启动且安全组放行',
    'admin-port': '本地管理端口不可用：确认 frpc 正在运行',
    'proxy-error': '有映射未就绪：检查对应本地服务是否在运行、端口是否正确',
}

def _diagnostic(name):
    return {'name': name, 'advice': _DIAG_ADVICE.get(str(name), '')}


def diagnostics_report(role):
    """Machine-readable diagnostic snapshot; never includes secrets.

    每项都带 advice（中文建议），Web 端直接展示，避免"只报错不给下一步"。
    """
    def check(name, ok, detail):
        return {'name': name, 'ok': bool(ok), 'detail': str(detail),
                'advice': _DIAG_ADVICE.get(name.split('-')[-1] if False else name, '')
                          or _DIAG_ADVICE.get(name, '')}

    report = {'component': role, 'service_state': service(role, 'status').strip(),
              'config_path': str(ROOT / (role + '.toml')), 'checks': []}
    config = ROOT / (role + '.toml')
    if not config.exists():
        report['checks'].append(check('config', False, '配置不存在'))
        report['ok'] = False
        return report
    try:
        data = read_toml(config)
        verify(role, config.read_text(encoding='utf-8'))
        report['checks'].append(check('config', True, '语法校验通过'))
    except (ValueError, OSError) as exc:
        report['checks'].append(check('config', False, exc))
        report['ok'] = False
        return report

    if role == 'frps':
        web = data.get('webServer', {})
        if web.get('port'):
            host = web.get('addr', '127.0.0.1')
            probe = '127.0.0.1' if host in ('0.0.0.0', '::') else host
            reachable = port_reachable(probe, web['port'])
            report['checks'].append(check('dashboard-port', reachable,
                                          '%s:%s' % (probe, web['port'])))
            try:
                api('%s://%s:%s/api/serverinfo' % (api_scheme(web), api_host(probe), web['port']),
                    web.get('user', ''), web.get('password', ''))
                report['checks'].append(check('dashboard-auth', True, '认证成功'))
            except urllib.error.HTTPError as exc:
                report['checks'].append(check('dashboard-auth', False, 'HTTP %s' % exc.code))
            except (OSError, ValueError) as exc:
                report['checks'].append(check('dashboard-auth', False, exc))
        for key in ('bindPort', 'vhostHTTPPort', 'vhostHTTPSPort'):
            if data.get(key):
                hints = firewall_hint(data[key], 'tcp')
                report['checks'].append(check('firewall-' + key, not hints, '; '.join(hints)))
    elif active():
        profile = load_profile(active())
        report['active_profile'] = {'id': profile['id'], 'name': profile['name'],
                                    'server': label(profile)}
        reachable = port_reachable(profile['server_addr'], profile['server_port'])
        report['checks'].append(check('server-port', reachable,
                                      '%s:%s' % (profile['server_addr'],
                                                 profile['server_port'])))
        result = connection(profile['id'], fresh=True)
        report['checks'].append(check('connection', result['state'] == 'connected',
                                      result['state']))
    report['ok'] = all(item['ok'] for item in report['checks'])
    return report

def diagnostics(role):
    """Produce an actionable connection diagnostic report without exposing secrets."""
    lines = []
    ok = True
    lines.append('[诊断] ' + ('frps 服务端' if role == 'frps' else 'frpc 客户端'))
    state_value = service(role, 'status').strip().lower()
    lines.append('  服务状态：' + color_state(SERVICE_STATES.get(str(state_value).lower(), state_value or '未知')))
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
                lines.append(f'  Dashboard 端口 {probe}:{web["port"]}：' + color_state("可达" if reachable else "不可达"))
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
            lines.append(f'  服务端端口 {p["server_addr"]}:{p["server_port"]}：' + color_state("可达" if reachable else "不可达"))
            if not reachable: ok = False; lines.append('  建议：检查服务端防火墙、安全组和地址端口。')
            result = connection(pid, fresh=True)
            lines.append('  连接状态：' + color_state(CONNECTION_STATES.get(result['state'], result['state'])))
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

def save_mappings(pid, items, explicit=False, restart=True):
    """Persist mappings.

    explicit=True: `mapping ... --profile <id>` 可编辑非当前 profile（FUNC-6）。
    restart=False: 保存后不询问/执行重启（Web 端用，重启交给用户显式触发）。
    """
    if not explicit and pid != require_active(): raise ValueError('只能管理当前服务端的映射')
    load_profile(pid)
    before_count = len(mappings(pid))
    with lock(), transaction():
        if not explicit and pid != active(): raise ValueError('当前服务端已在另一终端切换，请重新打开映射菜单')
        validate_mappings(items)
        path = pdir(pid) / 'mappings.toml'; old = path.read_text(encoding='utf-8')
        backup([path]); atomic(path, toml({'schema_version': 1, 'mappings': items}))
        # 只有当前服务端才允许覆写全局运行配置 frpc.toml；否则会把正在运行的
        # 客户端配置换成另一个服务端的地址/token（BUG-2-1）。
        # apply() 的校验失败必须抛出，由 transaction() 回滚（不能吞）。
        if pid == active(): apply(pid)
        audit('mappings-saved', pid)
    effective = not running('frpc')
    message = '端口映射配置已保存。'
    detail = '映射数量：%d → %d；%s' % (
        before_count, len(items),
        '当前 frpc 未运行，下次启动时生效。' if effective
        else '当前 frpc 运行中，尚未生效——需要重启后才会加载新映射。')
    if effective: result_ok(message, detail)
    else: result_info(color_status_words(message) + '（待重启生效）', detail)
    if restart and running('frpc') and confirm('配置已保存，重启 frpc 生效？'):
        if restart_safely('frpc') == 'restarted': wait_connection(pid)

def mapping_wizard(old=None):
    """Step-by-step mapping wizard; returns None when cancelled with q.

    A single invalid field re-prompts that field instead of aborting the whole
    wizard (需求 §13.1 / 详细 §15.2(1)).
    """
    m = dict(old or {})
    # 默认值必须取「当前值」，否则编辑既有映射时一路回车会把 tcp/udp、
    # 本地地址、启用状态全部静默改掉（BUG-2-3）。
    fields = [
        ('name', '映射名称', None, lambda v: v),
        ('type', '类型 tcp/udp/http/https', m.get('type', 'tcp'), lambda v: v.lower()),
        ('local_ip', '本地地址', m.get('local_ip', '127.0.0.1'), lambda v: v),
        ('local_port', '本地端口', None, port),
        ('remote_or_domain', '远程端口/域名', None, lambda v: v),
        ('enabled', '启用？[Y/n]', 'y' if m.get('enabled', True) else 'n', lambda v: v.lower() != 'n'),
    ]
    while True:
        i = 0
        while i < len(fields):
            key, prompt, default, convert = fields[i]
            if key == 'remote_or_domain':
                # 同一字段按类型含义不同，提示与默认值都必须随之变化（UX-20 / BUG-2-3）
                if m.get('type') in ('tcp', 'udp'):
                    prompt = '远程端口（1-65535，映射到 frps 后对外暴露的端口）'
                    default = str(m['remote_port']) if m.get('remote_port') else None
                else:
                    prompt = '域名（例 nas.example.com；需 frps 已配置 vhostHTTPPort/vhostHTTPSPort）'
                    default = m.get('domain') or None
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

def _table(headers, rows, widths):
    """Render a CJK-safe table: columns are padded by DISPLAY width, not chars."""
    lines = ['  ' + ''.join(_pad_line(str(h), w) for h, w in zip(headers, widths))]
    for row in rows:
        lines.append('  ' + ''.join(_pad_line(str(c), w) for c, w in zip(row, widths)))
    return '\n'.join(lines)

def mapping_online_status(pid):
    """映射在线状态 {name: bool}；None 表示读不到（frpc 未运行/管理端口不可达）。

    对外的公有封装：Web 端等外部调用方不应直接碰私有函数。
    """
    return _proxy_status_map(pid)

def _proxy_status_map(pid):
    """Best-effort {proxy_name: True/False} online state from the local admin API."""
    try:
        prof = load_profile(pid); sec = secrets_for(pid)
        data = api(f'http://127.0.0.1:{prof.get("admin_port", 7400)}/api/status',
                   'frp-simple-bash', sec['admin_password'])
        rows = [x for group in data.values() if isinstance(group, list) for x in group if isinstance(x, dict)]
        return {str(r.get('name', '')): (r.get('status') in ('online', 'running')) for r in rows}
    except (OSError, ValueError, KeyError, TypeError):
        # None 表示"读不到"（frpc 未运行/管理端口不可达），与"没有在线映射"区分开，
        # 上层可以据此显示"未知"而不是谎报 0 在线
        return None

def show_mappings(items, pid=None):
    """Mapping table with an online column so 'enabled' is not confused with 'working'."""
    if not items:
        result_info('当前服务端暂无端口映射。'); return
    section('当前服务端端口映射')
    online = (_proxy_status_map(pid) if pid else None) or {}
    rows = []
    try:
        prof = load_profile(pid) if pid else {}
    except (ValueError, OSError, KeyError, TypeError):
        prof = {}
    server_addr = str(prof.get('server_addr', '') or '')
    for n, m in enumerate(items, 1):
        local = f'{m.get("local_ip", "127.0.0.1")}:{m["local_port"]}'
        if m['type'] in ('tcp', 'udp'):
            remote = str(m.get('remote_port', '-'))
            external = (server_addr + ':' + remote) if server_addr and remote != '-' else '-'
        else:
            remote = m.get('domain', '-')
            scheme = 'https' if m['type'] == 'https' else 'http'
            external = (scheme + '://' + remote) if remote != '-' else '-'
        enabled = '启用' if m.get('enabled', True) else '停用'
        if not m.get('enabled', True): state = '停用'
        elif not online: state = '—'
        else: state = '在线' if online.get(str(m.get('name', ''))) else '未就绪'
        rows.append([n, m['name'][:16], m['type'], local, remote, external[:26],
                     color_state(enabled), color_state(state)])
    print(_table(['序号', '名称', '类型', '本地地址', '远端', '外部地址', '启用', '状态'],
                 rows, [6, 18, 6, 20, 14, 28, 6, 8]))
    if not online:
        result_info('（映射在线状态需 frpc 运行且本机管理端口可访问，当前无法读取）')
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
            elif n == 2: show_mappings(items, pid); pause(); continue
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
                    result_info('已切换映射状态：'
                                + color_state('启用' if old_state else '停用') + ' → '
                                + color_state('停用' if old_state else '启用') + '。')
                elif n == 5:
                    name = items[index-1]['name']
                    if not confirm('确认删除映射 ' + name + '？此操作不可撤销', danger=True):
                        result_info('已取消删除。'); continue
                    items.pop(index-1)
                    result_info(f'已删除映射 {name}；映射数量：{before_count} → {len(items)}。')
                save_mappings(pid, items); result_ok('端口映射操作已完成。', detail=f'映射数量：{before_count} → {len(items)}')
                pause()
        except MENU_ERRORS as e: result_error(str(e)); pause()

def profile_summary(p, s):
    """Readable, secret-free view of the active profile."""
    web_user = s.get('dashboard_username', '')
    print('\n  服务端名称：' + p['name'])
    print('  服务端地址：' + p['server_addr'])
    print('  业务端口：' + str(p['server_port']))
    print('  Dashboard：' + color_state(('已启用，端口 ' + str(p.get('dashboard_port', 7500))) if web_user else '未启用'))
    print('  TLS：' + color_state('已启用' if p.get('tls_enable', True) else '未启用'))
    print('  token：' + color_state('已设置' if s.get('token') else '未设置'))
    print('  本地管理端口：' + str(p.get('admin_port', 7400)))
    print('  映射数量：' + str(len(mappings(p['id'])) if p.get('id') else 0))

def edit_profile():
    clear_screen()
    pid = require_active(); p = load_profile(pid); s = secrets_for(pid)
    version = hashlib.sha256((pdir(pid) / 'profile.toml').read_bytes() + (pdir(pid) / 'secrets.toml').read_bytes()).hexdigest()

    baseline = json.dumps([p, s], sort_keys=True, ensure_ascii=False)
    def is_dirty():
        return json.dumps([p, s], sort_keys=True, ensure_ascii=False) != baseline

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
        # 标题里直接显示未保存状态，用户随时知道"还没保存"（UX-14）
        title = label(p) + ('（有未保存修改）' if is_dirty() else '')
        try:
            n = choose(['查看连接配置', '修改服务端名称', '修改服务器地址', '修改服务器端口',
                        '修改 token', '修改 TLS 配置', '修改 Dashboard/API', '修改传输协议',
                        '修改日志级别与保留天数', '保存并返回', '删除当前服务端（危险）'],
                       title)
        except (QuitProgram, KeyboardInterrupt):
            # 未保存修改在退出前必须确认，否则整段编辑静默丢失（UX-08）；
            # Ctrl-C 走的是 KeyboardInterrupt，必须一并处理（BUG-4-5）。
            if is_dirty() and not confirm('有未保存的修改，确认退出并放弃？'): continue
            raise
        if not n:
            if is_dirty() and not confirm('有未保存的修改，确认返回并放弃？'): continue
            return
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
            # 默认值必须反映当前设置，否则回车会把 tls_enable=False 静默打开（BUG-2-4）
            v = ask('启用 TLS？[Y/n]', 'y' if p.get('tls_enable', True) else 'n')
            if v is not None:
                p['tls_enable'] = v.lower() != 'n'
                for key in ('certFile', 'keyFile', 'trustedCaFile'):
                    store(s, 'tls_' + key, ask('可选 ' + key + ' 路径', s.get('tls_' + key, ''), allow_empty=True))
        elif n == 7:
            v = ask('Dashboard/API 端口', str(p.get('dashboard_port', 7500)))
            if v is not None:
                field(lambda: p.__setitem__('dashboard_port', port(v)))
                store(p, 'dashboard_addr', ask('Dashboard 主机', p.get('dashboard_addr', p['server_addr']),
                                               allow_empty=True))
                store(s, 'dashboard_username', ask('Dashboard 用户名（输入 - 关闭远程查询）',
                                                   s.get('dashboard_username', ''), allow_empty=True))
                if s.get('dashboard_username'):
                    store(s, 'dashboard_password', ask('密码', secret=True, allow_empty=True))
                else:
                    s['dashboard_password'] = ''
        elif n == 8:
            v = ask('传输协议 tcp/kcp/quic/websocket/wss', p.get('transport_protocol', 'tcp'))
            if v is not None:
                protocol = v.lower()
                if protocol not in ('tcp', 'kcp', 'quic', 'websocket', 'wss'):
                    result_error('不支持的传输协议：' + protocol + '（可选 tcp/kcp/quic/websocket/wss）'); continue
                p['transport_protocol'] = protocol
                v2 = ask('启用传输加密？[y/N]', 'y' if p.get('transport_use_encryption') else 'n')
                if v2 is not None: p['transport_use_encryption'] = v2.lower() == 'y'
                v3 = ask('启用传输压缩？[y/N]', 'y' if p.get('transport_use_compression') else 'n')
                if v3 is not None: p['transport_use_compression'] = v3.lower() == 'y'
        elif n == 9:
            v = ask('日志级别 trace/debug/info/warn/error', p.get('log_level', 'info'))
            if v is not None:
                level = v.lower()
                if level not in ('trace', 'debug', 'info', 'warn', 'error'):
                    result_error('不支持的日志级别：' + level + '（可选 trace/debug/info/warn/error）'); continue
                p['log_level'] = level
                days = ask('日志保留天数（1-365）', str(p.get('log_max_days', 30)))
                if days is not None:
                    if not days.isdigit() or not 1 <= int(days) <= 365:
                        result_error('日志保留天数必须为 1-365'); continue
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
            if running('frpc') and confirm('重启 frpc 生效？'):
                if restart_safely('frpc') == 'restarted': wait_connection(pid)
            return


def _ask_validated(prompt, default=None, validator=None, secret=False,
                   allow_empty=False, example=None, step=None):
    """Ask one field until the validator accepts it.

    A mistyped field re-prompts that field only; it never aborts the caller.
    Returns None only when the user cancels with q.
    """
    label_text = (step + ' · ' + prompt) if step else prompt
    while True:
        raw = ask(label_text, default=default, secret=secret,
                  allow_empty=allow_empty, example=example)
        if raw is None: return None
        if validator is None: return raw
        try:
            return validator(raw)
        except ValueError as exc:
            result_error(str(exc), hint='该项未保存，请重新输入；其他已填项保持不变。')

def configure_server():
    """First-run frps wizard.

    Every field is validated and re-asked in place (LOGIC-3): a single typo must
    never abort the wizard, because aborting here rolls the whole install back.
    """
    step = '第 1/6 步'
    addr = _ask_validated('业务监听地址', '0.0.0.0', validator=address,
                          example='0.0.0.0 或 192.0.2.10', step=step)
    if addr is None: return
    bind_port = _ask_validated('业务端口', '7000', validator=port,
                               example='7000', step='第 2/6 步')
    if bind_port is None: return
    # token：留空自动生成；输入 none 表示明确关闭认证；否则强制 >=8 位。
    while True:
        raw = ask('第 3/6 步 · token（留空自动生成；输入 none 关闭认证）',
                  secret=True, allow_empty=True)
        if raw is None: return
        if raw == '':
            token = secrets.token_urlsafe(24)
            result_info('已自动生成随机 token：' + token,
                        detail='请立即记录；在 frpc 侧填写同一 token。本值只在此显示一次。')
            break
        if raw.lower() == 'none':
            if confirm('确认在无 token 模式下运行 frps？任何客户端都可接入。', danger=True):
                token = ''
                break
            continue
        if len(raw) < 8:
            result_error('token 长度至少 8 个字符（需求 §7.3 强制最小长度）', hint='请重新输入，或留空自动生成。')
            continue
        token = raw
        break
    data = {'bindAddr': addr, 'bindPort': bind_port,
            'auth.method': 'token', 'auth.token': token,
            'log.to': str(ROOT / 'logs' / 'frps.log'), 'log.maxDays': 30, 'log.disablePrintColor': True}
    if addr not in ('127.0.0.1', '::1'):
        result_info('业务监听 ' + addr + ' 将对公网/局域网开放，请确认防火墙与安全组规则。')
    user = ask('第 4/6 步 · Dashboard 用户名（留空关闭）', allow_empty=True)
    if user is None: return
    if user:
        host = _ask_validated('Dashboard 监听地址', '0.0.0.0', validator=address, step='第 5/6 步')
        if host is None: return
        if host not in ('127.0.0.1', '::1') and not confirm('管理接口将对外监听，确认？'): return
        while True:
            password = ask('Dashboard 密码', secret=True)
            if password is None: return
            if password: break
            result_error('启用 Dashboard 时密码不能为空')
        def _dash_port(raw):
            value = port(raw)
            if value == bind_port: raise ValueError('Dashboard 端口不能与业务端口相同')
            return value
        dash_port = _ask_validated('Dashboard 端口', '7500', validator=_dash_port, step='第 5/6 步')
        if dash_port is None: return
        data.update({'webServer.addr': host, 'webServer.port': dash_port,
                     'webServer.user': user, 'webServer.password': password})
    if confirm('第 5/6 步 · 启用 TLS（transport.tls）？生产环境建议启用'):
        while True:
            cert = ask('TLS 证书路径（PEM，留空跳过）', '', allow_empty=True)
            if cert is None: return
            if not cert: break
            key = ask('TLS 私钥路径', '', allow_empty=True)
            if key is None: return
            try:
                if not Path(cert).is_file(): raise ValueError('证书文件不存在或不可读：' + cert)
                if not Path(key).is_file(): raise ValueError('私钥文件不存在或不可读：' + key)
            except ValueError as exc:
                result_error(str(exc), hint='TLS 未保存，请重新输入路径或留空跳过。')
                continue
            data['transport.tls.certFile'] = cert
            data['transport.tls.keyFile'] = key
            break
    for key in ('vhostHTTPPort', 'vhostHTTPSPort'):
        # 空值表示「禁用」，因此校验器必须放行空串，否则会无限重问。
        value = _ask_validated(key + '（留空禁用）', None,
                               validator=lambda v: port(v) if v else '',
                               allow_empty=True, step='第 6/6 步')
        if value is None: return
        if value: data[key] = value
    # Final guard: still validate before writing, but report instead of aborting.
    try:
        validate_frps_config(expand_keys(data))
    except ValueError as exc:
        result_error('配置未通过校验：' + str(exc), hint='请重新运行配置向导或从备份恢复。')
        return
    content = toml(data); verify('frps', content)
    with lock(), transaction(): backup([ROOT / 'frps.toml']); atomic(ROOT / 'frps.toml', content); mark_hash('frps')
    if running('frps') and confirm('重启 frps？'):
        restart_safely('frps')
    else:
        result_info('frps 配置已保存；当前运行服务将在下次重启后使用新配置。')

def check_port_conflict(host, port_value, proto='tcp', exclude_pid=None, exclude_role=None):
    """Return True when ANOTHER process listens on host:port/protocol.

    Linux uses `ss -p`; macOS uses `lsof` (its equivalent, since `ss` does not
    exist there). A listener whose process name equals exclude_role (e.g. frps
    itself) is never a conflict. Returns None when the tool is unavailable.
    """
    needle = ':' + str(port_value)
    if shutil.which('ss'):
        result = run_cmd(['ss', '-lntuHp'], capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4: continue
            state = fields[0].lower()
            # ss State column: LISTEN = TCP listener, UNCONN = UDP listener.
            if proto == 'tcp' and state != 'listen': continue
            if proto == 'udp' and state != 'unconn': continue
            # ss -lntuH columns: State Recv-Q Send-Q Local Peer Process.
            local = fields[3]
            if not local.endswith(needle): continue
            if host not in ('0.0.0.0', '::', '') and not local.startswith(host): continue
            if exclude_pid and ('pid=' + str(exclude_pid)) in line: continue
            if exclude_role:
                import re as _re
                mname = _re.search(r'users:\(\("([^"]+)"', line)
                if mname and mname.group(1) == exclude_role: continue
            return True
        return False
    if sys.platform == 'darwin' and shutil.which('lsof'):
        # lsof -nP -iTCP -sTCP:LISTEN:  COMMAND PID ... NAME
        # NAME looks like "*:7500 (LISTEN)" or "127.0.0.1:7500 (LISTEN)".
        args = ['lsof', '-nP', '-i' + ('TCP' if proto == 'tcp' else 'UDP')]
        if proto == 'tcp': args += ['-sTCP:LISTEN']
        result = run_cmd(args, capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines()[1:]:  # skip header
            fields = line.split()
            if len(fields) < 9: continue
            comm = fields[0]
            name = fields[8]  # NAME column
            # Match the port in the NAME field, e.g. ":7500 (LISTEN)".
            if ':' + str(port_value) not in name and not name.endswith(':' + str(port_value)): continue
            if host not in ('0.0.0.0', '::', '', '*') and host not in name: continue
            if exclude_pid and fields[1] == str(exclude_pid): continue
            if exclude_role and comm == exclude_role: continue
            return True
        return False
    return None

def firewall_hint(port_value, proto='tcp'):
    """Detect common firewall tools and return actionable hints; never modify rules."""
    hints = []
    if shutil.which('ufw'):
        result = run_cmd(['ufw', 'status'], capture_output=True, text=True, timeout=10)
        # Match the actual rule token "PORT/PROTO" (or "PORT" alone) as a whole
        # field, not a substring: "80" must not match "8080"/"180".
        import re as _re
        allowed = False
        for line in result.stdout.splitlines():
            if not _re.search(r'\b(ALLOW|允许)', line, _re.IGNORECASE): continue
            fields = line.split()
            for tok in fields:
                if tok == f'{port_value}/{proto}' or tok == str(port_value):
                    allowed = True; break
            if allowed: break
        if 'Status: active' in result.stdout and not allowed:
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
    if restart and running('frps') and confirm('配置已保存，重启 frps 生效？'):
        outcome = restart_safely('frps')
        if outcome == 'cancelled':
            result_info('frps 配置已保存；未重启，将在下次重启后生效。')
            return
        if outcome == 'scheduled':
            result_info('frps 配置已保存；已排期后台重启，稍后生效。')
            return
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
        except MENU_ERRORS as exc: result_error(str(exc)); pause(); return
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
                    result_info('已自动生成随机 token：' + value,
                                detail='请立即记录；本值只在此显示一次。')
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
                    value = ask(key + '（回车保留，输入 - 禁用）', str(data.get(key, '')), allow_empty=True)
                    if value is None: break
                    if value: data[key] = port(value)
                    else: data.pop(key, None)
                else:
                    save_frps_config(data)
            elif n == 6:
                for key in ('kcpBindPort', 'quicBindPort', 'tcpmuxHTTPConnectPort'):
                    value = ask(key + '（回车保留，输入 - 禁用）', str(data.get(key, '')), allow_empty=True)
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
                undetectable = []
                for key in ('bindPort', 'vhostHTTPPort', 'vhostHTTPSPort', 'kcpBindPort', 'quicBindPort', 'tcpmuxHTTPConnectPort'):
                    if not data.get(key): continue
                    hit = check_port_conflict('0.0.0.0', data[key], exclude_role='frps')
                    if hit is None: undetectable.append(key)
                    elif hit:
                        result_error(f'{key}={data[key]} 已被其他进程监听。', hint='frps 自身监听该端口不算冲突；确认是否有别的服务占用了此端口。')
                if web.get('port'):
                    hit = check_port_conflict('127.0.0.1', web['port'], exclude_role='frps')
                    if hit is None: undetectable.append('webServer.port')
                    elif hit: result_error(f'Dashboard 端口 {web["port"]} 已被占用。')
                if undetectable:
                    result_info('端口占用未检测（本机缺少 ss/lsof，无法判断）：' + ', '.join(undetectable))
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
        except MENU_ERRORS as exc: result_error(str(exc)); pause()

def _human_bytes(value):
    """Render a byte count as KB/MB/GB for readability."""
    try: n = float(value)
    except (TypeError, ValueError): return str(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB': return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0
    return str(value)

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
    try:
        clients, proxies = paged('clients'), paged('proxies')
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        result_error('无法获取实时客户端信息：' + str(exc),
                     hint='确认 frps 正在运行、Dashboard/API 端口可达、用户名密码正确；'
                          '也可用「运行连接诊断」逐项检查。')
        return
    # 先给结论，再给明细，避免一屏英文刷屏后找不到重点。
    online_c = sum(1 for c in clients if c.get('online'))
    online_p = sum(1 for x in proxies if str(x.get('status', {}).get('phase', '')).lower() in ('online', 'running'))
    section('客户端连接概览')
    print(_kv('客户端', '{0} 个（{1}，{2}）'.format(
        len(clients), color_state('在线 %d' % online_c), color_state('离线 %d' % (len(clients) - online_c)))))
    print(_kv('代理', '{0} 个（{1}，{2}）'.format(
        len(proxies), color_state('在线 %d' % online_p), color_state('未就绪 %d' % (len(proxies) - online_p)))))
    print(RULE)
    if not clients and not proxies:
        result_info('暂无客户端连接。'); return
    page_size = 10
    for start in range(0, max(len(clients), 1), page_size):
        chunk = clients[start:start + page_size]
        if not chunk: break
        section(f'客户端明细 {start + 1}-{start + len(chunk)} / {len(clients)}')
        for n, item in enumerate(chunk, start + 1):
            state = '在线' if item.get('online') else '离线'
            hostname = str(item.get('hostname') or '未命名')
            # clientID 就是 frps 里的 key，也是白名单/端口映射用的标识。
            # 早期只显示 hostname，用户去白名单里根本找不到这个客户端。
            client_id = str(item.get('key') or item.get('clientID')
                            or item.get('clientId') or '')
            print(f'  {n}. {hostname}  [' + color_state(state) + ']')
            print('     clientID={0}  版本={1}  IP={2}  代理数={3}'.format(
                col_ok(client_id) if client_id else '未知',
                item.get('version', '-'), item.get('clientIP', '-'),
                item.get('status', {}).get('proxyCount', '-')))
        if start + page_size < len(clients):
            print(RULE)
            try: more = input('  回车看下一页，输入任意其它内容结束 … ')
            except EOFError: break
            if more.strip() != '': break
    if proxies:
        section('代理状态')
        phase_map = {'online': '在线', 'running': '在线', 'offline': '离线',
                     'new': '新建', 'waiting': '等待中', 'start': '启动中',
                     'closed': '已关闭', 'error': '异常'}
        for n, item in enumerate(proxies[:20], 1):
            status = item.get('status', {})
            phase = str(status.get('phase', '未知'))
            phase_text = color_state(phase_map.get(phase.lower(), phase))
            cid = str(item.get('clientID') or item.get('clientId') or '')
            print(f'  {n}. {item.get("name", "未知")} [{phase_text}]'
                  f' 类型={item.get("spec", {}).get("type", "未知")}'
                  + (' clientID=' + cid if cid else ''))
            print('     今日流量：入 {0} / 出 {1}，连接数 {2}'.format(
                _human_bytes(status.get('todayTrafficIn', 0)),
                _human_bytes(status.get('todayTrafficOut', 0)),
                status.get('curConns', 0)))
        if len(proxies) > 20:
            print(RULE)
            result_info(f'代理较多，仅显示前 20 个（共 {len(proxies)} 个）；完整数据请用 Dashboard 查看。')
    print(RULE)
    result_ok(f'查询完成：{len(clients)} 个客户端，{len(proxies)} 个代理。')


def import_external(role):
    path = ROOT / (role + '.toml'); s = state()
    if not path.exists(): return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if s.get(role + '_hash') == digest: return
    result_info('检测到外部运行配置修改。')
    n = choose(['导入外部修改', '保留管理器配置', '查看变更字段（隐藏敏感值）'], '配置同步')
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
    result_ok('外部修改已导入当前服务端配置。')

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
    """One-line compact status line.

    当前主菜单改用 _status_body（状态块）后，本函数只保留给测试与未来复用；
    不删除是因为它承载了「坏 mappings 文件不崩菜单」的回归断言。
    """
    state_text = SERVICE_STATES.get(str(data.get('service_state', '')).lower(), data.get('service_state', '未知'))
    parts = [role, color_state(state_text), str(data.get('version', ''))]
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
            parts += [label(profile), color_state(translated), count]
        else:
            parts.append(color_state('未选择服务端' if not data.get('profile_error') else '配置异常'))
    if data.get('pending'):
        parts.append(color_state('有待重启修改'))
    print('  ' + ' | '.join(str(p) for p in parts if p))

def status_concerns(role, data):
    """需要用户处理的事项（终端与 Web 共用同一份判定，避免逻辑漂移）。

    返回 [{'level': 'err'|'warn', 'text': str}]。
    「有修改等待重启」不在这里——它由状态块/状态带单独显示，避免重复。
    """
    concerns = []
    state = str(data.get('service_state', '')).lower()
    if state in ('failed', 'inactive', 'stopped'):
        concerns.append({'level': 'err', 'text': '服务未运行，可先用「启动」或查看日志排查'})
    if role == 'frpc':
        conn = (data.get('connection') or {}).get('state', '')
        if conn in ('auth-failed', 'tls-failed', 'config-error'):
            concerns.append({'level': 'err',
                             'text': '连接未就绪（' + CONNECTION_STATES.get(conn, conn)
                                     + '）：检查 token / TLS / 服务端地址'})
        elif conn in ('proxy-error', 'unreachable', 'disconnected'):
            concerns.append({'level': 'err',
                             'text': '连接异常（' + CONNECTION_STATES.get(conn, conn)
                                     + '）：检查网络与服务端状态'})
        elif conn in ('timeout', 'connecting'):
            concerns.append({'level': 'warn',
                             'text': '连接未完成（' + CONNECTION_STATES.get(conn, conn)
                                     + '）：可稍后刷新或查看诊断'})
        if data.get('profile_error'):
            concerns.append({'level': 'err',
                             'text': '当前服务端配置损坏：' + str(data['profile_error'])
                                     + '；可从「备份与恢复」恢复或重新选择服务端'})
    if role == 'frps':
        try:
            port_cfg = auto_port_config()
        except (ValueError, OSError):
            port_cfg = {'enabled': False}
        if port_cfg.get('enabled'):
            timer = ports_timer_status()
            if timer not in ('运行中', '不适用（非 systemd 平台）'):
                concerns.append({'level': 'err',
                                 'text': '端口自动开通已开启，但定时器未运行（%s）：'
                                         '新映射不会自动放行；进「6) 端口自动开通」'
                                         '选 5) 重新开启' % timer})
    return concerns

def _status_body(role, data):
    """Print the compact status fields and the 'needs attention' list (no title).

    只保留 4-5 行：版本号与配置路径对终端用户价值低，却每次占 2 行，
    会把 24 行 SSH 窗口里的「启动/停止」挤出屏幕；它们放在诊断/升级里查看即可。
    """
    print(_kv('状态', color_state(SERVICE_STATES.get(str(data['service_state']).lower(), data['service_state']))))
    if role == 'frps':
        path = ROOT / 'frps.toml'
        if path.exists():
            try:
                config = read_toml(path)
            except (ValueError, OSError) as exc:
                print(_kv('状态说明', color_state('配置文件无法解析：' + str(exc)[:60])))
                print(_kv('建议', '用「备份与恢复配置」还原，或「修改服务端配置」重建'))
                return
            host = config.get('bindAddr', '0.0.0.0')
            if ':' in host: host = '[' + host + ']'
            print(_kv('监听', str(host) + ':' + str(config.get('bindPort', 7000))))
            web = config.get('webServer', {})
            if web.get('port'):
                addr = web.get('addr', '127.0.0.1')
                if ':' in addr: addr = '[' + addr + ']'
                scheme = 'https' if web.get('tls', {}).get('certFile') else 'http'
                print(_kv('Dashboard', scheme + '://' + str(addr) + ':' + str(web['port'])))
                if web.get('addr', '127.0.0.1') in ('127.0.0.1', '::1'):
                    print(_kv('管理访问', '仅本机'))
            else: print(_kv('Dashboard', color_state('未启用')))
        else: print(_kv('状态说明', color_state('尚未生成配置，请先修改服务端配置')))
    else:
        p = data.get('active_profile')
        if p:
            print(_kv('服务端', label(p)))
            connection_data = data['connection']
            print(_kv('连接状态', color_state(CONNECTION_STATES.get(connection_data['state'], connection_data['state']))))
            try: print(_kv('映射数量', str(len(mappings(p['id'])))))
            except (ValueError, OSError, KeyError, TypeError): print(_kv('映射数量', '不可读'))
            if data.get('pending'): print(_kv('生效', color_state('有修改等待重启')))
    # Surface only what needs attention; normal details stay compact.
    concerns = status_concerns(role, data)
    if concerns:
        print()
        head = '\033[1;31m需关注\033[0m' if UI_COLOR else '需关注'
        print('  ' + head + '（以下事项需要处理）：')
        for c in concerns:
            text = c['text'] if isinstance(c, dict) else str(c)
            print('    - ' + color_state(text) if UI_COLOR else '    - ' + text)

def show_status(role, data=None):
    """Render a standalone status screen (title + body + rule)."""
    if data is None: data = status(role)
    section('当前状态')
    _status_body(role, data)
    print(RULE)
    return data

def _restart_command(role):
    """Platform command list that restarts the service."""
    if os.name == 'nt':
        wrapper = BIN.parent / 'services' / ('frp-simple-bash-' + role + '.exe')
        return [str(wrapper), 'restart']
    if shutil.which('systemctl') and Path('/run/systemd/system').exists():
        return ['systemctl', 'restart', role]
    if shutil.which('rc-service'):
        return ['rc-service', role, 'restart']
    if sys.platform == 'darwin' and shutil.which('launchctl'):
        return ['launchctl', 'kickstart', '-k', 'system/frp-simple-bash.' + role]
    return None

def schedule_detached_restart(role, delay=2):
    """Restart in a NEW session so it survives the caller's SSH dropping.

    When frpc maps the user's SSH port, restarting frpc kills the very session
    that is running this menu: the command would be suspended mid-way and never
    finish. Detaching (setsid) + a short delay lets this command return first,
    then the restart completes independently of the SSH connection.
    """
    cmd = _restart_command(role)
    if not cmd: raise ValueError('无支持的服务管理器，无法重启')
    script = 'sleep %d; exec %s' % (int(delay), ' '.join(shlex.quote(c) for c in cmd))
    subprocess.Popen(['/bin/sh', '-c', script],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    # 不在这里清 pending：重启尚未真正发生，提前清会把"未生效"误标成"已生效"。
    # 进程真正重启后，refresh_pending() 会依据「进程启动时间 > 配置修改时间」自动清除。
    audit('service-restart-scheduled')

def _has_enabled_mappings():
    """True when the ACTIVE profile has an enabled mapping (a tunnel may be in use).

    只看当前运行的 profile：非当前 profile 的映射不会影响本次重启。
    读取失败时保守返回 True，宁可多问一次"延迟重启"也不要切断 SSH（BUG-4-1）。
    """
    pid = active()
    if not pid: return False
    try:
        return any(m.get('enabled', True) for m in mappings(pid))
    except (ValueError, OSError, KeyError, TypeError):
        return True

def restart_safely(role, delay=2):
    """Restart with an SSH-tunnel-safe path.

    Returns 'restarted' | 'scheduled' | 'cancelled'. When frpc has enabled
    mappings the user is offered a detached delayed restart, because an
    immediate restart would cut the SSH session they are typing in.
    """
    if not _is_tty():
        schedule_detached_restart(role, delay)
        result_info(f'已在后台计划 {delay} 秒后重启 {role}（非交互模式）。')
        return 'scheduled'
    if role == 'frpc' and _has_enabled_mappings():
        n = choose(['立即重启（当前连接会瞬断）',
                    '后台延迟重启（推荐：正通过映射端口 SSH 登录时选这个）'], '重启 frpc')
        if not n: return 'cancelled'
        if n == 2:
            schedule_detached_restart(role, delay)
            result_info(f'已在后台计划 {delay} 秒后重启 frpc。',
                        detail='若你正通过映射端口 SSH 登录，本连接会断开；重连后新映射/配置已生效。')
            return 'scheduled'
    service_feedback(role, 'restart')
    return 'restarted'

def service_feedback(role, action):
    """Actually perform the service action, then report the resulting state."""
    labels = {'start': '启动', 'stop': '停止', 'restart': '重启'}
    service(role, action)
    state_value = service(role, 'status').strip().lower()
    expected = {'start': ('active', 'started', 'running'), 'stop': ('inactive', 'stopped', 'dead'), 'restart': ('active', 'started', 'running')}[action]
    if state_value in expected:
        result_ok(f'{labels[action]}成功：当前状态为 {"运行中" if action != "stop" else "已停止"}。')
    else:
        result_error(f'{labels[action]}请求已发送，但当前服务状态为：'
                     + color_state(SERVICE_STATES.get(str(state_value).lower(), state_value or '未知')) + '。请查看日志。')
    return state_value

def main_menu(role):
    """Main menu. 进入后先显示《当前状态》，再进入操作菜单。"""
    global _CURRENT_ROLE
    _CURRENT_ROLE = role
    while True:
        clear_screen()
        refresh_pending(role)
        try:
            data = status(role)
            profile = data.get('active_profile') or {}
            alias = profile.get('name', '未选择') if isinstance(profile, dict) else '未选择'
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
            result_error('配置无法读取；请检查配置文件或从 backups 恢复，服务管理仍可使用。')
            alias = '配置错误'
            data = {'component': role, 'version': VERSION, 'service_state': 'unknown',
                    'config_path': str(ROOT / (role + '.toml')), 'pending': False}
        if role == 'frpc':
            short = _shorten(alias, 12)
            items = [
                '启动', '停止', '重启',
                '新建服务端', '查看服务端',
                f'切换服务端（当前 {short}）',
                f'修改服务端（{short}）配置',
                '删除服务端/数据',
                f'管理（{short}）端口映射',
                '显示所有映射',
                '查看日志与连接诊断', 'Web 管理端', '备份与恢复配置',
                '设置/取消开机自启动', '卸载 frpc', '升级 frpc',
            ]
            groups = {0: '── 服务控制 ──', 3: '── 服务端管理（FRPS） ──',
                      8: '── 端口映射管理 ──', 10: '── 运维 ──', 14: '── 危险操作 ──'}
        else:
            items = [
                '启动', '停止', '重启',
                '查看客户端连接信息', '修改服务端配置', '端口自动开通',
                '查看日志与连接诊断', '备份与恢复配置', '设置/取消开机自启动',
                '卸载 frps', '升级 frps',
            ]
            groups = {0: '── 服务控制 ──', 3: '── 配置与连接 ──',
                      6: '── 运维 ──', 9: '── 危险操作 ──'}
        def _prelude(_role=role, _data=data):
            # 状态块紧跟菜单标题显示（用户要求的布局）
            _status_body(_role, _data)
            if _role == 'frpc' and not profiles():
                print()
                print('  ' + _tag('[提示]') + ' 还没有服务端配置，建议按顺序：')
                print('    ① 新建服务端（本菜单 4）  →  ② 管理端口映射（本菜单 9）  →  ③ 启动并验证（本菜单 1）')
        n = choose(items, 'frpc 客户端操作' if role == 'frpc' else 'frps 服务端操作',
                   '退出', groups=groups, prelude=_prelude)
        if not n: return
        needs_pause = True
        terminal = False
        try:
            if role == 'frpc':
                if n == 1:
                    require_active()
                    service_feedback(role, 'start'); wait_connection(active())
                elif n == 2:
                    service_feedback(role, 'stop')
                    result_info('frpc 已停止，当前配置和 profile 保留。')
                elif n == 3:
                    if restart_safely(role) == 'restarted': wait_connection(active())
                elif n == 4: profile_wizard()
                elif n == 5: list_profiles()
                elif n == 6:
                    pid = select_profile()
                    if pid: switch(pid)
                    else: needs_pause = False
                elif n == 7: edit_profile(); needs_pause = False
                elif n == 8: delete_server_menu(); needs_pause = False
                elif n == 9: mapping_menu(); needs_pause = False
                elif n == 10: show_all_mappings()
                elif n == 11: logs_and_diagnostics_menu(role); needs_pause = False
                elif n == 12: web_manager_menu(); needs_pause = False
                elif n == 13: backup_menu(); needs_pause = False
                elif n == 14:
                    if not autostart(role): needs_pause = False
                elif n == 15: terminal = uninstall_menu(role)
                elif n == 16: terminal = upgrade(role)
            else:
                if n == 1: service_feedback(role, 'start')
                elif n == 2:
                    service_feedback(role, 'stop')
                    result_info('frps 已停止。')
                elif n == 3: restart_safely(role)
                elif n == 4: server_clients()
                elif n == 5: frps_config_menu(); needs_pause = False
                elif n == 6: auto_port_menu(); needs_pause = False
                elif n == 7: logs_and_diagnostics_menu(role); needs_pause = False
                elif n == 8: backup_menu(); needs_pause = False
                elif n == 9:
                    if not autostart(role): needs_pause = False
                elif n == 10: terminal = uninstall_menu(role)
                elif n == 11: terminal = upgrade(role)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as e:
            result_error(str(e))
        if terminal:
            return
        if needs_pause:
            pause()


class _ManagerProxy(object):
    """把"当前 manager 模块的全局命名空间"活着代理出去。

    webui 里的 import manager 会再执行一份 manager.py（模块级缓存、异常类、
    ROOT 全都双份），所以这里注入一个代理：属性访问实时走本模块的 globals()。
    用代理而不是 `sys.modules[__name__]` 的好处是——即使 manager 被 importlib
    直接执行、没注册进 sys.modules（测试场景），也能正确注入。
    """

    __slots__ = ('_namespace',)

    def __init__(self, namespace):
        object.__setattr__(self, '_namespace', namespace)

    def __getattr__(self, name):
        try:
            return self._namespace[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self._namespace[name] = value


def _webui_module():
    """按需加载 webui 模块（manager 不能静态 import 它，否则循环依赖）。"""
    import importlib.util as _ilu
    path = Path(__file__).resolve().parent / 'webui.py'
    if not path.exists():
        raise ValueError('Web 管理端后端缺失：' + str(path) + '（请重新安装）')
    spec = _ilu.spec_from_file_location('frp_webui', str(path))
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.bind_manager(_ManagerProxy(globals()))
    return module


def _web_access_url(webui):
    cfg = webui.load_web_config()
    bind = str(cfg.get('bind') or '127.0.0.1')
    port = int(cfg.get('port') or 7501)
    host = '[%s]' % bind if ':' in bind else bind
    if bind in ('0.0.0.0', '::'):
        host = '127.0.0.1'
        note = '（已绑定所有网卡，本机访问用 127.0.0.1，其它机器用本机内网 IP）'
    else:
        note = ''
    return 'http://%s:%d/' % (host, port), bind, port, note


def web_manager_menu():
    """Web 管理端子菜单：查看地址、启停服务、改监听、重置密码。"""
    while True:
        clear_screen()
        try:
            webui = _webui_module()
            url, bind, port, note = _web_access_url(webui)
        except (ValueError, OSError) as exc:
            result_error(str(exc), hint='重新运行安装脚本可补回 Web 端文件。')
            pause(); return
        status = web_service_status()
        installed = web_service_installed()

        def _prelude():
            print('  ' + _kv('服务状态', color_state(status)))
            print('  ' + _kv('访问地址', url + ('  ' + _dim(note) if note else '')))
            print('  ' + _kv('配置文件', str(Path(ROOT) / 'web.json')))
            if status in ('运行中', 'active', 'started'):
                print('  ' + _dim('浏览器打开即可；远程服务器可用 SSH 隧道：'))
                print('  ' + _dim('  ssh -L %d:127.0.0.1:%d 用户名@服务器' % (port, port)))

        items = [
            '查看访问地址',
            '停止并禁用服务' if installed and status not in ('已停止', '未安装') else '安装并启动服务',
            '修改监听地址（端口/绑定）',
            '重置面板密码',
            '移除常驻服务' if installed else '查看安装说明',
        ]
        n = choose(items, 'Web 管理端', '返回上一级', prelude=_prelude)
        if not n: return
        try:
            if n == 1:
                section('访问地址')
                print('  ' + url)
                print()
                print('  ' + _dim('本机直接打开；远程服务器建议用 SSH 隧道：'))
                print('  ' + _dim('  ssh -L %d:127.0.0.1:%d 用户名@服务器' % (port, port)))
                print('  ' + _dim('  然后在本地浏览器打开 http://127.0.0.1:%d/' % port))
                pause()
            elif n == 2:
                if installed and status not in ('已停止', '未安装'):
                    ok, message = web_service_disable()
                    if ok: result_ok('已停止并禁用 Web 服务。', detail='恢复：再次进入本菜单选择「安装并启动服务」')
                    else: result_error('停用失败：' + (message or '未知原因'))
                else:
                    if not installed:
                        ok, message = web_service_install()
                        if ok: result_ok('已安装并启动 Web 服务。', detail='访问地址：' + url)
                        else: result_error('安装失败：' + (message or '未知原因'),
                                           hint='可手动执行 frpc web --install-service 查看详细原因')
                    else:
                        ok, message = web_service_enable()
                        if ok: result_ok('已启用并启动 Web 服务。', detail='访问地址：' + url)
                        else: result_error('启动失败：' + (message or '未知原因'))
                pause()
            elif n == 3:
                section('修改监听地址')
                print('  ' + _dim('当前：%s:%d' % (bind, port)))
                new_bind = ask('绑定地址（回车保持不变；0.0.0.0 表示所有网卡）',
                               bind, allow_empty=True)
                if new_bind is None or new_bind == '': new_bind = bind
                new_port_raw = ask('端口（回车保持不变）', str(port), allow_empty=True)
                if new_port_raw is None: new_port_raw = str(port)
                try:
                    new_port = int(new_port_raw or port)
                except ValueError:
                    raise ValueError('端口必须是数字')
                if not 1 <= new_port <= 65535: raise ValueError('端口必须在 1-65535 之间')
                cfg = webui.load_web_config()
                # 非回环地址 = 把 root 控制面暴露到网络，必须已设密码
                if new_bind not in ('127.0.0.1', '::1') and not cfg.get('password_hash'):
                    raise ValueError('绑定非回环地址前必须先设置面板密码（本菜单第 4 项）')
                if (new_bind, new_port) != (bind, port):
                    if not _web_port_free(new_bind, new_port):
                        raise ValueError('端口 %d 已被占用，请换一个' % new_port)
                    cfg['bind'], cfg['port'] = new_bind, new_port
                    webui.save_web_config(cfg)
                    result_ok('已保存监听地址：%s:%d' % (new_bind, new_port))
                    if installed and confirm('立即重启 Web 服务使其生效？'):
                        ok, message = web_service_restart()
                        if ok:
                            result_ok('已重启：http://%s:%d/' % (new_bind, new_port))
                        else:
                            result_error('重启失败：' + (message or '未知原因'),
                                         hint='可稍后执行 frpc web --restart')
                    else:
                        result_info('稍后可用 frpc web --restart 或本菜单第 2 项重启。')
                else:
                    result_info('监听地址未变化。')
                pause()
            elif n == 4:
                section('重置面板密码')
                print('  ' + _dim('至少 8 位；输入不回显。忘记旧密码也可以直接重置（需要 root）。'))
                first = ask('新密码', secret=True, allow_empty=True)
                if first is None or first == '': result_info('已取消。'); pause(); continue
                if len(first) < 8: result_error('密码至少 8 位。'); pause(); continue
                second = ask('再输入一次', secret=True, allow_empty=True)
                if second != first: result_error('两次输入不一致。'); pause(); continue
                cfg = webui.load_web_config()
                cfg['password_hash'] = webui.hash_password(first)
                cfg['must_change_password'] = False
                webui.save_web_config(cfg)
                web_audit_via(webui, 'password-reset')
                result_ok('面板密码已更新。', detail='若使用常驻服务，重启后生效。')
                pause()
            elif n == 5:
                if installed:
                    if not confirm('移除 Web 管理端常驻服务？面板将无法访问，frpc 本体不受影响。', danger=True):
                        result_info('已取消。'); pause(); continue
                    ok = web_service_uninstall()
                    if ok: result_ok('已移除 Web 管理端常驻服务。')
                    else: result_error('移除失败，请检查权限或手动删除服务单元。')
                else:
                    section('安装说明')
                    print('  安装 frpc 时会自动安装 Web 管理端；若当时跳过了，可执行：')
                    print('  ' + _dim('  sudo frpc web --install-service'))
                    print('  ' + _dim('  或在本菜单第 2 项选择「安装并启动服务」'))
                pause()
        except (ValueError, OSError) as exc:
            result_error(str(exc)); pause()


def _web_port_free(bind, port):
    """端口能否绑定（仅探测，不占用）。"""
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((str(bind), int(port)))
        return True
    except OSError:
        return False
    finally:
        try: probe.close()
        except OSError: pass


def web_audit_via(webui, action):
    """透过 webui 的审计通道记录一次操作（保持日志格式统一）。"""
    try:
        webui.web_audit(action, result='ok', detail='via menu')
    except (AttributeError, OSError):
        pass


def delete_server_menu():
    """先选服务端，再选「清空映射」或「删除服务端」两种删除方式。"""
    ps = profiles()
    if not ps:
        result_info('暂无服务端，请先新建。'); return
    n = choose([label(p) + (' [当前]' if p['id'] == active() else '') for p in ps],
               '选择要处理的服务端')
    if not n: return
    target = ps[n - 1]
    mode = choose([f'清空「{target["name"]}」的全部端口映射（保留服务端）',
                   f'删除「{target["name"]}」（服务端与全部映射一并删除）'],
                  '删除方式')
    if not mode: return
    if mode == 1:
        count = len(mappings(target['id']))
        if count == 0:
            result_info('该服务端当前没有映射。'); return
        if not confirm(f'将清空 {count} 条映射，服务端配置保留；此操作不可恢复，确认继续？',
                       danger=True): return
        save_mappings(target['id'], [], explicit=True)
        if target['id'] == active():
            apply(target['id'])
        result_ok(f'已清空 {count} 条映射，服务端「{target["name"]}」保留。')
        return
    delete_profile_id(target['id'])

def show_all_mappings():
    """Read-only overview: every profile and every mapping under it."""
    ps = profiles()
    if not ps:
        result_info('暂无服务端，请先新建。'); return
    section('所有服务端的端口映射')
    total = 0
    for p in ps:
        try:
            rows = mappings(p['id'])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            print('  ' + label(p) + '  ' + color_state('映射不可读：' + str(exc)))
            continue
        total += len(rows)
        mark = ' [当前]' if p['id'] == active() else ''
        print('  ' + label(p) + mark + f'（{len(rows)} 条）')
        if not rows:
            print('     （无映射）')
        for m in rows:
            local = f'{m.get("local_ip", "127.0.0.1")}:{m["local_port"]}'
            if m['type'] in ('tcp', 'udp'): remote = str(m.get('remote_port', '-'))
            else: remote = m.get('domain', '-')
            state = color_state('启用' if m.get('enabled', True) else '停用')
            print('     - {0}  {1}  {2} → {3}  {4}'.format(m['name'], m['type'], local, remote, state))
    print(RULE)
    result_info(f'共 {len(ps)} 个服务端、{total} 条映射。')

def log_files(role):
    base = ROOT / 'logs'
    if not base.exists(): return []
    return sorted([p for p in base.iterdir() if p.is_file() and not p.is_symlink() and (p.name == role + '.log' or p.name.startswith(role + '.log.'))], key=lambda p: p.stat().st_mtime, reverse=True)

_LOG_BAD = ('error', 'fatal', 'panic')
_LOG_WARN = ('warn',)

def _highlight_log_line(line):
    """Colour whole log lines that contain ERROR/FATAL or WARN (no-op without colour)."""
    if not UI_COLOR: return line
    low = line.lower()
    if any(w in low for w in _LOG_BAD): return '\033[1;31m' + line + '\033[0m'
    if any(w in low for w in _LOG_WARN): return '\033[1;33m' + line + '\033[0m'
    return line

def show_logs(role, lines=50, only_errors=False):
    path = ROOT / 'logs' / (role + '.log')
    if path.exists():
        content = path.read_text(encoding='utf-8', errors='replace').splitlines()
        recent = content[-lines:]
        errors = sum(1 for x in recent if any(w in x.lower() for w in _LOG_BAD))
        warns = sum(1 for x in recent if any(w in x.lower() for w in _LOG_WARN))
        if only_errors:
            recent = [x for x in recent if any(w in x.lower() for w in _LOG_BAD + _LOG_WARN)]
            title = f'{role} 日志 · 最近 {lines} 行中的错误/警告（{len(recent)} 条）'
        else:
            title = f'{role} 日志（最近 {min(lines, len(content))} 行）'
        section(title)
        for line in recent:
            print(_highlight_log_line(line))
        print(RULE)
        if not only_errors:
            result_info(f'共 {len(content)} 行；本页 {len(recent)} 行，其中错误 {errors} 条、警告 {warns} 条。',
                        detail='红色=错误，黄色=警告。菜单「只看错误」可过滤。')
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
        n = choose(['查看最近 50 行日志', '查看最近 200 行日志', '只看错误与警告',
                    '持续跟踪日志（Ctrl-C 返回）', '查看日志目录与轮转策略',
                    '清理轮转归档（保留当前日志）', '运行连接诊断', '导出诊断报告（文件）'],
                   '日志与连接诊断', '返回上一级')
        if not n: return
        if n == 1: show_logs(role, 50); pause()
        elif n == 2: show_logs(role, 200); pause()
        elif n == 3: show_logs(role, 500, only_errors=True); pause()
        elif n == 4: follow_log(role, 50); pause()
        elif n == 5:
            result_info('日志目录：' + str(ROOT / 'logs'),
                        detail='轮转策略：单文件超过 50MB 自动归档；保留 30 天；总量上限 500MB。'); pause()
        elif n == 6:
            archives = [f for f in files if f.name != role + '.log']
            if not archives:
                result_info('没有可清理的轮转归档。'); pause(); continue
            total = sum(f.stat().st_size for f in archives if f.exists())
            if not confirm(f'将删除 {len(archives)} 个归档文件（约 {total // 1024 // 1024} MB），当前日志保留。'
                           '此操作不可恢复，确认继续？', danger=True):
                result_info('已取消。'); pause(); continue
            removed = 0
            for item in archives:
                unlink_quiet(item); removed += 1
            result_ok(f'已清理 {removed} 个轮转归档，当前日志保留。'); pause()
        elif n == 7: diagnostics(role); pause()
        elif n == 8:
            report = diagnostics_report(role)
            target = ROOT / 'logs' / ('diagnostics-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
            atomic(target, json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            result_ok('诊断报告已导出：', detail=str(target))
            pause()

def autostart(role):
    # Windows services expose three start types; systemd/OpenRC expose enable/disable.
    if os.name == 'nt':
        n = choose(['自动（开机启动）', '手动（不自动启动）', '禁用'])
        if not n: return False
        action = ('enable', 'manual', 'disable')[n - 1]
        label = ('自动', '手动', '禁用')[n - 1]
    else:
        n = choose(['启用开机自启动', '取消开机自启动'])
        if not n: return False
        action = 'enable' if n == 1 else 'disable'
        label = '启用' if n == 1 else '取消'
    service(role, action)
    current = service(role, 'status')
    result_ok(f'已设置 {role} 启动类型为「{label}」；当前运行状态：'
              + color_state(SERVICE_STATES.get(str(current).lower(), current or '未知')) + '。')
    return True

def set_autostart(role, enabled, manual=False):
    """非交互设置开机自启（Web 端复用）。Windows 额外支持 manual。"""
    if os.name == 'nt':
        action = 'enable' if enabled else ('manual' if manual else 'disable')
    else:
        action = 'enable' if enabled else 'disable'
    service(role, action)
    return service(role, 'status')

def autostart_enabled(role):
    """当前是否开机自启；平台不支持时返回 None。"""
    try:
        if os.name == 'nt':
            out = _cmd_out(['sc', 'qc', role])
            return 'AUTO_START' in out.upper()
        if shutil.which('systemctl'):
            out = _cmd_out(['systemctl', 'is-enabled', role])
            return out.strip().lower() in ('enabled', 'enabled-runtime', 'static')
        out = _cmd_out(['rc-update', 'show', 'default'])
        return role in out
    except (OSError, ValueError):
        return None

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
                    detail='请用主菜单 6「切换服务端」选择一个，或 4「新建服务端」。')
    else:
        result_info('已删除当前服务端，当前没有可用服务端。',
                    detail='请先用主菜单 4「新建服务端」创建，再回到 1「启动」。')

def delete_profile_id(pid):
    p = load_profile(pid)
    was_active = pid == active()
    # Both confirmations happen BEFORE any side effect, so cancelling the
    # second one never leaves frpc stopped (LOGIC-2).
    if was_active and running('frpc'):
        if not confirm('删除当前服务端将停止 frpc，确认继续？', danger=True): return
    if not confirm('删除 ' + label(p) + ' 及其全部端口映射？删除前会自动备份，可从「备份与恢复配置」恢复。'
                   '确认继续？', danger=True): return
    was_frpc_running = bool(was_active and running('frpc'))
    with lock(), transaction():
        folder = pdir(pid)
        # 删除前留一份快照，让"撤销"有真实落点（UX-13）
        try:
            backup([folder / 'profile.toml', folder / 'secrets.toml', folder / 'mappings.toml'], 'pre-delete')
        except (ValueError, OSError):
            pass
        if folder.parent != ROOT / 'profiles': raise ValueError('删除路径错误')
        shutil.rmtree(folder)
        if was_active:
            set_current(''); unlink_quiet(ROOT / 'frpc.toml')
            _clear_runtime_state()
        rebuild_index(); audit('profile-deleted')
    # 事务成功后才停服务：万一事务失败，也不会留下"服务已停但配置还在"的半状态（BUG-2-13）
    if was_frpc_running:
        try: service('frpc', 'stop')
        except (ValueError, OSError): pass
    _after_profile_removed(was_active)

def mapping_command(argv, flags=None):
    """mapping list/add/edit/enable/disable/delete [--profile <id>]."""
    flags = flags or {}
    explicit = bool(flags.get('profile'))
    pid = flags.get('profile') or require_active()
    if explicit:
        load_profile(pid)   # 校验请求的 profile 存在
    items = mappings(pid)
    action = argv[0] if argv else 'menu'
    if action == 'list':
        print(json.dumps(items, ensure_ascii=False, indent=2)); return
    if action == 'add':
        created = mapping_wizard()
        if created is None: return
        items.append(created); save_mappings(pid, items, explicit=explicit); return
    if action in ('edit', 'enable', 'disable', 'delete'):
        if len(argv) < 2: raise ValueError('需要映射名称：mapping ' + action + ' <name>')
        name_arg = argv[1]; index = next((i for i, item in enumerate(items) if item['name'] == name_arg), None)
        if index is None: err('E012', '映射不存在：' + name_arg)
        # 非交互环境（管道/脚本）必须显式 --yes，否则不能"确认失败后静默成功退出"（BUG-2-7）
        skip_confirm = bool(flags.get('yes'))
        if not TEST and not skip_confirm and not sys.stdin.isatty():
            err('E015', '非交互环境下的写操作需要 --yes 明确授权。')
        if action == 'edit':
            edited = mapping_wizard(items[index])
            if edited is None: return
            items[index] = edited
        elif action == 'enable': items[index]['enabled'] = True
        elif action == 'disable': items[index]['enabled'] = False
        elif action == 'delete':
            if not skip_confirm and not confirm('确认删除映射 ' + name_arg + '？此操作不可撤销', danger=True): return
            items.pop(index)
        save_mappings(pid, items, explicit=explicit); return
    if action == 'menu': mapping_menu(); return
    raise ValueError('支持 mapping list/add/edit/enable/disable/delete')

# ============================================================================
# 端口自动开通（auto-port）—— 路线 A：服务端被动同步
# 数据源是 frps Dashboard 里"实际已注册并在线"的 proxy，而不是客户端声明，
# 因此客户端不需要持有任何凭据，也无法凭一次请求打开任意端口。
# ============================================================================

AUTO_PORT_DENY_DEFAULT = (
    21, 22, 23, 25, 53, 110, 111, 135, 137, 138, 139, 161, 162, 389, 445,
    465, 512, 513, 514, 587, 636, 873, 993, 995, 1080, 1433, 1521, 2049,
    2375, 2376, 2379, 3306, 3389, 5432, 5900, 6379, 6443, 7001, 7002,
    9200, 9300, 10250, 11211, 27017,
)

def auto_port_config_path(): return ROOT / 'auto-port.toml'
def auto_port_state_path(): return ROOT / 'ports-state.json'

def auto_port_audit_path():
    path = ROOT / 'logs' / 'ports-audit.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path

def ports_audit(action, **fields):
    """Append one audit record. Fixed key set; never writes secrets."""
    record = {'time': dt.datetime.now().isoformat(timespec='seconds'), 'action': str(action)}
    for key in ('client_id', 'proxy', 'port', 'proto', 'result', 'detail'):
        if key in fields: record[key] = fields[key]
    try:
        path = auto_port_audit_path()
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        if os.name != 'nt':
            try: os.chmod(str(path), 0o600)
            except OSError: pass
    except OSError:
        pass

def auto_port_config():
    """Load auto-port config, filling documented defaults."""
    raw = read_toml(auto_port_config_path()) if auto_port_config_path().exists() else {}
    if not isinstance(raw, dict): raw = {}
    try:
        max_ports = int(raw.get('max_ports_per_client', 20))
    except (TypeError, ValueError):
        max_ports = 20
    try:
        interval = int(raw.get('sync_interval_seconds', 15))
    except (TypeError, ValueError):
        interval = 15
    try:
        prune = int(raw.get('prune_after_seconds', 120))
    except (TypeError, ValueError):
        prune = 120
    deny = raw.get('deny_ports')
    if deny is None:
        deny_list = list(AUTO_PORT_DENY_DEFAULT)
    else:
        deny_list = []
        for item in deny:
            try: deny_list.append(int(item))
            except (TypeError, ValueError): continue
    allow, seen_allow = [], set()
    for item in (raw.get('allow_clients') or []):
        text = str(item).strip()
        # 去重：历史数据/多次添加可能产生重复项，既占地方又让菜单看起来错乱
        if text and text not in seen_allow:
            seen_allow.add(text); allow.append(text)
    return {
        'enabled': bool(raw.get('enabled', False)),
        'mode': str(raw.get('mode', 'firewall')),
        'allow_clients': allow,
        'max_ports_per_client': max(0, max_ports),
        'deny_ports_enabled': bool(raw.get('deny_ports_enabled', True)),
        'deny_ports': deny_list,
        'sync_interval_seconds': max(5, interval),
        'prune_after_seconds': max(10, prune),
        'panel_mode': str(raw.get('panel_mode', 'auto') or 'auto').lower(),
    }

def save_auto_port_config(cfg):
    """Persist auto-port config with 0600 permissions.

    必须**保留**文件里已有的其它键（尤其 [panel] 段）。
    早期版本整体重写成固定字段集，导致任何一次保存（开关自动同步、改白名单）
    都会把用户配好的宝塔 API key 抹掉——这就是"key 过一段时间就没了"的根因。
    """
    path = auto_port_config_path()
    raw = read_toml(path) if path.exists() else {}
    if not isinstance(raw, dict): raw = {}
    payload = {
        'schema_version': 1,
        'enabled': bool(cfg['enabled']),
        'mode': str(cfg['mode']),
        'allow_clients': list(cfg['allow_clients']),
        'max_ports_per_client': int(cfg['max_ports_per_client']),
        'deny_ports_enabled': bool(cfg['deny_ports_enabled']),
        'deny_ports': list(cfg['deny_ports']),
        'sync_interval_seconds': int(cfg['sync_interval_seconds']),
        'prune_after_seconds': int(cfg['prune_after_seconds']),
        'panel_mode': str(cfg.get('panel_mode', 'auto')),
    }
    # 保留文件中的其它所有键；[panel] 段原样带过去
    for key, value in raw.items():
        if key in payload: continue
        payload[key] = value
    atomic(path, toml(payload))
    try: os.chmod(str(auto_port_config_path()), 0o600)
    except OSError: pass
    audit('auto-port-config-saved')

def auto_port_state():
    data = read_json(auto_port_state_path(), {})
    if not isinstance(data, dict): data = {}
    opened = data.get('opened')
    return {'schema_version': 1, 'backend': str(data.get('backend', '')),
            'opened': opened if isinstance(opened, list) else []}

def save_auto_port_state(st):
    save_json(auto_port_state_path(), st)

# ---------------------------------------------------------------- 防火墙兼容层
# 目标：不同发行版用不同防火墙（ufw / firewalld / nftables / iptables / Windows），
# 这里统一探测、统一操作，并对"未启用"的情况给出可执行的启用方案。

def os_release():
    """Parse /etc/os-release into a dict (best effort)."""
    data = {}
    try:
        text = Path('/etc/os-release').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return data
    for line in text.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data

def os_family():
    """Return 'debian' | 'rhel' | 'suse' | 'alpine' | 'arch' | 'windows' | 'other'."""
    if os.name == 'nt': return 'windows'
    rel = os_release()
    joined = ((rel.get('ID') or '') + ' ' + (rel.get('ID_LIKE') or '')).lower()
    for key, family in (('debian', 'debian'), ('ubuntu', 'debian'),
                        ('rhel', 'rhel'), ('centos', 'rhel'), ('fedora', 'rhel'),
                        ('rocky', 'rhel'), ('alma', 'rhel'), ('oracle', 'rhel'),
                        ('suse', 'suse'), ('opensuse', 'suse'),
                        ('alpine', 'alpine'), ('arch', 'arch'), ('manjaro', 'arch')):
        if key in joined: return family
    return 'other'

def preferred_firewall():
    """Distro-appropriate firewall manager to recommend."""
    family = os_family()
    if family == 'debian': return 'ufw'
    if family in ('rhel', 'suse'): return 'firewalld'
    if family == 'windows': return 'windows'
    return 'nftables'          # alpine / arch / other 默认 nftables

def _cmd_out(args, timeout=10):
    try:
        return (run_cmd(args, capture_output=True, text=True, timeout=timeout).stdout or '')
    except (OSError, ValueError, subprocess.SubprocessError):
        return ''

def _cmd_ok(args, timeout=10):
    try:
        return run_cmd(args, capture_output=True, text=True, timeout=timeout).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

def _iptables_effective(rules):
    """Meaningful host firewall = INPUT chain policy DROP/REJECT."""
    for line in (rules or '').splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == '-P' and parts[1] == 'INPUT':
            return parts[2].upper() in ('DROP', 'REJECT')
    return False

def _nft_effective(rules):
    low = (rules or '').lower()
    return 'hook input' in low and 'policy drop' in low

def firewall_detect():
    """Comprehensive firewall detection across distros.

    Returns {
      'backend': 'ufw'|'firewalld'|'nftables'|'iptables'|'windows'|'',
      'active': bool,
      'available': [已安装的管理器],
      'family': os_family(),
      'preferred': 推荐管理器,
      'detail': {管理器: 'active'|'installed'|'missing'}
    }
    """
    family = os_family()
    preferred = preferred_firewall()
    info = {'backend': '', 'active': False, 'available': [], 'family': family,
            'preferred': preferred, 'detail': {}}
    if TEST:
        info.update({'backend': 'ufw', 'active': True, 'available': ['ufw']})
        info['detail'] = {'ufw': 'active', 'simulated': 'test'}
        info['panels'] = panel_detect()
        info['managers'] = ['ufw']
        info['conflict'] = bool(info['panels'])
        return info
    if family == 'windows':
        out = _cmd_out(['netsh', 'advfirewall', 'show', 'allprofiles', 'state'])
        active = 'ON' in (out or '').upper()
        info['available'] = ['windows'] if shutil.which('netsh') else []
        info['detail'] = {'windows': 'active' if active else 'installed'}
        if active: info.update({'backend': 'windows', 'active': True})
        return info

    ufw_installed = bool(shutil.which('ufw'))
    fwd_installed = bool(shutil.which('firewall-cmd'))
    nft_installed = bool(shutil.which('nft'))
    ipt_installed = bool(shutil.which('iptables'))
    if ufw_installed: info['available'].append('ufw')
    if fwd_installed: info['available'].append('firewalld')
    if nft_installed: info['available'].append('nftables')
    if ipt_installed: info['available'].append('iptables')

    ufw_active = fwd_active = False
    if ufw_installed:
        ufw_active = 'status: active' in _cmd_out(['ufw', 'status']).lower()
        info['detail']['ufw'] = 'active' if ufw_active else 'installed'
    if fwd_installed:
        fwd_active = _cmd_out(['firewall-cmd', '--state']).strip().lower() == 'running'
        info['detail']['firewalld'] = 'active' if fwd_active else 'installed'
    if ipt_installed:
        info['detail']['iptables'] = 'active' if _iptables_effective(_cmd_out(['iptables', '-S'])) else 'installed'
    if nft_installed:
        info['detail']['nftables'] = 'active' if _nft_effective(_cmd_out(['nft', 'list', 'ruleset'])) else 'installed'

    # 优先选"真正在生效"的那一个；高级管理器优先（ufw 管理 iptables，firewalld 管理 nft）
    if ufw_active: info.update({'backend': 'ufw', 'active': True})
    elif fwd_active: info.update({'backend': 'firewalld', 'active': True})
    elif info['detail'].get('iptables') == 'active': info.update({'backend': 'iptables', 'active': True})
    elif info['detail'].get('nftables') == 'active': info.update({'backend': 'nftables', 'active': True})
    info['panels'] = panel_detect()
    info['managers'] = firewall_managers(info)
    # 面板 + 系统防火墙同时管理 → 可能互相覆盖
    info['conflict'] = bool(info['panels']) and bool(info['managers'])
    return info

# ---------------------------------------------------------------- 面板接管检测
# 宝塔/aaPanel/小皮/1Panel/cPanel/Plesk 都是"防火墙管理层"，它们直接操作
# iptables 或 firewalld。面板接管时若我们直接写系统防火墙，规则可能在面板
# 重载时被覆盖而静默失效，所以必须显性识别并默认切只读。

PANEL_ROOTS = (
    ('/www/server/panel', 'baota', '宝塔面板 / aaPanel'),
    ('/www/server/phpstudy', 'xiaopi', '小皮面板'),
    ('/xp/server', 'xiaopi', '小皮面板'),
    ('/opt/1panel', '1panel', '1Panel'),
    ('/usr/local/cpanel', 'cpanel', 'cPanel'),
    ('/usr/local/psa', 'plesk', 'Plesk'),
)

def panel_detect():
    """Detect panel software that manages the firewall. Read-only probes."""
    if TEST: return []
    panels = []
    try:
        roots = list(PANEL_ROOTS)
        if os.name == 'nt':
            roots = [((os.environ.get('ProgramFiles', 'C:/Program Files') + '/btpanel'),
                      'baota', '宝塔面板（Windows）')]
        seen = set()
        for path, key, name in roots:
            try:
                exists = Path(path).exists()
            except OSError:
                exists = False
            if exists and key not in seen:
                seen.add(key)
                panels.append({'key': key, 'name': name, 'path': path})
        if os.name != 'nt' and shutil.which('bt') and 'baota' not in seen:
            panels.append({'key': 'baota', 'name': '宝塔面板 / aaPanel', 'path': 'bt'})
    except (OSError, ValueError):
        pass
    return panels


def firewall_managers(info=None):
    """Names of firewall layers that are actually active."""
    info = info or firewall_detect()
    detail = info.get('detail') or {}
    return [name for name in ('ufw', 'firewalld', 'iptables', 'nftables', 'windows')
            if detail.get(name) == 'active']

def firewall_mode(info=None):
    """Decide operating mode: 'readonly' | 'panel-api' | 'direct'.

    auto（默认）：
      面板 API 已配置            → panel-api（写进面板数据库，重载不丢）
      检测到面板但未配置 API      → readonly（只生成清单）
      无面板                     → direct（直写系统防火墙）
    可用 auto-port.toml 的 panel_mode 覆盖为 readonly / panel-api / direct。
    """
    cfg = auto_port_config()
    requested = str(cfg.get('panel_mode', 'auto')).lower()
    if requested in ('direct', 'readonly', 'panel-api'): return requested
    info = info or firewall_detect()
    if panel_api_ready():
        return 'panel-api'
    return 'readonly' if (info.get('panels') or []) else 'direct'




def firewall_open_ports():
    """Set of (port, proto) currently allowed by the active backend (one query)."""
    info = firewall_detect()
    backend, active = info['backend'], info['active']
    opened = set()
    if not active: return opened
    try:
        if backend == 'ufw':
            for line in _cmd_out(['ufw', 'status']).splitlines():
                match = re.match(r'\s*(\d+)/(tcp|udp)\s+ALLOW', line, re.I)
                if match: opened.add((int(match.group(1)), match.group(2).lower()))
        elif backend == 'firewalld':
            for token in _cmd_out(['firewall-cmd', '--list-ports']).split():
                if '/' in token:
                    port, proto = token.split('/', 1)
                    if port.isdigit(): opened.add((int(port), proto.lower()))
        elif backend == 'iptables':
            for line in _cmd_out(['iptables', '-S', 'INPUT']).splitlines():
                match = re.search(r'-p (tcp|udp).*--dport (\d+).*-j ACCEPT', line)
                if match: opened.add((int(match.group(2)), match.group(1).lower()))
        elif backend == 'nftables':
            text = _cmd_out(['nft', 'list', 'chain', 'inet', 'filter', 'input'])
            for match in re.finditer(r'(tcp|udp) dport (\d+)\s+accept', text, re.I):
                opened.add((int(match.group(2)), match.group(1).lower()))
        elif backend == 'windows':
            for rec in auto_port_state().get('opened') or []:
                try:
                    if firewall_port_open(int(rec['port']), str(rec['proto'])):
                        opened.add((int(rec['port']), str(rec['proto'])))
                except (KeyError, TypeError, ValueError):
                    continue
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return opened
# ---------------------------------------------------------------- 宝塔面板 API
# 宝塔/aaPanel 的防火墙是 iptables 的管理界面。直接写 iptables 会在面板重载时
# 被覆盖；通过面板 API 写入规则则进入面板数据库，重载后依然存在。
#
# 鉴权：request_token = md5(request_time + md5(api_key))
# 接口：POST {url}/firewall?action=AddRule  (参数 port/ps/types)
# 注意：不同宝塔版本参数名可能略有差异，因此这里全部做成可配置项，
#      并提供 `probe` 命令把原始返回打出来，便于现场校准。

PANEL_TYPES = ('baota', 'aapanel')

def _safe_int(value, default):
    """int() 的容错版本：配置被手改成非数字时不应让菜单崩溃。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def panel_config():
    """Read the [panel] section from auto-port.toml."""
    raw = read_toml(auto_port_config_path()) if auto_port_config_path().exists() else {}
    section = {}
    if isinstance(raw, dict):
        section = raw.get('panel') or {}
        if not isinstance(section, dict): section = {}
    cfg = auto_port_config()
    from_env = os.environ.get('FRP_PANEL_API_KEY', '')
    file_key = str(section.get('api_key', '') or '')
    return {
        'type': str(section.get('type', 'baota') or 'baota').lower(),
        'url': str(section.get('url', 'http://127.0.0.1:8888') or '').rstrip('/'),
        'api_key': file_key or from_env,
        # 密钥来自环境变量时标记来源，保存配置时不要把它物化到磁盘
        'api_key_from_env': bool(from_env and not file_key),
        'verify_tls': bool(section.get('verify_tls', True)),
        'timeout': _safe_int(section.get('timeout', 10), 10),
        'node': str(section.get('node', '') or ''),          # aaPanel 多节点时可填
        'list_action': str(section.get('list_action', 'GetRules')),
        'add_action': str(section.get('add_action', 'AddRule')),
        'del_action': str(section.get('del_action', 'DelRule')),
        'mode': str(cfg.get('panel_mode', 'auto')).lower(),
    }

def save_panel_config(panel):
    """Persist the [panel] section without touching other keys."""
    raw = read_toml(auto_port_config_path()) if auto_port_config_path().exists() else {}
    if not isinstance(raw, dict): raw = {}
    api_key = '' if panel.get('api_key_from_env') else str(panel.get('api_key', ''))
    raw['panel'] = {
        'type': str(panel.get('type', 'baota')),
        'url': str(panel.get('url', '')),
        'api_key': api_key,
        'verify_tls': bool(panel.get('verify_tls', True)),
        'timeout': int(panel.get('timeout', 10)),
        'list_action': str(panel.get('list_action', 'GetRules')),
        'add_action': str(panel.get('add_action', 'AddRule')),
        'del_action': str(panel.get('del_action', 'DelRule')),
    }
    if panel.get('node'): raw['panel']['node'] = str(panel['node'])
    raw.setdefault('schema_version', 1)
    raw.setdefault('panel_mode', 'auto')
    raw.setdefault('allow_clients', [])
    raw.setdefault('max_ports_per_client', 20)
    raw.setdefault('deny_ports_enabled', True)
    raw.setdefault('deny_ports', list(AUTO_PORT_DENY_DEFAULT))
    raw.setdefault('sync_interval_seconds', 15)
    raw.setdefault('prune_after_seconds', 120)
    atomic(auto_port_config_path(), toml(raw))
    try: os.chmod(str(auto_port_config_path()), 0o600)
    except OSError: pass
    audit('panel-config-saved')

def panel_api_ready(panel=None):
    """True when the panel API is configured enough to be used."""
    panel = panel or panel_config()
    return bool(panel['type'] in PANEL_TYPES and panel['url'] and panel['api_key'])

def _panel_sign(api_key):
    """Baota signature: md5(request_time + md5(api_key))."""
    stamp = str(int(time.time()))
    inner = hashlib.md5(api_key.encode()).hexdigest()
    token = hashlib.md5((stamp + inner).encode()).hexdigest()
    return stamp, token

def panel_request(action, params=None, panel=None, timeout=None):
    """Signed POST to the panel firewall endpoint. Returns parsed JSON dict."""
    panel = panel or panel_config()
    if not panel['url']: raise ValueError('面板 API 地址未配置（panel.url）')
    if not panel['api_key']: raise ValueError('面板 API 密钥未配置（panel.api_key 或 FRP_PANEL_API_KEY）')
    if not panel['url'].lower().startswith(('http://', 'https://')):
        raise ValueError('面板 API 地址必须以 http:// 或 https:// 开头')
    stamp, token = _panel_sign(panel['api_key'])
    body = {'request_time': stamp, 'request_token': token}
    if panel.get('node'): body['node'] = panel['node']
    for key, value in (params or {}).items():
        if value is not None: body[key] = str(value)
    data = urllib.parse.urlencode(body).encode()
    url = '%s/firewall?action=%s' % (panel['url'], urllib.parse.quote(action))
    request = urllib.request.Request(url, data=data, method='POST')
    context = None
    if url.lower().startswith('https://') and not panel['verify_tls']:
        # 关闭校验意味着可被中间人伪造响应；每次调用都记审计便于事后追溯
        ports_audit('panel-tls-insecure', result='warn', detail='面板 HTTPS 校验已关闭')
        import ssl
        context = ssl._create_unverified_context()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if context is not None:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),
                                             urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout or panel['timeout']) as response:
            raw = response.read().decode('utf-8', errors='replace')
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError('面板 API 请求失败：' + str(exc))
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('面板 API 返回非 JSON：' + raw[:200])
    if not isinstance(payload, dict):
        raise ValueError('面板 API 返回格式异常：' + raw[:200])
    return payload

def panel_rules(panel=None):
    """List firewall rules through the panel API."""
    panel = panel or panel_config()
    payload = panel_request(panel['list_action'], {}, panel)
    # 宝塔常见返回：{"data": [...]} 或 {"data": {"data": [...]}}
    rows = payload.get('data')
    if isinstance(rows, dict): rows = rows.get('data') or rows.get('list')
    if not isinstance(rows, list): return []
    result = []
    for row in rows:
        if not isinstance(row, dict): continue
        result.append(row)
    return result

PANEL_RULE_MARK = 'frp-simple-bash-auto'

def _panel_find_rule(port, proto, panel=None, ours_only=True):
    """Find the panel rule id for a port/proto pair.

    ours_only=True 时只匹配本工具创建的规则（ps 带 PANEL_RULE_MARK），
    避免误删用户手工放的 SSH/面板等规则。
    """
    for row in panel_rules(panel):
        try:
            row_port = int(str(row.get('port', '')).split(',')[0].split('-')[0])
        except (TypeError, ValueError):
            continue
        row_types = str(row.get('types', 'tcp')).lower()
        if row_port != int(port): continue
        if not (proto in row_types or row_types in ('', ',')): continue
        if ours_only and PANEL_RULE_MARK not in str(row.get('ps', '')):
            continue
        return row.get('id')
    return None

def _panel_status_ok(payload):
    """面板返回是否成功。缺 status 字段一律视为失败（避免静默假成功）。"""
    status = str(payload.get('status', '')).lower()
    if status in ('true', '1', 'ok'): return True
    if status == '': return False
    return False

def panel_add_rule(port, proto, panel=None):
    """Add an inbound allow rule through the panel. Idempotent.

    已存在本工具的规则时直接返回成功，不重复 AddRule
    （重复添加会让面板报"已存在"，进而被当成失败）。
    """
    panel = panel or panel_config()
    if _panel_find_rule(port, proto, panel) is not None:
        return {'status': True, 'msg': 'already exists'}
    payload = panel_request(panel['add_action'],
                            {'port': int(port), 'types': proto,
                             'ps': PANEL_RULE_MARK, 'address': ''}, panel)
    if not _panel_status_ok(payload):
        raise ValueError('面板添加规则失败：' + str(payload.get('msg') or payload)[:200])
    ports_audit('panel-port-open', port=port, proto=proto, result='ok')
    return payload

def panel_del_rule(port, proto, panel=None):
    """Remove OUR panel rule for a port/proto pair.

    只删 ps 标记为 frp-simple-bash-auto 的规则；用户自己加的规则不碰。
    返回 True=已删除 / False=没有本工具的规则可删。
    """
    panel = panel or panel_config()
    rule_id = _panel_find_rule(port, proto, panel)
    if rule_id is None: return False
    payload = panel_request(panel['del_action'], {'id': rule_id}, panel)
    if not _panel_status_ok(payload):
        raise ValueError('面板删除规则失败：' + str(payload.get('msg') or payload)[:200])
    ports_audit('panel-port-close', port=port, proto=proto, result='ok')
    return True

def panel_probe(panel=None):
    """Connectivity + credential check; returns {'ok', 'rules', 'raw'} or raises."""
    panel = panel or panel_config()
    payload = panel_request(panel['list_action'], {}, panel)
    rows = payload.get('data')
    if isinstance(rows, dict): rows = rows.get('data') or rows.get('list')
    count = len(rows) if isinstance(rows, list) else 0
    return {'ok': True, 'rules': count, 'raw': payload}
def firewall_backend():
    """Compatibility wrapper: (backend, active)."""
    info = firewall_detect()
    return (info['backend'], info['active'])

def firewall_port_open(port, proto):
    """True when the active backend already allows that port."""
    info = firewall_detect()
    backend, active = info['backend'], info['active']
    if not active: return False
    needle = '%d/%s' % (port, proto)
    try:
        if backend == 'ufw':
            for line in _cmd_out(['ufw', 'status']).splitlines():
                if line.strip().startswith(needle) and 'ALLOW' in line.upper():
                    return True
            return False
        if backend == 'firewalld':
            return _cmd_ok(['firewall-cmd', '--query-port=' + needle])
        if backend == 'windows':
            return _cmd_ok(['netsh', 'advfirewall', 'firewall', 'show', 'rule',
                            'name=' + _win_rule_name(port, proto)])
        if backend == 'iptables':
            return _cmd_ok(['iptables', '-C', 'INPUT', '-p', proto, '--dport', str(port), '-j', 'ACCEPT'])
        if backend == 'nftables':
            out = _cmd_out(['nft', 'list', 'chain', 'inet', 'filter', 'input'])
            # 逐条规则匹配：必须同一条规则里既有该 dport 又 verdict 为 accept
            pattern = re.compile(r'%s\s+dport\s+%d\s+accept' % (proto, port), re.I)
            return bool(pattern.search(out))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return False

def _win_rule_name(port, proto):
    return 'frp-simple-bash-auto-%s-%d' % (proto, port)

def _persist_iptables():
    """Persist iptables rules so they survive a reboot.

    规则本身是临时的：不落盘的话重启即失效，而 state 里仍记着"已放行"，
    用户会看到"界面说放行了但端口不通"。这里按发行版尝试多个落盘方式，
    全都失败时返回 False，由调用方告警。
    """
    try:
        out = run_cmd(['iptables-save'], capture_output=True, text=True, timeout=20)
        if out.returncode != 0: return False
        payload = out.stdout
        # 1) Debian/Ubuntu: iptables-persistent
        target = Path('/etc/iptables/rules.v4')
        if target.parent.exists():
            try:
                atomic(target, payload); return True
            except (OSError, ValueError): pass
        # 2) RHEL/CentOS: iptables-services
        target = Path('/etc/sysconfig/iptables')
        if target.parent.exists():
            try:
                atomic(target, payload); return True
            except (OSError, ValueError): pass
        # 3) 通用：netfilter-persistent
        if shutil.which('netfilter-persistent'):
            proc = run_cmd(['netfilter-persistent', 'save'],
                           capture_output=True, text=True, timeout=30)
            if proc.returncode == 0: return True
        # 4) 通用：service iptables save
        if shutil.which('service'):
            proc = run_cmd(['service', 'iptables', 'save'],
                           capture_output=True, text=True, timeout=30)
            if proc.returncode == 0: return True
        return False
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

def firewall_add_port(port, proto):
    """Allow one port in the detected backend. Raises ValueError on failure."""
    info = firewall_detect()
    backend, active = info['backend'], info['active']
    if not backend:
        raise ValueError('未检测到防火墙（' + _firewall_hint_text(info) + '）')
    if not active:
        raise ValueError('系统防火墙未启用（检测到 %s 但未运行）。' % backend
                         + '请先执行 `frps ports firewall enable` 或手动启用后再同步。')
    needle = '%d/%s' % (port, proto)
    try:
        if backend == 'ufw':
            out = run_cmd(['ufw', 'allow', needle], capture_output=True, text=True, timeout=20)
        elif backend == 'firewalld':
            out = run_cmd(['firewall-cmd', '--permanent', '--add-port=' + needle],
                          capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                run_cmd(['firewall-cmd', '--reload'], capture_output=True, text=True, timeout=30)
        elif backend == 'windows':
            protocol = 'TCP' if proto == 'tcp' else 'UDP'
            out = run_cmd(['netsh', 'advfirewall', 'firewall', 'add', 'rule',
                           'name=' + _win_rule_name(port, proto), 'dir=in', 'action=allow',
                           'protocol=' + protocol, 'localport=' + str(port)],
                          capture_output=True, text=True, timeout=20)
        elif backend == 'iptables':
            out = run_cmd(['iptables', '-I', 'INPUT', '-p', proto, '--dport', str(port),
                           '-j', 'ACCEPT', '-m', 'comment', '--comment', 'frp-simple-bash:auto'],
                          capture_output=True, text=True, timeout=20)
            if out.returncode == 0 and not _persist_iptables():
                ports_audit('persist-failed', port=port, proto=proto, result='warn',
                            detail='iptables 规则未能持久化，重启后失效')
        else:  # nftables
            if not _cmd_ok(['nft', 'list', 'table', 'inet', 'filter']):
                raise ValueError('当前系统使用 nftables，但未找到标准 inet filter 表；'
                                 '请手动放行 ' + needle + '，或安装 ufw/firewalld。')
            out = run_cmd(['nft', 'add', 'rule', 'inet', 'filter', 'input',
                           proto, 'dport', str(port), 'accept',
                           'comment', '"frp-simple-bash:auto"'],
                          capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('防火墙放行失败：' + str(exc))
    if out.returncode != 0:
        raise ValueError('防火墙放行失败：' + (out.stderr or out.stdout or '').strip())

def firewall_del_port(port, proto):
    """Remove one port rule added by this tool. Returns True/False (real result)."""
    info = firewall_detect()
    backend, active = info['backend'], info['active']
    if not backend or not active: return False
    needle = '%d/%s' % (port, proto)
    try:
        if backend == 'ufw':
            out = run_cmd(['ufw', 'delete', 'allow', needle],
                          capture_output=True, text=True, timeout=20)
            return out.returncode == 0
        if backend == 'firewalld':
            out = run_cmd(['firewall-cmd', '--permanent', '--remove-port=' + needle],
                          capture_output=True, text=True, timeout=20)
            if out.returncode != 0: return False
            run_cmd(['firewall-cmd', '--reload'], capture_output=True, text=True, timeout=30)
            return True
        if backend == 'windows':
            out = run_cmd(['netsh', 'advfirewall', 'firewall', 'delete', 'rule',
                           'name=' + _win_rule_name(port, proto)],
                          capture_output=True, text=True, timeout=20)
            return out.returncode == 0
        if backend == 'iptables':
            out = run_cmd(['iptables', '-D', 'INPUT', '-p', proto, '--dport', str(port),
                           '-j', 'ACCEPT', '-m', 'comment', '--comment', 'frp-simple-bash:auto'],
                          capture_output=True, text=True, timeout=20)
            if out.returncode != 0: return False
            _persist_iptables()
            return True
        # 添加时带了 comment，删除必须用同样表达式，否则永远删不掉
        out = run_cmd(['nft', 'delete', 'rule', 'inet', 'filter', 'input',
                       proto, 'dport', str(port), 'accept',
                       'comment', '"frp-simple-bash:auto"'],
                      capture_output=True, text=True, timeout=20)
        return out.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

# ---------------------------------------------------------------- 防火墙启用引导

def ssh_listen_ports(strict=True):
    """Ports sshd actually listens on. strict=True refuses to guess.

    误判 SSH 端口会在启用防火墙时把用户锁在服务器外面，所以这里要么拿到
    权威结果（监听套接字 / sshd_config），要么直接拒绝自动启用。
    """
    ports = set()
    detected = False
    for line in _cmd_out(['ss', '-lntp']).splitlines():
        if 'sshd' in line:
            for match in re.finditer(r':(\d+)\b', line):
                try: ports.add(int(match.group(1)))
                except ValueError: pass
            detected = True
    if not detected:
        # 1) sshd -T 是最权威的（已合并 Include 与默认值）
        try:
            out = _cmd_out(['sshd', '-T'])
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == 'port' and parts[1].isdigit():
                    ports.add(int(parts[1])); detected = True
        except (OSError, ValueError):
            pass
    if not detected:
        # 2) 退回解析 sshd_config 及其 sshd_config.d/*.conf
        candidates = [Path('/etc/ssh/sshd_config')]
        drop_in = Path('/etc/ssh/sshd_config.d')
        if drop_in.is_dir():
            candidates.extend(sorted(drop_in.glob('*.conf')))
        for candidate in candidates:
            try:
                text = candidate.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith('#'): continue
                if stripped.lower().startswith('port '):
                    value = stripped.split()[1]
                    if value.isdigit():
                        ports.add(int(value)); detected = True
    if not detected:
        # 3) Windows: 从 OpenSSH 配置读
        try:
            base = os.environ.get('ProgramData', r'C:\ProgramData')
            cfg = Path(base) / 'ssh' / 'sshd_config'
            if cfg.exists():
                for line in cfg.read_text(encoding='utf-8', errors='replace').splitlines():
                    if line.strip().lower().startswith('port '):
                        value = line.split()[1]
                        if value.isdigit(): ports.add(int(value)); detected = True
        except OSError:
            pass
    if not ports:
        if strict:
            raise ValueError('无法确定 SSH 监听端口，拒绝自动启用防火墙（可能把你锁在服务器外）。'
                             '请手动放行 SSH 端口后再启用。')
        ports.add(22)
    return sorted(ports)

def frps_keep_ports(include_opened=True):
    """Ports frps itself needs before/after enabling a firewall.

    include_opened=False 时只返回 frps 自身端口（不含本工具已放行的映射端口）——
    撤销操作的保护名单要用这个，否则"保护"会把所有待撤销端口也保护起来。
    """
    ports = []
    try:
        data = read_toml(ROOT / 'frps.toml') if (ROOT / 'frps.toml').exists() else {}
    except (ValueError, OSError):
        data = {}
    def add(key, proto='tcp'):
        value = data.get(key)
        if isinstance(value, int) and 0 < value < 65536:
            ports.append((value, proto))
    add('bindPort')
    for key in ('vhostHTTPPort', 'vhostHTTPSPort', 'tcpmuxHTTPConnectPort', 'kcpBindPort', 'quicBindPort'):
        add(key)
    web = data.get('webServer') or {}
    if web.get('port') and str(web.get('addr', '127.0.0.1')) not in ('127.0.0.1', '::1'):
        try: ports.append((int(web['port']), 'tcp'))
        except (TypeError, ValueError): pass
    # 已由本工具放行的映射端口也要保留（仅启用防火墙场景需要）
    if include_opened:
        for rec in auto_port_state().get('opened') or []:
            try: ports.append((int(rec['port']), str(rec.get('proto', 'tcp'))))
            except (KeyError, TypeError, ValueError): continue
    seen, unique = set(), []
    for item in ports:
        if item in seen: continue
        seen.add(item); unique.append(item)
    return unique

def _install_command(manager):
    """Distro-appropriate install command for a firewall manager."""
    family = os_family()
    package = {'ufw': 'ufw', 'firewalld': 'firewalld', 'nftables': 'nftables'}.get(manager, manager)
    if manager == 'windows': return []
    if family == 'debian': return ['apt-get', 'install', '-y', package]
    if family == 'rhel': return ['dnf', 'install', '-y', package]
    if family == 'suse': return ['zypper', '--non-interactive', 'install', package]
    if family == 'alpine': return ['apk', 'add', package]
    if family == 'arch': return ['pacman', '-S', '--noconfirm', package]
    return []

def firewall_enable_plan(ssh_port=None):
    """Build a SAFE enable plan: SSH first, then frps ports, then activate."""
    info = firewall_detect()
    family = info['family']
    target = info['preferred']
    installed = target in info['available']
    if not installed and target not in ('ufw', 'firewalld', 'nftables'):
        raise ValueError('当前系统（%s）没有可自动启用的防火墙管理器，请手动配置。' % family)
    if target == 'nftables':
        raise ValueError(
            '当前系统推荐 nftables，但它没有"一键启用"的概念，规则还需写入 '
            '/etc/nftables.conf 才能持久化。\n'
            '为避免"只加了规则却不生效"，本工具拒绝自动启用 nftables。\n'
            '请任选其一：\n'
            '  1) 安装并启用 ufw 或 firewalld（推荐，之后本工具可自动管理）；\n'
            '  2) 手动在 /etc/nftables.conf 中放行所需端口。')
    if ssh_port:
        ssh_ports = [int(ssh_port)]
    else:
        ssh_ports = ssh_listen_ports(strict=True)
    keep = frps_keep_ports()
    install_cmds, allow_cmds, notes = [], [], []
    if not installed:
        cmd = _install_command(target)
        if not cmd:
            raise ValueError('无法为 %s 推断包管理器，请手动安装 %s。' % (family, target))
        install_cmds.append(cmd)
    for port in ssh_ports:
        if target == 'ufw':
            allow_cmds.append(['ufw', 'allow', '%d/tcp' % port])
        elif target == 'firewalld':
            allow_cmds.append(['firewall-cmd', '--permanent', '--add-port=%d/tcp' % port])
        elif target == 'nftables':
            allow_cmds.append(['nft', 'add', 'rule', 'inet', 'filter', 'input',
                               'tcp', 'dport', str(port), 'accept'])
    for port, proto in keep:
        if target == 'ufw':
            allow_cmds.append(['ufw', 'allow', '%d/%s' % (port, proto)])
        elif target == 'firewalld':
            allow_cmds.append(['firewall-cmd', '--permanent', '--add-port=%d/%s' % (port, proto)])
        elif target == 'nftables':
            allow_cmds.append(['nft', 'add', 'rule', 'inet', 'filter', 'input',
                               proto, 'dport', str(port), 'accept'])
    if target == 'ufw':
        enable_cmds = [['ufw', '--force', 'enable']]
    elif target == 'firewalld':
        enable_cmds = [['systemctl', 'enable', '--now', 'firewalld']]
    elif target == 'nftables':
        enable_cmds = []
        notes.append('nftables 没有"启用"开关：需要先把上面的规则写入 /etc/nftables.conf，'
                     '再执行 systemctl enable --now nftables。')
    else:
        enable_cmds = []
    notes.append('SSH 端口：' + ', '.join(str(p) for p in ssh_ports) + '（会最先放行，避免自锁）')
    return {'target': target, 'family': family, 'installed': installed,
            'ssh_ports': ssh_ports, 'keep': keep,
            'install_cmds': install_cmds, 'allow_cmds': allow_cmds,
            'enable_cmds': enable_cmds, 'notes': notes}

def firewall_snapshot():
    """Save current firewall state so enabling can be rolled back."""
    snap = {'time': dt.datetime.now().isoformat(timespec='seconds')}
    try:
        # iptables 快照（ufw/firewalld 底层都是 iptables 或 nft）
        out = run_cmd(['iptables-save'], capture_output=True, text=True, timeout=30)
        if out.returncode == 0: snap['iptables'] = out.stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        if shutil.which('nft'):
            out = run_cmd(['nft', 'list', 'ruleset'], capture_output=True, text=True, timeout=30)
            if out.returncode == 0: snap['nft'] = out.stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        if shutil.which('ufw'):
            out = run_cmd(['ufw', 'status'], capture_output=True, text=True, timeout=20)
            snap['ufw_status'] = (out.stdout or '').strip()
            snap['ufw_active'] = 'status: active' in (out.stdout or '').lower()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        data = read_toml(ROOT / 'frps.toml') if (ROOT / 'frps.toml').exists() else {}
        snap['bind_port'] = data.get('bindPort')
    except (ValueError, OSError):
        pass
    return snap

def firewall_snapshot_save(snap):
    """Persist a snapshot next to auto-port state (0600)."""
    path = ROOT / 'firewall-snapshot.json'
    save_json(path, snap)
    try: os.chmod(str(path), 0o600)
    except OSError: pass
    return path

def firewall_snapshot_load():
    path = ROOT / 'firewall-snapshot.json'
    if not path.exists(): return {}
    try:
        return read_json(path, {})
    except (ValueError, OSError):
        return {}

def firewall_revert(snap=None, yes=False):
    """Roll the firewall back to the saved snapshot.

    用于「启用防火墙后把自己或服务锁在外面」的急救场景。
    """
    snap = snap or firewall_snapshot_load()
    if not snap:
        raise ValueError('没有可用的防火墙快照；请手动检查 ufw / iptables 状态。')
    if not yes and not confirm(
            '将把防火墙回滚到 ' + str(snap.get('time', '未知时间')) + ' 的快照状态。\n'
            '这会覆盖当前的防火墙规则。确认继续？', danger=True):
        return {'ok': False, 'reason': 'cancelled'}
    actions = []
    # 1) 如果快照里 ufw 原本是关闭的，就关掉它（最常见的"锁死"根因）
    if snap.get('ufw_active') is False:
        try:
            run_cmd(['ufw', 'disable'], capture_output=True, text=True, timeout=60)
            actions.append('ufw disable')
        except (OSError, subprocess.SubprocessError):
            pass
    # 2) 恢复 iptables 规则
    if snap.get('iptables'):
        try:
            proc = subprocess.run(['iptables-restore'], input=snap['iptables'],
                                  capture_output=True, text=True, timeout=60)
            actions.append('iptables-restore' + ('' if proc.returncode == 0
                                                 else '（失败：%s）' % (proc.stderr or '').strip()))
        except (OSError, subprocess.SubprocessError) as exc:
            actions.append('iptables-restore 失败：' + str(exc))
    ports_audit('firewall-revert', result='ok', detail='; '.join(actions))
    return {'ok': True, 'actions': actions}

def firewall_enable(yes=False, force=False, ssh_port=None):
    """Install (if needed) and enable a firewall, SSH-safe and reversible.

    Order is strictly: 快照 → 放行 SSH → 放行 frps 端口 → 启用 → 复查。
    面板接管时默认**拒绝执行**（只给出说明），因为启用系统防火墙会覆盖面板
    已经下发的 iptables 规则，导致既有服务失联。确需执行时必须 force。
    """
    plan = firewall_enable_plan(ssh_port=ssh_port)
    info0 = firewall_detect()
    # 面板接管：硬阻止（不再是"警告后仍可继续"）
    if info0.get('panels') and not force:
        names = '、'.join(p.get('name', '') for p in info0['panels'])
        raise ValueError(
            '检测到面板正在管理防火墙（%s），已拒绝启用系统防火墙。\n'
            '原因：启用 ufw/firewalld 会重建规则链，覆盖面板已放行的端口，'
            '可能导致现有服务（含 frps 7000、SSH）立即失联。\n'
            '建议做法：在面板「安全 → 防火墙」中手工放行所需端口，'
            '或改用「端口自动开通 → 4) 宝塔 API」让本工具通过面板 API 放行。\n'
            '若你确实要接管系统防火墙（风险自负），请用：'
            'frps ports firewall enable --force' % names)

    keep_desc = ', '.join('%d/%s' % (p, t) for p, t in plan['keep']) or '无'
    if not yes and not confirm(
            '将启用防火墙：' + plan['target'] + '\n'
            '会先保存当前防火墙快照，再放行 SSH 端口 '
            + ', '.join(str(p) for p in plan['ssh_ports'])
            + '，然后放行 frps 端口（' + keep_desc + '），最后启用。\n'
            '若启用后失联，可用 `frps ports firewall revert` 回滚。确认继续？', danger=True):
        return {'ok': False, 'reason': 'cancelled', 'plan': plan}

    # 0) 快照（回滚依据）
    snap = firewall_snapshot()
    snap_path = firewall_snapshot_save(snap)

    for cmd in plan['install_cmds']:
        out = run_cmd(cmd, capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise ValueError('安装失败：' + ' '.join(cmd) + ' — ' + (out.stderr or '').strip())
    ssh_allowed = 0
    for cmd in plan['allow_cmds']:
        is_ssh = any((':%d' % port) in ' '.join(cmd) or ('%d/' % port) in ' '.join(cmd)
                     for port in plan['ssh_ports'])
        out = run_cmd(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0 and is_ssh:
            raise ValueError('放行 SSH 端口失败，已中止启用（未启用防火墙，安全）：'
                             + (out.stderr or '').strip())
        if is_ssh: ssh_allowed += 1
    if ssh_allowed == 0:
        raise ValueError('未能放行任何 SSH 端口，已中止启用。')
    for cmd in plan['enable_cmds']:
        out = run_cmd(cmd, capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise ValueError('启用失败：' + ' '.join(cmd) + ' — ' + (out.stderr or '').strip())
    info = firewall_detect()
    # 启用后自检：frps 业务端口如果不在放行集合里，外部客户端会立刻失联，
    # 这是"启用防火墙把自己服务锁在外面"最常见的情形，必须当场告警。
    missing = []
    try:
        live = firewall_open_ports()
        for port, proto in plan['keep']:
            if live and (port, proto) not in live: missing.append('%d/%s' % (port, proto))
    except (OSError, ValueError):
        pass
    ports_audit('firewall-enable', result='ok' if info['active'] else 'inactive',
                detail='%s family=%s ssh=%s snapshot=%s missing=%s'
                       % (plan['target'], plan['family'],
                          ','.join(str(p) for p in plan['ssh_ports']),
                          Path(str(snap_path)).name, ','.join(missing) or 'none'))
    return {'ok': info['active'], 'reason': '' if info['active'] else 'not-active',
            'plan': plan, 'snapshot': str(snap_path), 'missing': missing}

def _firewall_hint_text(info):
    """One-line guidance describing what to do about the missing firewall."""
    family = info.get('family', 'other')
    preferred = info.get('preferred', '')
    available = info.get('available') or []
    if available:
        return '已安装 %s 但均未启用，可执行 `frps ports firewall enable`' % '/'.join(available)
    if family == 'windows':
        return 'Windows 防火墙未启用，可用 netsh advfirewall set allprofiles state on'
    return ('当前发行版（%s）未安装防火墙；建议安装 %s 后执行 `frps ports firewall enable`'
            % (family, preferred or 'ufw'))

# ---------------------------------------------------------------- 端口占用检查

def port_listener_pids(port, proto='tcp'):
    """Best-effort PIDs listening on a TCP/UDP port."""
    pids = []
    try:
        if shutil.which('ss'):
            args = ['ss', '-lntpH' if proto == 'tcp' else '-lnupH', 'sport = :%d' % port]
            out = run_cmd(args, capture_output=True, text=True, timeout=10)
            for match in re.finditer(r'pid=(\d+)', out.stdout or ''):
                pids.append(int(match.group(1)))
        elif sys.platform == 'darwin' and shutil.which('lsof'):
            out = run_cmd(['lsof', '-nP', '-i' + proto.upper(), '-s' + proto.upper() + ':LISTEN'],
                          capture_output=True, text=True, timeout=15)
            for line in (out.stdout or '').splitlines():
                parts = line.split()
                if len(parts) > 8 and (':' + str(port)) in parts[8]:
                    if parts[1].isdigit(): pids.append(int(parts[1]))
        elif os.name == 'nt':
            # Windows: netstat -ano 输出 "  TCP  0.0.0.0:6000  0.0.0.0:0  LISTENING  1234"
            out = run_cmd(['netstat', '-ano'], capture_output=True, text=True, timeout=20)
            needle = ':%d' % port
            for line in (out.stdout or '').splitlines():
                parts = line.split()
                if len(parts) < 5: continue
                if 'LISTEN' not in parts[3].upper(): continue
                if not parts[1].endswith(needle): continue
                if proto == 'udp' and parts[0].upper() != 'UDP': continue
                if proto == 'tcp' and parts[0].upper() != 'TCP': continue
                if parts[4].isdigit(): pids.append(int(parts[4]))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return sorted(set(pids))

def port_conflict(port, proto, frps_pid=None):
    """True when a process OTHER than frps holds the port.

    注意：frps 的 remotePort 由 **frps 自己监听并转发**，所以"端口有监听"
    是正常现象，不是冲突。这里只在能确定监听者既不是 frps、也不是
    本工具视角下的 frp 转发时才判冲突。
    """
    pids = port_listener_pids(port, proto)
    if not pids: return False
    if not frps_pid:
        # 拿不到 frps PID（容器/OpenRC/PID 变化）时无法区分"frps 转发"
        # 与"第三方占用"。而端口来自 frps Dashboard 的在线 proxy 列表，
        # 本身就证明是 frps 在监听——此时一律不判冲突，否则会导致
        # **所有映射都被跳过、一个端口都放不出去**。
        return False
    if all(pid == frps_pid for pid in pids): return False
    return True

# ---------------------------------------------------------------- 数据源

def frps_dashboard_config():
    """Return (scheme, host, web) for the local frps Dashboard/API."""
    path = ROOT / 'frps.toml'
    data = read_toml(path) if path.exists() else {}
    web = data.get('webServer') or {}
    if not web.get('port'):
        raise ValueError('frps Dashboard/API 未启用，无法读取实际映射；'
                         '请先在「修改服务端配置」中开启 Dashboard。')
    host = web.get('addr', '127.0.0.1')
    if host in ('0.0.0.0', '::'): host = '127.0.0.1' if host == '0.0.0.0' else '::1'
    return api_scheme(web), api_host(host), web

def frps_online_proxies():
    """Read all proxies from the local frps v2 Dashboard API."""
    scheme, host, web = frps_dashboard_config()
    rows, page = [], 1
    while True:
        url = '%s://%s:%s/api/v2/proxies?page=%d&pageSize=200' % (scheme, host, web['port'], page)
        data = api(url, web.get('user', ''), web.get('password', ''))
        items = data.get('items') or []
        rows.extend(items)
        try: total = int(data.get('total', 0))
        except (TypeError, ValueError): total = 0
        if not items or page * 200 >= total or page >= 50: break
        page += 1
    return rows

def ports_desired(cfg=None):
    """Compute { (port, proto): record } from actually-online proxies.

    Returns (desired, skipped) where skipped explains every excluded item.
    """
    cfg = cfg or auto_port_config()
    allow = set(cfg['allow_clients'])
    deny = set(cfg['deny_ports']) if cfg['deny_ports_enabled'] else set()
    quota = cfg['max_ports_per_client']
    desired, skipped, per_client = {}, [], {}
    for item in frps_online_proxies():
        status = item.get('status') or {}
        if str(status.get('phase', '')).lower() not in ('online', 'running'): continue
        spec = item.get('spec') or {}
        proto = str(spec.get('type', '')).lower()
        if proto not in ('tcp', 'udp'): continue
        port = spec.get('remotePort') or spec.get('remote_port')
        try: port = int(port)
        except (TypeError, ValueError): continue
        client_id = str(item.get('clientID') or item.get('clientId') or '')
        name = str(item.get('name', ''))
        base = {'port': port, 'proto': proto, 'client_id': client_id, 'proxy': name}
        # 白名单是可选的：留空表示"放行所有已通过 frps auth.token 认证的客户端"。
        # 能连上 frps 的客户端本来就已经过 token 验证，白名单只是额外的收窄手段，
        # 不该成为必填步骤（早期版本留空会导致一个端口都不放行）。
        if allow and (not client_id or client_id not in allow):
            skipped.append(dict(base, reason='client_not_allowed')); continue
        if port in deny:
            skipped.append(dict(base, reason='denied_port')); continue
        if quota and client_id and per_client.get(client_id, 0) >= quota:
            skipped.append(dict(base, reason='quota_exceeded')); continue
        # 不做端口占用检查：这些端口来自 frps Dashboard 的**在线** proxy 列表，
        # 说明 frps 已经在监听并转发它们——"有监听"是正常现象。
        # 早期版本在这里调 port_conflict()，只要拿不到准确的 frps PID
        # （容器/OpenRC/PID 变化/子进程监听）就会把全部映射判成"端口被占用"，
        # 表现为"配好了 API 但一个端口都放不出去"。
        key = (port, proto)
        if key in desired: continue
        per_client[client_id] = per_client.get(client_id, 0) + 1
        desired[key] = base
    return desired, skipped

# ---------------------------------------------------------------- 同步

def ports_sync(apply_changes=True, force=False):
    """同步一次：读实际在线映射 → 按模式放行/回收。

    模式由 firewall_mode() 决定（受 auto-port.toml 的 panel_mode 覆盖）：
      panel-api → 调宝塔 API（规则进面板数据库）
      manual（=readonly）→ 只列清单，不写任何地方
      direct    → 直写系统防火墙（裸机）

    状态语义：opened 只记录**真实放行成功**的端口；manual 模式绝不写入。
    回收有宽限期（prune_after_seconds），避免 frpc 抖动导致规则反复增删。
    """
    cfg = auto_port_config()
    panel = panel_config()
    mode = firewall_mode()
    if mode == 'readonly': mode = 'manual'
    report = {'enabled': cfg['enabled'], 'mode': mode, 'added': [], 'removed': [],
              'pending': [], 'skipped': [], 'opened': [], 'busy': False}
    if not cfg['enabled'] and not force:
        return report
    try:
        with lock():
            return _ports_sync_locked(cfg, panel, mode, apply_changes, report)
    except ValueError as exc:
        text = str(exc)
        # lock() 实际抛的是"另一个配置操作正在执行"；之前匹配 '锁'/'另一个终端'
        # 永远不命中，导致 busy 分支是死代码、锁冲突直接冒成错误
        if '另一个配置操作' in text or '锁' in text or '另一个终端' in text:
            report['busy'] = True
            return report
        raise

def _ports_sync_locked(cfg, panel, mode, apply_changes, report):
    desired, skipped = ports_desired(cfg)
    report['skipped'] = skipped
    st = auto_port_state()
    known = {}
    for rec in st.get('opened') or []:
        try: known[(int(rec['port']), str(rec['proto']))] = rec
        except (KeyError, TypeError, ValueError): continue
    report['opened'] = [{'port': k[0], 'proto': k[1], 'proxy': known[k].get('proxy', '')}
                        for k in sorted(known)]

    if mode == 'manual':
        report['pending'] = [{'port': k[0], 'proto': k[1], 'proxy': v.get('proxy', '')}
                             for k, v in sorted(desired.items()) if k not in known]
        return report

    opening = [k for k in sorted(desired) if k not in known]
    if not apply_changes:
        report['added'] = [{'port': k[0], 'proto': k[1]} for k in opening]
        return report

    opener = (lambda pr, pt: panel_add_rule(pr, pt, panel)) if mode == 'panel-api' \
        else (lambda pr, pt: firewall_add_port(pr, pt))
    closer = (lambda pr, pt: panel_del_rule(pr, pt, panel)) if mode == 'panel-api' \
        else (lambda pr, pt: firewall_del_port(pr, pt))
    opened = dict(known)
    now = dt.datetime.now()
    for key in opening:
        rec = desired[key]
        # 该端口本来就已被放行（用户手工加的）→ 绝不"认领"。
        # 否则后续回收会按端口删掉用户的规则（最坏删到 SSH 放行，重启后整机失联）。
        try:
            already = firewall_port_open(key[0], key[1])
        except (ValueError, OSError):
            already = False
        if already:
            ports_audit('port-open', port=key[0], proto=key[1], result='skip',
                        detail='端口此前已被放行，本工具不接管也不回收')
            report['skipped'].append(dict(rec, reason='already_open',
                                          detail='该端口此前已放行（非本工具添加），不接管'))
            continue
        try:
            opener(key[0], key[1])
            opened[key] = {'port': key[0], 'proto': key[1], 'client_id': rec['client_id'],
                           'proxy': rec['proxy'],
                           'opened_at': now.isoformat(timespec='seconds')}
            report['added'].append({'port': key[0], 'proto': key[1]})
        except (ValueError, OSError) as exc:
            ports_audit('port-open', port=key[0], proto=key[1], result='failed', detail=str(exc))
            report['skipped'].append(dict(rec, reason='apply_failed', detail=str(exc)))
    grace = max(10, int(cfg.get('prune_after_seconds', 120)))
    for key in sorted(list(opened)):
        if key in desired:
            opened[key].pop('missing_since', None)
            continue
        rec = opened[key]
        stamp = rec.get('missing_since')
        if not stamp:
            rec['missing_since'] = now.isoformat(timespec='seconds')
            continue
        try:
            since = dt.datetime.fromisoformat(stamp)
        except (ValueError, TypeError):
            since = now
        if (now - since).total_seconds() < grace:
            continue
        try:
            deleted = closer(key[0], key[1])
        except (ValueError, OSError) as exc:
            ports_audit('port-close', port=key[0], proto=key[1], result='failed',
                        detail=str(exc))
            rec['last_error'] = str(exc)[:120]
            continue
        if deleted is False:
            # 回收失败必须如实反映：不能清记录冒充"已回收"
            ports_audit('port-close', port=key[0], proto=key[1], result='failed',
                        detail='后端未确认删除')
            continue
        opened.pop(key, None)
        report['removed'].append({'port': key[0], 'proto': key[1]})
    st['opened'] = [opened[k] for k in sorted(opened)]
    save_auto_port_state(st)
    return report

def _protected_ports():
    """绝不能被本工具删除的端口：frps 自身端口 + SSH 监听端口。"""
    guarded = set(frps_keep_ports(include_opened=False))
    try:
        for p in ssh_listen_ports(strict=False):
            guarded.add((int(p), 'tcp'))
    except (ValueError, OSError):
        pass
    return guarded


def _state_record(rec):
    """校验并规整 state 里的一条记录；非法记录返回 None（绝不盲信状态文件）。"""
    try:
        port = int(rec.get('port'))
        proto = str(rec.get('proto')).lower()
    except (AttributeError, TypeError, ValueError):
        return None
    if not 1 <= port <= 65535: return None
    if proto not in ('tcp', 'udp'): return None
    return port, proto


def ports_revert():
    """撤销本工具放行的端口，并关闭自动同步。

    返回 {'removed': [...], 'failed': [...], 'manual': [...]}：
      removed 真的删掉了；failed 删除失败；manual 面板机器上没有 API，只能在面板手工删。

    安全约束：状态文件可能被篡改/从旧备份恢复，因此删除前逐条校验，
    并跳过 frps/SSH 等关键端口——宁可不删，也不能把用户锁在门外。
    """
    panel = panel_config()
    api_ready = panel_api_ready(panel)
    has_panel = bool(panel_detect())
    st = auto_port_state()
    result = {'removed': [], 'failed': [], 'manual': []}
    guarded = _protected_ports()
    for rec in st.get('opened') or []:
        parsed = _state_record(rec)
        if parsed is None:
            ports_audit('revert-skip', result='warn',
                        detail='状态文件里有非法记录，已跳过：%r' % (rec,))
            continue
        port, proto = parsed
        label = '%d/%s' % (port, proto)
        if (port, proto) in guarded:
            # 关键端口：即使状态文件说"是本工具加的"也不删
            ports_audit('revert-skip', port=port, proto=proto, result='warn',
                        detail='关键端口（frps/SSH）受保护，拒绝删除')
            result['manual'].append(label)
            continue
        if api_ready:
            try:
                (result['removed'] if panel_del_rule(port, proto, panel)
                 else result['manual']).append(label)
            except (ValueError, OSError):
                result['failed'].append(label)
        elif has_panel:
            result['manual'].append(label)      # 面板接管但没有 API：删不了，只能提示
        else:
            try:
                (result['removed'] if firewall_del_port(port, proto)
                 else result['failed']).append(label)
            except (ValueError, OSError):
                result['failed'].append(label)
    cfg = auto_port_config()
    if cfg.get('enabled'):
        cfg['enabled'] = False; save_auto_port_config(cfg)
    ports_timer_remove()
    # 只有真正删掉/可交给用户删的才从状态里移除；失败项保留以便下次重试
    keep = set(result['failed'])
    st['opened'] = [r for r in (st.get('opened') or [])
                    if '%s/%s' % (r.get('port'), r.get('proto')) in keep]
    save_auto_port_state(st)
    ports_audit('revert', result='ok' if not result['failed'] else 'partial',
                detail='removed=%d failed=%d manual=%d'
                       % (len(result['removed']), len(result['failed']), len(result['manual'])))
    return result

def _report_revert(outcome):
    """如实渲染撤销结果：删掉的、失败的、需要在面板手工删的分开说。"""
    removed = outcome.get('removed') or []
    failed = outcome.get('failed') or []
    manual = outcome.get('manual') or []
    lines = []
    if removed: lines.append('已删除 %d 个：%s' % (len(removed), _list_or_more(removed)))
    if manual: lines.append('需在面板手工删除 %d 个：%s' % (len(manual), _list_or_more(manual)))
    if failed: lines.append('删除失败 %d 个：%s' % (len(failed), _list_or_more(failed)))
    if lines: _report('撤销结果', lines)
    if failed:
        result_error('有 %d 个端口删除失败，已保留记录，可稍后重试。' % len(failed))
    elif manual:
        result_info('已关闭自动同步；有 %d 个端口需要在宝塔面板手工删除。' % len(manual))
    elif removed:
        result_ok('已撤销 %d 个端口，并关闭自动同步。' % len(removed))
    else:
        result_info('没有需要撤销的端口。')

def ports_enable(cfg, yes=False):
    """Validate prerequisites and enable auto-port.

    前置条件只有两个（与菜单逻辑保持一致）：
      1. frps Dashboard 可读（否则拿不到实际映射）
      2. 白名单非空（否则不会放行任何端口）
    防火墙是否启用**不是**必需条件：
      - 配了宝塔 API → 规则进面板数据库，不需要系统防火墙
      - 有面板没配 API → 只列清单，也不需要系统防火墙
      - 裸机         → 需要系统防火墙，由 ports_sync 在 direct 模式下报错
    """
    problems = []
    try:
        frps_dashboard_config()
    except (ValueError, OSError) as exc:
        problems.append(str(exc))
    if not cfg['allow_clients']:
        problems.append('客户端白名单为空：启用后不会有任何端口被放行，请先登记 clientID')
    if problems:
        raise ValueError('无法启用端口自动开通：\n  - ' + '\n  - '.join(problems))
    # 裸机直连模式才需要系统防火墙
    api_ready = panel_api_ready()
    has_panel = bool(panel_detect())
    if not api_ready and not has_panel:
        backend, active = firewall_backend()
        if not backend:
            raise ValueError('未检测到 ufw / firewalld / iptables，'
                             '也没有配置宝塔 API，无法放行端口。')
        if not active and not yes:
            if not confirm('系统防火墙未启用（%s 未运行）。\n'
                           '此时放行不会生效。仍要启用？' % backend, danger=True):
                return False
    cfg['enabled'] = True
    save_auto_port_config(cfg)
    timer_ok = ports_timer_install(cfg)
    ports_audit('enable', result='ok',
                detail='api=%s panel=%s timer=%s' % (api_ready, has_panel, timer_ok))
    if not timer_ok:
        # 非 systemd 平台没有定时器：退化为"进菜单时同步一次"
        result_info('已启用，但本平台没有可用的定时器（非 systemd）。',
                    detail='将改为每次进入「端口自动开通」时同步一次；'
                           '也可用 `frps ports apply` 手动触发。')
    return True

# ---------------------------------------------------------------- 定时同步

PORTS_TIMER_NAME = 'frp-simple-bash-ports'
PORTS_TIMER_DIR = Path('/etc/systemd/system')

def _systemd_available():
    if TEST: return False
    return bool(shutil.which('systemctl') and Path('/run/systemd/system').exists())

def ports_timer_installed():
    """定时器是否真的在运行（文件存在 ≠ 已启用）。"""
    if not (PORTS_TIMER_DIR / (PORTS_TIMER_NAME + '.timer')).exists():
        return False
    if not _systemd_available():
        return False
    try:
        out = run_cmd(['systemctl', 'is-active', PORTS_TIMER_NAME + '.timer'],
                      capture_output=True, text=True, timeout=20)
        return (out.stdout or '').strip().lower() == 'active'
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

def _python_bin():
    """定时器要用的 Python 绝对路径。

    unit 里写 /usr/bin/env python3 会依赖 PATH；root 服务的 PATH 若含可被
    非 root 写入的目录，定时任务就能被劫持。优先用当前解释器的绝对路径。
    """
    exe = sys.executable or ''
    if exe and os.path.isabs(exe) and os.access(exe, os.X_OK):
        return exe
    for candidate in ('/usr/bin/python3', '/usr/local/bin/python3'):
        if os.access(candidate, os.X_OK):
            return candidate
    return 'python3'


def ports_timer_install(cfg=None):
    """Install + enable the systemd timer that keeps firewall rules in sync.

    Returns True when the timer is active. Non-systemd platforms return False
    (sync then happens on menu entry / manual `frps ports apply`).
    """
    if not _systemd_available(): return False
    cfg = cfg or auto_port_config()
    interval = min(3600, max(5, int(cfg.get('sync_interval_seconds', 15))))
    script = INSTALL_ROOT / 'lib' / 'manager.py'
    service = (
        '[Unit]\n'
        'Description=frp-simple-bash auto port sync\n'
        'After=network-online.target\n\n'
        '[Service]\n'
        'Type=oneshot\n'
        'ExecStart=%s %s frps ports apply\n'
        % (_python_bin(), script)
    )
    timer = (
        '[Unit]\n'
        'Description=frp-simple-bash auto port sync timer\n\n'
        '[Timer]\n'
        'OnBootSec=30\n'
        'OnUnitActiveSec=%d\n'
        'AccuracySec=5\n\n'
        '[Install]\n'
        'WantedBy=timers.target\n'
        % interval
    )
    try:
        atomic(PORTS_TIMER_DIR / (PORTS_TIMER_NAME + '.service'), service)
        atomic(PORTS_TIMER_DIR / (PORTS_TIMER_NAME + '.timer'), timer)
        for path in (PORTS_TIMER_DIR / (PORTS_TIMER_NAME + '.service'),
                     PORTS_TIMER_DIR / (PORTS_TIMER_NAME + '.timer')):
            try: os.chmod(str(path), 0o644)
            except OSError: pass
        run_cmd(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=30)
        out = run_cmd(['systemctl', 'enable', '--now', PORTS_TIMER_NAME + '.timer'],
                      capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            audit('ports-timer-install-failed', detail=(out.stderr or '')[:200])
            return False
        check = run_cmd(['systemctl', 'is-active', PORTS_TIMER_NAME + '.timer'],
                        capture_output=True, text=True, timeout=20)
        if (check.stdout or '').strip().lower() != 'active':
            audit('ports-timer-inactive', detail=(check.stdout or '').strip()[:80])
            return False
        audit('ports-timer-installed')
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

def ports_timer_remove():
    """Disable + delete the timer; safe when it was never installed."""
    if not _systemd_available(): return False
    try:
        run_cmd(['systemctl', 'disable', '--now', PORTS_TIMER_NAME + '.timer'],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    for name in (PORTS_TIMER_NAME + '.timer', PORTS_TIMER_NAME + '.service'):
        unlink_quiet(PORTS_TIMER_DIR / name)
    try:
        run_cmd(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    audit('ports-timer-removed')
    return True

def ports_timer_status():
    """Human-readable timer state for the menu/CLI."""
    if not _systemd_available(): return '不适用（非 systemd 平台）'
    if not ports_timer_installed(): return '未安装'
    try:
        out = run_cmd(['systemctl', 'is-active', PORTS_TIMER_NAME + '.timer'],
                      capture_output=True, text=True, timeout=10)
        return '运行中' if (out.stdout or '').strip() == 'active' else '已停止'
    except (OSError, subprocess.SubprocessError):
        return '未知'
def auto_port_report_lines(report, plan_only=False):
    """把 ports_sync 的 report 渲染成人能看懂的若干行（CLI 用）。"""
    mode = report.get('mode', 'direct')
    mode_text = {'panel-api': '宝塔 API（规则写入面板数据库）',
                 'manual': '手动（只列清单，不改任何防火墙）',
                 'direct': '直写系统防火墙'}.get(mode, mode)
    lines = ['模式：' + mode_text]
    if report.get('busy'):
        lines.append('另一个进程正在同步，本次跳过。')
        return lines
    pending = report.get('pending') or []
    opened = report.get('opened') or []
    added = report.get('added') or []
    removed = report.get('removed') or []
    skipped = report.get('skipped') or []
    if pending:
        lines.append('待手工放行 %d 个：%s' % (len(pending), _list_or_more(
            ['%s/%s' % (x['port'], x['proto']) for x in pending])))
    if added:
        lines.append(('将放行 %d 个：' if plan_only else '已放行 %d 个：') % len(added)
                     + _list_or_more(['%s/%s' % (x['port'], x['proto']) for x in added]))
    if removed:
        lines.append(('将回收 %d 个：' if plan_only else '已回收 %d 个：') % len(removed)
                     + _list_or_more(['%s/%s' % (x['port'], x['proto']) for x in removed]))
    if opened:
        lines.append('累计已放行 %d 个' % len(opened))
    lines.extend(_skip_lines(skipped))
    return lines

def auto_port_menu():
    """端口自动开通：一屏 6 项，无子菜单。

    核心逻辑：读 frps 实际在线映射 → 有宝塔 API 就调 API 放行，没 API 就列清单手动加。
    进入菜单时若已启用且没有定时器（非 systemd），先静默同步一次作为兜底。
    """
    clear_screen()
    synced = False
    while True:
        cfg = auto_port_config()
        panel = panel_config()
        api_ready = panel_api_ready(panel)
        enabled = cfg['enabled']
        count = len(cfg['allow_clients'])
        if enabled and not synced and not ports_timer_installed():
            synced = True
            try: ports_sync(apply_changes=True)
            except MENU_ERRORS: pass

        def _prelude():
            # 3 行分别回答：怎么放行 / 放行谁 / 已经放行了多少
            print('  ' + _kv('放行方式', col_ok('宝塔 API 自动') if api_ready
                             else col_warn('手动（未配 API）')))
            print('  ' + _kv('白名单', col_ok('%d 个客户端' % count) if count
                             else _dim('不限（放行所有已认证客户端）')))
            opened = auto_port_state().get('opened') or []
            if not enabled:
                # 关闭状态下定时器必然不存在，显示它是纯噪声（用户反馈的困惑点）
                auto_text = col_warn('关')
            else:
                timer = ports_timer_status()
                if timer in ('运行中', '不适用（非 systemd 平台）'):
                    auto_text = col_ok('开') + '  ' + _dim(timer)
                else:
                    auto_text = col_bad('开') + '  ' + col_bad('定时器未运行，新端口不会自动放行！')
            print('  ' + _kv('自动同步', auto_text))
            print('  ' + _kv('已放行', (col_ok('%d 个端口' % len(opened)) if opened
                                      else _dim('0 个端口'))
                             + '  ' + _dim('（撤销用 6)')))
            print('  ' + _dim('命令行等价：frps ports status / plan / apply'))

        def _guide():
            if count and enabled:
                return                          # 配置齐全，不再打扰
            if not count:
                print('  ' + _tag('[提示]') + ' 建议顺序：')
                print('    ① 4) 宝塔 API（不配则需自己去面板手工加）')
                print('    ② 5) 开启自动同步')
                print('    ' + _dim('白名单是可选的：留空 = 放行所有已通过 token 认证的客户端'))
            elif not api_ready:
                print('  ' + _dim('未配宝塔 API：同步后需到面板手工放行；配了就能全自动。'))

        items = [
            '查看待放行端口',
            '同步并放行',
            '白名单（可选）',
            '宝塔 API',
            '关闭自动同步' if enabled else '开启自动同步',
            '撤销全部放行',
        ]
        groups = {0: '── 使用 ──', 2: '── 设置 ──', 5: '── 危险操作 ──'}
        def _prelude_all():
            _prelude()
            _guide()

        n = choose(items, '端口自动开通', '返回上一级', groups=groups, prelude=_prelude_all)
        if not n: return
        try:
            if n == 1:
                _show_pending_ports(cfg, panel); pause()
            elif n == 2:
                _apply_ports(cfg, panel); pause()
            elif n == 3:
                # 白名单内部已给出结果；用户按 0 返回时不该再吃一次回车
                _whitelist_action(cfg)
            elif n == 4:
                _panel_setup(panel)
            elif n == 5:
                if enabled:
                    # 关闭有副作用（删定时器、不再自动放行），必须有确认；
                    # 但它是可逆的，不需要输入 YES（危险级留给「撤销全部放行」）。
                    if not confirm('关闭后新映射不再自动放行（已放行的端口保持不变）。确认关闭？'):
                        result_info('已取消。'); pause(); continue
                    cfg['enabled'] = False; save_auto_port_config(cfg)
                    ports_timer_remove(); ports_audit('disable', result='ok')
                    result_ok('自动同步已停用。', detail='已放行的端口保持不变，需要时可用 6) 撤销。')
                else:
                    # 白名单可选（留空 = 放行所有已通过 token 认证的客户端），
                    # 这里不再强制要求配置白名单
                    if not confirm('开启后每 15 秒自动同步一次，把新映射的端口放行。确认？'):
                        result_info('已取消。'); pause(); continue
                    if ports_enable(cfg, yes=True):
                        result_ok('自动同步已开启。')
                pause()
            elif n == 6:
                opened = auto_port_state().get('opened') or []
                if not opened:
                    result_info('当前没有由本工具放行的端口。'); pause(); continue
                if not confirm('将删除本工具放行的 %d 个端口，并关闭自动同步。确认？'
                               % len(opened), danger=True):
                    result_info('已取消。'); pause(); continue
                outcome = ports_revert()
                _report_revert(outcome); pause()
        except MENU_ERRORS as exc:
            result_error(str(exc)); pause()

def _whitelist_action(cfg):
    """白名单：单次动作（添加或移除），不循环、不套子菜单。"""
    section('白名单')
    print('  当前：' + (', '.join(cfg['allow_clients'])
                       if cfg['allow_clients'] else col_bad('空（不会放行任何端口）')))
    online = _online_client_ids()
    if online:
        print('  ' + col_ok('在线') + '：' + ', '.join(
            cid if cid in cfg['allow_clients'] else cid + col_warn('（未加入）')
            for cid in sorted(online)))
    print(RULE)
    while True:
        n = choose(['添加客户端', '移除客户端'], '白名单操作', '返回上一级')
        if not n: return
        if n == 1:
            if online:
                options = sorted(online) + ['手动输入 clientID']
                index = choose(options, '选择要加入的客户端', '返回上一级')
                if not index: continue          # 0 = 退一层，回到本层
                value = options[index - 1] if index <= len(online) else ask(
                    'clientID（留空取消）', allow_empty=True)
            else:
                value = ask('clientID（留空取消）', allow_empty=True)
            if not value: continue
            if value in cfg['allow_clients']:
                result_info('该客户端已在白名单中。'); break
            cfg['allow_clients'].append(value)
            save_auto_port_config(cfg)
            result_ok('已加入白名单：' + value)
            break
        else:
            if not cfg['allow_clients']:
                result_info('白名单为空，没有可移除的客户端。'); break
            index = choose(cfg['allow_clients'], '选择要移除的客户端', '返回上一级')
            if not index: continue              # 0 = 退一层，回到本层
            removed = cfg['allow_clients'].pop(index - 1)
            save_auto_port_config(cfg)
            result_ok('已移除：' + removed)
            break

def _online_client_ids():
    """当前在线客户端的 clientID 集合（读不到时返回空）。"""
    try:
        rows = frps_online_proxies()
    except (ValueError, OSError):
        return set()
    ids = set()
    for item in rows:
        cid = str(item.get('clientID') or item.get('clientId') or '')
        if cid: ids.add(cid)
    return ids

def _panel_setup(panel):
    """宝塔 API：一次性配置（地址 → 密钥 → 测试），不套子菜单。"""
    section('宝塔 API 配置')
    print('  ' + _kv('地址', panel['url'] or color_state('未配置')))
    print('  ' + _kv('密钥', color_state('已配置') if panel['api_key']
                     else color_state('未配置')))
    print(RULE)
    result_info('在宝塔面板操作：设置 → API 接口 → 开启，复制「接口密钥」。',
                detail='建议把 API 白名单限制为 127.0.0.1，仅供本机调用。')
    if panel_api_ready(panel) and confirm('是否先测试当前连接是否正常？'):
        try:
            info = panel_probe(panel)
            result_ok('连接正常：面板当前有 %d 条防火墙规则。' % info['rules'])
        except MENU_ERRORS as exc:
            result_error('连接失败：' + str(exc),
                         hint='检查密钥、面板 API 是否开启、调用 IP 是否在白名单内。')
        return
    url = ask('API 地址（回车保留当前）', panel['url'] or 'http://127.0.0.1:8888',
              allow_empty=True)
    if url is None: return
    if url:
        if not url.lower().startswith(('http://', 'https://')):
            result_error('地址必须以 http:// 或 https:// 开头。'); return
        panel['url'] = url.rstrip('/')
    key = ask('API 密钥（输入不回显，回车保留当前）', secret=True, allow_empty=True)
    if key is None: return
    if key: panel['api_key'] = key.strip()
    save_panel_config(panel)
    if not panel_api_ready(panel):
        result_error('配置不完整：需要同时有 API 地址和密钥。'); return
    if not confirm('配置已保存，立即测试连接？'):
        result_ok('已保存。'); return
    try:
        info = panel_probe(panel)
        result_ok('连接成功：面板当前有 %d 条防火墙规则。' % info['rules'],
                  detail='之后新映射的端口会自动写入宝塔。')
    except MENU_ERRORS as exc:
        result_error('连接失败：' + str(exc),
                     hint='检查密钥是否正确、面板 API 是否开启、调用 IP 是否在白名单内。')

def _desired_ports(cfg):
    """实际在线且通过白名单/黑名单/配额过滤后的端口。"""
    try:
        desired, skipped = ports_desired(cfg)
    except (ValueError, OSError) as exc:
        raise ValueError('读取 frps Dashboard 失败（需要先在「修改服务端配置」里开启 Dashboard/API）：'
                         + str(exc))
    return desired, skipped

_SKIP_REASONS = {
    'client_not_allowed': '客户端不在白名单',
    'already_open': '端口此前已被放行（非本工具添加，不接管也不回收）',
    'denied_port': '命中敏感端口黑名单',
    'quota_exceeded': '超出单客户端配额',
    'port_in_use': '端口已被其它进程占用',
    'apply_failed': '放行失败',
}

def _list_or_more(items, limit=20, sep='、'):
    """把列表拼成一行，超出 limit 时明确说"等 N 个"，绝不静默丢弃。"""
    items = list(items)
    shown = sep.join(str(x) for x in items[:limit])
    if len(items) > limit:
        shown += ' 等 %d 个' % len(items)
    return shown


def _skip_lines(skipped, limit=6):
    """把跳过项转成人能看懂的行。"""
    counts = {}
    for item in skipped:
        reason = item.get('reason', '')
        counts.setdefault(reason, []).append(item)
    lines = []
    for reason, items in counts.items():
        ports = '、'.join('%s/%s' % (i.get('port'), i.get('proto')) for i in items[:limit])
        more = ' 等 %d 个' % len(items) if len(items) > limit else ''
        lines.append('跳过 %s：%s%s' % (_SKIP_REASONS.get(reason, reason), ports, more))
    return lines

def _show_pending_ports(cfg, panel):
    """只读：分别显示「未放行」和「已放行」两段，每行带 ✓/✗ 标记。"""
    # force=True：只读探查不该受"自动同步开关"影响，否则关闭时会误报"没有在线映射"
    report = ports_sync(apply_changes=False, force=True)
    pending = report.get('pending') or []
    opened = report.get('opened') or []
    skipped = report.get('skipped') or []
    mode = report.get('mode')
    if mode == 'manual':
        # 手工模式没有 opened 概念，先把 known 算成"已登记放行"
        known = {(int(r['port']), str(r['proto'])) for r in opened}
        desired, _ = _desired_ports(cfg)
        pending = [{'port': k[0], 'proto': k[1], 'proxy': v.get('proxy', '')}
                   for k, v in sorted(desired.items()) if k not in known]
    if not pending and not opened:
        if skipped:
            _report('没有可放行的端口',
                    ['在线映射里有 %d 个端口被规则拦下：' % len(skipped)] + _skip_lines(skipped))
            result_info('请检查白名单（3) 白名单）、敏感端口黑名单或配额设置。')
        else:
            result_info('当前没有在线映射。',
                        detail='先在客户端「端口映射」里添加映射并启动 frpc，再回来同步。')
        return
    lines = []
    if pending:
        lines.append('未放行 %d 个：' % len(pending))
        for item in pending[:20]:
            lines.append('   ✗ %s/%s  ← %s' % (item['port'], item['proto'],
                                               item.get('proxy') or '-'))
        if len(pending) > 20: lines.append('   … 另有 %d 个' % (len(pending) - 20))
    if opened:
        lines.append('已放行 %d 个：' % len(opened))
        for item in opened[:20]:
            lines.append('   ✓ %s/%s  ← %s' % (item['port'], item['proto'],
                                               item.get('proxy') or '-'))
        if len(opened) > 20: lines.append('   … 另有 %d 个' % (len(opened) - 20))
    if skipped:
        lines.append('')
        lines.extend(_skip_lines(skipped))
    _report('放行状态', lines)
    if pending:
        if mode == 'manual':
            result_info('以上 %d 个端口需要到宝塔面板「安全 → 防火墙」手工添加。' % len(pending),
                        detail='配好「4) 宝塔 API」后本工具可自动完成。')
        else:
            result_info('以上 %d 个端口尚未放行，按 2) 同步并放行 执行。' % len(pending))

def ports_apply_conclusion(report, via_menu=False):
    """把一次同步的结论告诉用户（菜单与 CLI 共用，避免两套结论）。"""
    mode = report.get('mode')
    added = report.get('added') or []
    removed = report.get('removed') or []
    pending = report.get('pending') or []
    skipped = report.get('skipped') or []
    failed = [x for x in skipped if x.get('reason') == 'apply_failed']
    if report.get('busy'):
        result_info('另一个进程正在同步，本次跳过。')
        return
    if mode == 'manual':
        if not pending:
            if skipped:
                result_info('没有端口被放行：全部被规则拦下。',
                            detail='检查白名单、敏感端口黑名单与配额设置'
                                   '（在 /etc/frp/auto-port.toml 里）。')
            else:
                result_info('没有需要放行的新端口。')
            return
        result_warn('已列出 %d 个端口，请在宝塔面板「安全 → 防火墙」手工添加。'
                    % len(pending),
                    detail='配好「4) 宝塔 API」后可自动完成，无需手工。')
        return
    if failed:
        result_error('%d 个端口放行失败。' % len(failed),
                     hint='重进「4) 宝塔 API」，保存配置后会自动测试连接。')
        return
    if added or removed:
        result_ok('同步完成：放行 %d 个，回收 %d 个。' % (len(added), len(removed)))
        return
    if skipped:
        result_info('没有端口被放行：全部被规则拦下。',
                    detail='检查白名单、敏感端口黑名单与配额设置'
                           '（在 /etc/frp/auto-port.toml 里）。')
        return
    result_info('没有需要放行的新端口。')


def _apply_ports(cfg, panel):
    """执行一次同步，并按真实结果给出结论（失败/全跳过不得报成功）。"""
    report = ports_sync(apply_changes=True, force=True)
    if not report.get('enabled'):
        result_info('自动同步当前是关闭的，本次为手动执行一次。',
                    detail='如需持续自动放行，用 5) 开启自动同步。')
    added = report.get('added') or []
    removed = report.get('removed') or []
    pending = report.get('pending') or []
    skipped = report.get('skipped') or []
    lines = []
    if added:
        lines.append('已放行 %d 个：%s' % (len(added), _list_or_more(
            ['%s/%s' % (x['port'], x['proto']) for x in added])))
    if removed:
        lines.append('已回收 %d 个：%s' % (len(removed), _list_or_more(
            ['%s/%s' % (x['port'], x['proto']) for x in removed])))
    if pending:
        lines.append('待手工放行 %d 个：%s' % (len(pending), _list_or_more(
            ['%s/%s' % (x['port'], x['proto']) for x in pending])))
    if skipped:
        lines.extend(_skip_lines(skipped))
    if lines:
        _report('同步结果', lines)
    ports_apply_conclusion(report, via_menu=True)


PORTS_USAGE = (
    '用法：frps ports <子命令>\n'
    '  status | plan | apply | enable | disable | clients | rules | revert\n'
    '  clients list|discover|add <id>|remove <id>\n'
    '  panel   status|set <url>|set-key|probe|clear\n'
    '  firewall status|plan|enable [--force] [--ssh-port N]|snapshot|revert\n'
    '也可用菜单：frps → 6) 端口自动开通'
)

_MODE_TEXT = {
    'readonly': '只读（只生成清单，不改防火墙）',
    'panel-api': '宝塔 API（规则写入面板数据库）',
    'direct': '直写系统防火墙',
    'manual': '手动（只列清单，不改任何防火墙）',
}
_FAMILY_TEXT = {
    'debian': 'Debian/Ubuntu 系', 'rhel': 'RHEL/CentOS 系',
    'alpine': 'Alpine（OpenRC）', 'arch': 'Arch 系',
    'windows': 'Windows', 'darwin': 'macOS', 'other': '其它发行版',
}
_BACKEND_TEXT = {
    'ufw': 'ufw', 'firewalld': 'firewalld', 'iptables': 'iptables',
    'nftables': 'nftables', 'windows': 'Windows 防火墙',
}


def _mode_label(mode):
    return _MODE_TEXT.get(str(mode), str(mode))


def _family_label(family):
    return _FAMILY_TEXT.get(str(family), str(family))


def ports_command(argv, flags=None):
    """CLI: ports status|plan|apply|enable|disable|clients|rules|revert."""
    flags = flags or {}
    as_json = bool(flags.get('json'))
    action = argv[0] if argv else 'status'
    if action == 'status':
        cfg = auto_port_config(); backend, active = firewall_backend()
        opened = auto_port_state().get('opened') or []
        payload = {'enabled': cfg['enabled'], 'backend': backend, 'firewall_active': active,
                   'allow_clients': cfg['allow_clients'],
                   'max_ports_per_client': cfg['max_ports_per_client'],
                   'deny_ports_enabled': cfg['deny_ports_enabled'],
                   'opened': [{'port': r.get('port'), 'proto': r.get('proto'),
                               'client_id': r.get('client_id'), 'proxy': r.get('proxy')} for r in opened]}
        if as_json: print(json.dumps(payload, ensure_ascii=False, indent=2)); return
        section('端口自动开通状态')
        print(_kv('功能', color_state('已启用') if cfg['enabled'] else color_state('未启用')))
        print(_kv('防火墙', (backend or '未检测到') + ('' if active else color_state('（未运行）'))))
        print(_kv('白名单', ', '.join(cfg['allow_clients']) if cfg['allow_clients'] else '空'))
        print(_kv('已放行', col_ok('%d 个端口' % len(opened)) if opened
                  else _dim('0 个端口')))
        print(RULE)
        return
    if action == 'plan':
        report = ports_sync(apply_changes=False)
        if as_json: print(json.dumps(report, ensure_ascii=False, indent=2)); return
        _report('变更计划（未执行）', auto_port_report_lines(report, plan_only=True)); return
    if action == 'apply':
        report = ports_sync(apply_changes=True, force=True)
        if as_json: print(json.dumps(report, ensure_ascii=False, indent=2)); return
        _report('同步结果', auto_port_report_lines(report))
        ports_apply_conclusion(report)          # 与菜单同一套结论，不再无条件报成功
        return
    if action == 'enable':
        cfg = auto_port_config()
        if ports_enable(cfg, yes=bool(flags.get('yes'))):
            result_ok('已启用端口自动开通。'); ports_sync(apply_changes=True)
        return
    if action == 'disable':
        cfg = auto_port_config(); cfg['enabled'] = False; save_auto_port_config(cfg)
        ports_timer_remove()
        ports_audit('disable', result='ok')
        result_ok('已关闭端口自动开通。')
        return
    if action == 'clients':
        cfg = auto_port_config(); sub = argv[1] if len(argv) > 1 else 'list'
        if sub == 'list' or sub == 'ls':
            print(json.dumps(cfg['allow_clients'], ensure_ascii=False, indent=2)); return
        if sub == 'discover':
            rows = frps_online_proxies(); seen = {}
            for item in rows:
                cid = str(item.get('clientID') or item.get('clientId') or '')
                if cid: seen[cid] = seen.get(cid, 0) + 1
            print(json.dumps(seen, ensure_ascii=False, indent=2)); return
        if sub == 'add' or sub == 'remove':
            if len(argv) < 3: raise ValueError('用法：ports clients add|remove <clientID>')
            target = argv[2]
            if sub == 'add':
                if target not in cfg['allow_clients']: cfg['allow_clients'].append(target)
            else:
                if target in cfg['allow_clients']: cfg['allow_clients'].remove(target)
            save_auto_port_config(cfg)
            result_ok(('已加入白名单：' if sub == 'add' else '已移除：') + target)
            return
        raise ValueError(PORTS_USAGE)
    if action == 'firewall':
        sub = argv[1] if len(argv) > 1 else 'status'
        info = firewall_detect()
        if sub == 'status':
            if as_json:
                print(json.dumps(info, ensure_ascii=False, indent=2, default=str)); return
            section('防火墙检测')
            print(_kv('发行版', _family_label(info['family'])))
            print(_kv('推荐管理器', info['preferred']))
            print(_kv('当前后端', (info['backend'] or '未检测到') if info['active']
                       else color_state('未启用')))
            for name, state in sorted((info.get('detail') or {}).items()):
                print('  ' + _pad_line('  - ' + name, 20) + state)
            if not info['active']:
                print()
                result_info(_firewall_hint_text(info))
            print(RULE); return
        if sub == 'plan':
            plan = firewall_enable_plan(ssh_port=flags.get('ssh_port'))
            if as_json:
                print(json.dumps(plan, ensure_ascii=False, indent=2, default=str)); return
            lines = ['目标管理器：%s（%s）' % (_BACKEND_TEXT.get(plan['target'], plan['target']),
                                          _family_label(plan['family']))]
            if plan['install_cmds']:
                lines.append('安装：' + ' '.join(plan['install_cmds'][0]))
            lines.append('放行 SSH：' + ', '.join(str(p) for p in plan['ssh_ports']))
            lines.append('放行 frps 端口：' + (', '.join('%d/%s' % (p, t) for p, t in plan['keep']) or '无'))
            for note in plan['notes']: lines.append('说明：' + note)
            _report('启用计划（未执行）', lines); return
        if sub == 'enable':
            result = firewall_enable(yes=bool(flags.get('yes')), force=bool(flags.get('force')),
                                     ssh_port=flags.get('ssh_port'))
            if result['ok']:
                result_ok('防火墙已启用：' + result['plan']['target'],
                          detail='已保存快照：' + result.get('snapshot', ''))
                if result.get('missing'):
                    result_error('⚠ 以下端口未被放行，外部客户端可能立即失联：'
                                 + '、'.join(result['missing']),
                                 hint='请立刻检查，或执行 `frps ports firewall revert` 回滚。')
                if auto_port_config()['enabled']:
                    ports_sync(apply_changes=True)
            else:
                result_error('防火墙未启用（%s）' % (result.get('reason') or '未知'),
                             hint='可先执行 `frps ports firewall plan` 查看将要执行的命令。')
            return
        if sub == 'snapshot':
            snap = firewall_snapshot()
            path = firewall_snapshot_save(snap)
            result_ok('已保存防火墙快照：', detail=str(path))
            return
        if sub == 'revert':
            result = firewall_revert(yes=bool(flags.get('yes')))
            if result.get('ok'):
                result_ok('已回滚防火墙。', detail='；'.join(result.get('actions') or []))
            else:
                result_info('已取消回滚。')
            return
        raise ValueError(PORTS_USAGE)
    if action == 'panel':
        sub = argv[1] if len(argv) > 1 else 'status'
        panel = panel_config()
        if sub == 'status':
            payload = {'type': panel['type'], 'url': panel['url'],
                       'key_configured': bool(panel['api_key']),
                       'verify_tls': panel['verify_tls'], 'mode': firewall_mode()}
            if as_json: print(json.dumps(payload, ensure_ascii=False, indent=2)); return
            section('面板 API 状态')
            print(_kv('面板类型', panel['type']))
            print(_kv('API 地址', panel['url'] or color_state('未配置')))
            print(_kv('API 密钥', color_state('已配置') if panel['api_key']
                       else color_state('未配置')))
            print(_kv('工作模式', _mode_label(payload['mode'])))
            print(RULE); return
        if sub == 'set':
            if len(argv) < 3: raise ValueError('用法：ports panel set <url>（密钥请用交互输入或环境变量）')
            url = argv[2].rstrip('/')
            if not url.lower().startswith(('http://', 'https://')):
                raise ValueError('API 地址必须以 http:// 或 https:// 开头')
            if len(argv) > 3:
                # 密钥不进 argv（会留在 shell history / ps 输出里）
                raise ValueError('请勿在命令行传 API 密钥：'
                                 '改用交互输入（frps ports panel set-key）'
                                 '或环境变量 FRP_PANEL_API_KEY')
            panel['url'] = url
            save_panel_config(panel)
            result_ok('已保存 API 地址：' + url); return
        if sub == 'set-key':
            import getpass
            try: key = getpass.getpass('面板 API 密钥（输入不回显）：').strip()
            except EOFError: raise ValueError('已取消')
            if not key: raise ValueError('密钥不能为空')
            panel['api_key'] = key
            save_panel_config(panel)
            result_ok('已保存 API 密钥（写入 auto-port.toml，权限 0600）。'); return
        if sub == 'probe':
            info = panel_probe(panel)
            if as_json: print(json.dumps({'ok': info['ok'], 'rules': info['rules']},
                                         ensure_ascii=False, indent=2)); return
            result_ok('API 连接正常：当前有 %d 条防火墙规则。' % info['rules']); return
        if sub == 'clear':
            if not flags.get('yes'):
                raise ValueError('清空面板 API 配置后自动放行会失效：请追加 --yes 明确授权。')
            panel['url'] = ''; panel['api_key'] = ''
            save_panel_config(panel)
            result_ok('已清空面板 API 配置。'); return
        raise ValueError(PORTS_USAGE)
    if action == 'rules':
        opened = auto_port_state().get('opened') or []
        if as_json: print(json.dumps(opened, ensure_ascii=False, indent=2)); return
        if not opened: result_info('当前没有由本工具放行的规则。'); return
        _report('已放行规则', ['%s/%s  client=%s  proxy=%s' % (r.get('port'), r.get('proto'),
                                                    r.get('client_id', '-'), r.get('proxy', '-'))
                               for r in opened]); return
    if action == 'revert':
        if not flags.get('yes'):
            raise ValueError('ports revert 会删除本工具放行的全部端口，属破坏性操作：'
                             '请追加 --yes 明确授权。')
        outcome = ports_revert()
        _report_revert(outcome); return
    raise ValueError(PORTS_USAGE)
WEB_SERVICE = 'frp-simple-bash-web'

def _web_unit_paths():
    """返回 (unit_path, kind)；kind ∈ systemd/openrc/launchd/unknown。"""
    if os.name == 'nt':
        return None, 'windows'
    if _systemd_available():
        return Path('/etc/systemd/system') / (WEB_SERVICE + '.service'), 'systemd'
    if shutil.which('rc-service') or Path('/etc/init.d').is_dir():
        return Path('/etc/init.d') / WEB_SERVICE, 'openrc'
    if sys.platform == 'darwin':
        return Path('/Library/LaunchDaemons/frp-simple-bash.web.plist'), 'launchd'
    return None, 'unknown'

def web_service_installed():
    path, kind = _web_unit_paths()
    if kind == 'windows': return False
    return bool(path and path.exists())

def web_service_install():
    """安装并启动 Web 管理端常驻服务。返回 (ok, message)。"""
    path, kind = _web_unit_paths()
    if kind == 'windows':
        return False, ('Windows 暂不支持自动注册常驻 Web 服务；'
                       '可手动运行 bin\\frpc-web.ps1，或用 WinSW 自行注册')
    if path is None:
        return False, '当前平台不支持自动注册服务（请手动用 nohup/screen 运行 frpc-web）'
    template = INSTALL_ROOT / 'templates' / ('frp-simple-bash-web.' + (
        'service' if kind == 'systemd' else 'openrc' if kind == 'openrc' else 'plist'))
    if not template.exists():
        return False, '缺少服务模板：' + str(template)
    try:
        if kind == 'systemd':
            atomic(path, template.read_text(encoding='utf-8'))
            os.chmod(str(path), 0o644)
            run_cmd(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=30)
            out = run_cmd(['systemctl', 'enable', '--now', WEB_SERVICE],
                          capture_output=True, text=True, timeout=60)
            return out.returncode == 0, (out.stderr or out.stdout).strip()[:300]
        if kind == 'openrc':
            atomic(path, template.read_text(encoding='utf-8'))
            os.chmod(str(path), 0o755)
            run_cmd(['rc-update', 'add', WEB_SERVICE, 'default'],
                    capture_output=True, text=True, timeout=30)
            out = run_cmd(['rc-service', WEB_SERVICE, 'restart'],
                          capture_output=True, text=True, timeout=60)
            return out.returncode == 0, (out.stderr or out.stdout).strip()[:300]
        atomic(path, template.read_text(encoding='utf-8'))
        os.chmod(str(path), 0o644)
        run_cmd(['launchctl', 'bootout', 'system/' + WEB_SERVICE],
                capture_output=True, text=True, timeout=30)
        out = run_cmd(['launchctl', 'bootstrap', 'system', str(path)],
                      capture_output=True, text=True, timeout=60)
        return out.returncode == 0, (out.stderr or out.stdout).strip()[:300]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, str(exc)

def web_service_uninstall():
    """停止并移除 Web 管理端服务（卸载 frpc 时调用）。"""
    path, kind = _web_unit_paths()
    if kind == 'windows' or path is None:
        return False
    try:
        if kind == 'systemd':
            run_cmd(['systemctl', 'disable', '--now', WEB_SERVICE],
                    capture_output=True, text=True, timeout=60)
        elif kind == 'openrc':
            run_cmd(['rc-service', WEB_SERVICE, 'stop'], capture_output=True, text=True, timeout=60)
            run_cmd(['rc-update', 'del', WEB_SERVICE, 'default'], capture_output=True, text=True, timeout=30)
        else:
            run_cmd(['launchctl', 'bootout', 'system/' + WEB_SERVICE],
                    capture_output=True, text=True, timeout=60)
        if path.exists(): unlink_quiet(path)
        if kind == 'systemd':
            run_cmd(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=30)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

def run_detached(argv):
    """在**服务 cgroup 之外**执行命令，避免脚本停掉本服务时把自己杀掉。

    通过 systemd-run 起临时 unit；没有 systemd 时退化为 setsid。
    返回 (ok, message)。
    """
    try:
        if _systemd_available() and shutil.which('systemd-run'):
            unit = 'frp-detached-' + secrets.token_hex(4)
            proc = run_cmd(['systemd-run', '--collect', '--quiet',
                            '--unit', unit, '--'] + list(argv),
                           capture_output=True, text=True, timeout=60)
            return proc.returncode == 0, (proc.stderr or '').strip()[:200]
        if shutil.which('setsid'):
            subprocess.Popen(list(argv), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, ''
        subprocess.Popen(list(argv), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, ''
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def web_service_restart_detached():
    """重启 Web 服务（用于升级后加载新代码）。

    必须在独立单元里执行：否则重启会杀掉发起者本身，导致升级"看起来成功、
    实际还在跑旧代码"。
    """
    path, kind = _web_unit_paths()
    if kind == 'systemd':
        return run_detached(['systemctl', 'restart', WEB_SERVICE])
    if kind == 'openrc':
        return run_detached(['rc-service', WEB_SERVICE, 'restart'])
    if kind == 'launchd':
        return run_detached(['launchctl', 'kickstart', '-k', 'system/' + WEB_SERVICE])
    return False, '当前平台不支持自动重启'

def web_service_disable():
    """停止并禁用 Web 服务，但保留单元文件（便于 --enable 恢复）。

    这是"忘记密码 / 改坏配置"时的终端逃生口：服务停掉后不影响 frpc 本体，
    用 --enable 或 --install-service 可以再起回来。
    """
    path, kind = _web_unit_paths()
    if kind == 'windows':
        return False, 'Windows 请用任务管理器或 net stop 停止服务'
    if path is None:
        return False, '当前平台不支持服务管理'
    if not path.exists():
        return False, 'Web 服务尚未安装（可先运行 frpc web --install-service）'
    try:
        if kind == 'systemd':
            out = run_cmd(['systemctl', 'disable', '--now', WEB_SERVICE],
                          capture_output=True, text=True, timeout=60)
        elif kind == 'openrc':
            run_cmd(['rc-service', WEB_SERVICE, 'stop'], capture_output=True, text=True, timeout=60)
            out = run_cmd(['rc-update', 'del', WEB_SERVICE, 'default'],
                          capture_output=True, text=True, timeout=30)
        else:
            run_cmd(['launchctl', 'bootout', 'system/' + WEB_SERVICE],
                    capture_output=True, text=True, timeout=60)
            out = run_cmd(['launchctl', 'disable', 'system/' + WEB_SERVICE],
                          capture_output=True, text=True, timeout=30)
        return out.returncode == 0, (out.stderr or '').strip()[:200]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def web_service_enable():
    """重新启用并启动已注册的 Web 服务（--disable 的逆操作）。"""
    path, kind = _web_unit_paths()
    if kind == 'windows':
        return False, 'Windows 请手动启动服务'
    if path is None:
        return False, '当前平台不支持服务管理'
    if not path.exists():
        return False, 'Web 服务尚未安装（可先运行 frpc web --install-service）'
    try:
        if kind == 'systemd':
            out = run_cmd(['systemctl', 'enable', '--now', WEB_SERVICE],
                          capture_output=True, text=True, timeout=60)
        elif kind == 'openrc':
            run_cmd(['rc-update', 'add', WEB_SERVICE, 'default'],
                    capture_output=True, text=True, timeout=30)
            out = run_cmd(['rc-service', WEB_SERVICE, 'start'],
                          capture_output=True, text=True, timeout=60)
        else:
            run_cmd(['launchctl', 'enable', 'system/' + WEB_SERVICE],
                    capture_output=True, text=True, timeout=30)
            out = run_cmd(['launchctl', 'bootstrap', 'system', str(path)],
                          capture_output=True, text=True, timeout=60)
        return out.returncode == 0, (out.stderr or '').strip()[:200]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def web_service_restart():
    """重启 Web 服务（让改过的端口/绑定/密码生效）。

    比让用户自己记 systemctl/rc-service/launchctl 更可靠：不同平台命令不同，
    文档里写 `systemctl restart ...` 在 OpenRC/macOS 上就是错的。
    """
    path, kind = _web_unit_paths()
    if kind == 'windows':
        return False, 'Windows 请手动重启服务'
    if path is None:
        return False, '当前平台不支持服务管理'
    if not path.exists():
        return False, 'Web 服务尚未安装（可先运行 frpc web --install-service）'
    try:
        if kind == 'systemd':
            out = run_cmd(['systemctl', 'restart', WEB_SERVICE],
                          capture_output=True, text=True, timeout=60)
        elif kind == 'openrc':
            out = run_cmd(['rc-service', WEB_SERVICE, 'restart'],
                          capture_output=True, text=True, timeout=60)
        else:
            out = run_cmd(['launchctl', 'kickstart', '-k', 'system/' + WEB_SERVICE],
                          capture_output=True, text=True, timeout=60)
        return out.returncode == 0, (out.stderr or out.stdout or '').strip()[:200]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def web_service_status():
    """返回 Web 服务状态文本（未安装时返回 '未安装'）。"""
    path, kind = _web_unit_paths()
    if kind == 'windows':
        return '不适用'
    if path is None:
        return '不适用'
    if not path.exists():
        return '未安装'
    try:
        if kind == 'systemd':
            out = run_cmd(['systemctl', 'is-active', WEB_SERVICE],
                          capture_output=True, text=True, timeout=20)
            # 必须归一为中文：菜单用中文元组判断，返回 'active'/'inactive'
            # 会让第 2 项永远是「停止并禁用服务」，服务停了也起不回来
            raw = (out.stdout or '').strip().lower()
            return {'active': '运行中', 'inactive': '已停止', 'failed': '已停止',
                    'activating': '启动中', 'deactivating': '停止中'}.get(raw, raw or '未知')
        if kind == 'openrc':
            out = run_cmd(['rc-service', WEB_SERVICE, 'status'],
                          capture_output=True, text=True, timeout=20)
            return '已停止' if out.returncode else '运行中'
        out = run_cmd(['launchctl', 'print', 'system/' + WEB_SERVICE],
                      capture_output=True, text=True, timeout=20)
        return '运行中' if out.returncode == 0 else '已停止'
    except (OSError, ValueError, subprocess.SubprocessError):
        return '未知'

def install(role, yes=False, no_start=False, non_interactive=False):
    script = Path(__file__).resolve().parent.parent / ('install-' + role + ('.ps1' if os.name == 'nt' else '.sh'))
    if not script.exists(): raise ValueError('安装脚本未安装')
    if os.name == 'nt':
        args = ['powershell', '-File', str(script), '-Version', 'v' + VERSION]
        if no_start: args += ['-NoStart']
        if yes: args += ['-Yes']
    else:
        args = ['bash', str(script), '--version', VERSION]
        if yes: args.append('--yes')
        if no_start: args.append('--no-start')
        if non_interactive: args.append('--non-interactive')
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
    """Choose keep-config vs purge; returns True only when uninstall ran."""
    choice = choose(['保留配置和日志（推荐）', '彻底清理（删除配置、映射、日志和备份）'], '卸载 ' + role)
    if not choice: return False
    if choice == 1:
        uninstall(role, purge=False)
        return True
    if not confirm('彻底清理将删除 /etc/frp 下全部配置、映射、日志与备份，且不可恢复。确认继续？', danger=True):
        result_info('已取消。'); return False
    uninstall(role, purge=True, yes=True)
    return True

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

class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only while they stay on https (SEC-1)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith('https://'):
            raise ValueError('拒绝跳转到非 HTTPS 地址：' + newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def _https_get(url, timeout=60):
    if not url.startswith('https://'):
        raise ValueError('拒绝非 HTTPS 下载：' + url)
    req = urllib.request.Request(url, headers={'User-Agent': 'frp-simple-bash/' + MANAGER_RELEASE_VERSION})
    opener = urllib.request.build_opener(_HttpsOnlyRedirect, urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()

def _latest_manager_version(mirror):
    """Read the authoritative latest manager version from the mirror.

    The bundled install-frps.sh at the mirror root is re-written on every
    release with the new FRP_MANAGER_VERSION, so it is the source of truth for
    "what's the newest version" — the local script's own hard-coded version
    would otherwise make upgrade() always target the currently-installed one.
    """
    import re as _re
    bootstrap = _https_get(f'{mirror}/frp-simple-bash/install-frps.sh', timeout=30).decode('utf-8', 'replace')
    m = _re.search(r"FRP_MANAGER_VERSION='([^']+)'", bootstrap)
    if not m:
        m = _re.search(r'FRP_MANAGER_VERSION="([^"]+)"', bootstrap)
    if not m:
        raise ValueError('镜像站引导脚本中未找到 FRP_MANAGER_VERSION，无法确定最新版本。')
    version = m.group(1)
    if not version.startswith('v') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in version):
        err('E017', '镜像站返回了非法的最新版本号：' + version)
    return version

# 与 upgrade() 实际写入范围保持一致（含 bin/ 与 templates/），否则回滚不完整（BUG-2-10）
UPGRADE_FILES = (
    'lib/manager.py', 'lib/download.sh', 'install-common.sh',
    'install-frps.sh', 'install-frpc.sh', 'install-windows.ps1',
    'install-frps.ps1', 'install-frpc.ps1', 'uninstall.sh',
    'uninstall-frps.ps1', 'uninstall-frpc.ps1', 'uninstall-windows.ps1',
    'bin/frps', 'bin/frpc', 'bin/frps.ps1', 'bin/frpc.ps1',
    'bin/frps.cmd', 'bin/frpc.cmd',
    'templates/frps.service', 'templates/frpc.service',
    'templates/frps.openrc', 'templates/frpc.openrc', 'templates/frpc.plist',
    'lib/webui.py',
)

def upgrade_snapshot():
    """Persist the current manager scripts before upgrade (详细设计 §12.4)."""
    base = ROOT / 'backups'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-upgrade-' + secrets.token_hex(3)
    staging = base / ('.staging-' + name)
    staging.mkdir(parents=True, mode=0o700)
    try:
        targets = list(UPGRADE_FILES)
        # web/dist 下的资源名带构建哈希，无法枚举，按目录整体快照
        dist = INSTALL_ROOT / 'web' / 'dist'
        if dist.is_dir():
            targets.extend(str(p.relative_to(INSTALL_ROOT))
                           for p in sorted(dist.rglob('*')) if p.is_file())
        copied, digests = [], {}
        for rel in targets:
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
    """Restore manager scripts from an upgrade snapshot.

    The directory MUST live directly under ROOT/backups: this writes into
    INSTALL_ROOT, so an arbitrary path would let anyone replace root-owned
    manager scripts (SEC-4).
    """
    base = (ROOT / 'backups').resolve()
    directory = Path(directory)
    if directory.is_symlink() or directory.resolve().parent != base:
        raise ValueError('升级快照路径非法：必须位于 ' + str(base))
    manifest = directory / 'manifest.json'
    if not manifest.exists(): raise ValueError('升级快照缺少 manifest.json')
    data = read_json(manifest, {})
    # Stage + verify everything first, then commit with rollback (SEC-5):
    # a mid-way failure must never leave a half-restored manager.
    staged = []
    for rel in data.get('files', []):
        relative = Path(rel)
        if relative.is_absolute() or '..' in relative.parts: raise ValueError('快照路径非法：' + rel)
        src = directory / 'install-root' / relative
        if not src.is_file(): raise ValueError('快照文件缺失：' + rel)
        payload = src.read_bytes()
        expected = (data.get('sha256') or {}).get(rel)
        if expected and hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError('快照文件校验失败：' + rel)
        dest = INSTALL_ROOT / relative
        if not _within(dest, INSTALL_ROOT): raise ValueError('目标路径非法：' + rel)
        mode = 0o755 if (rel.endswith('.sh') or rel.endswith('.ps1')) else 0o644
        staged.append((dest, payload, mode))
    backups = {}
    committed = []
    try:
        for dest, payload, mode in staged:
            if dest.exists(): backups[dest] = (dest.read_bytes(), dest.stat().st_mode & 0o777)
            else: backups[dest] = None
            # 先登记再写：chmod/os.replace 中途失败时该文件也必须回滚。
            committed.append(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + '.restore')
            tmp.write_bytes(payload); os.replace(str(tmp), str(dest)); os.chmod(str(dest), mode)
    except BaseException:
        for dest in committed:
            old = backups.get(dest)
            try:
                if old is None: unlink_quiet(dest)
                else:
                    tmp = dest.with_name(dest.name + '.rollback')
                    tmp.write_bytes(old[0]); os.replace(str(tmp), str(dest)); os.chmod(str(dest), old[1])
            except OSError: pass
        raise
    return [str(d.relative_to(INSTALL_ROOT)) for d, _, _ in staged]

def _service_manager():
    """Return 'systemd' / 'openrc' / '' for the running host."""
    if os.name == 'nt': return ''
    if shutil.which('systemctl') and Path('/run/systemd/system').exists(): return 'systemd'
    if shutil.which('rc-service') and shutil.which('rc-update'): return 'openrc'
    if sys.platform == 'darwin' and shutil.which('launchctl'): return 'launchd'
    return ''

def installed_unit_path(role):
    """Where the service unit for this component lives on this host."""
    manager = _service_manager()
    if manager == 'systemd': return Path('/etc/systemd/system') / (role + '.service')
    if manager == 'openrc': return Path('/etc/init.d') / role
    if manager == 'launchd': return Path('/Library/LaunchDaemons') / ('frp-simple-bash.' + role + '.plist')
    return None

def template_path(role):
    manager = _service_manager()
    if manager == 'systemd': return INSTALL_ROOT / 'templates' / (role + '.service')
    if manager == 'openrc': return INSTALL_ROOT / 'templates' / (role + '.openrc')
    if manager == 'launchd': return INSTALL_ROOT / 'templates' / (role + '.plist')
    return None

def unit_differs(role):
    """True when the registered unit content differs from the current template."""
    if role == WEB_SERVICE:
        unit, kind = _web_unit_paths()
        if unit is None: return False
        template = INSTALL_ROOT / 'templates' / ('frp-simple-bash-web.' + (
            'service' if kind == 'systemd' else 'openrc' if kind == 'openrc' else 'plist'))
    else:
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
    if not manager:
        result_info('当前平台没有可刷新的服务单元（Windows 服务由安装器管理）。')
        return []
    wanted = list(roles or ('frps', 'frpc'))
    # Web 管理端也是独立单元，模板更新后必须一起刷新（否则永远拿不到加固修复）
    if web_service_installed() and WEB_SERVICE not in wanted:
        wanted.append(WEB_SERVICE)
    stale = [r for r in wanted if unit_differs(r)]
    if not stale: return []
    names = '、'.join(stale)
    result_info('检测到服务单元模板已更新：' + names,
                detail='已注册的 ' + manager + ' 单元与当前模板不一致，可刷新使新配置生效。')
    if not yes and not confirm('用新模板刷新 ' + names + ' 的服务单元并 daemon-reload？'):
        result_info('已跳过服务单元刷新；现有服务注册保持不变。')
        return []
    refreshed, failed = [], []
    for role in stale:
        if role == WEB_SERVICE:
            unit, web_kind = _web_unit_paths()
            template = INSTALL_ROOT / 'templates' / ('frp-simple-bash-web.' + (
                'service' if web_kind == 'systemd' else 'openrc' if web_kind == 'openrc'
                else 'plist'))
            if unit is None or not template.exists():
                failed.append(role + '（缺少模板或平台不支持）')
                continue
        else:
            unit, template = installed_unit_path(role), template_path(role)
        try:
            old = unit.read_bytes() if unit.exists() else None
            atomic(unit, template.read_text(encoding='utf-8'))
            # systemd unit and launchd plist are both 0644; openrc init scripts are 0755.
            os.chmod(str(unit), 0o755 if manager == 'openrc' else 0o644)
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
            # manifest 记录的必须是磁盘上真实存在的文件名（BUG-2-11）
            atomic(directory / 'manifest.json', json.dumps(
                {'files': [(role + '.service.old')] if old is not None else [],
                 'reason': 'unit-refresh'}, ensure_ascii=False, indent=2) + '\n')
        except OSError: pass
    reload_note = ''
    if refreshed:
        if manager == 'systemd':
            try: run_cmd(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError): pass
            reload_note = '（已 daemon-reload）'
        elif manager == 'launchd':
            reload_note = '（plist 已更新；下次 launchd 加载或重启时生效）'
        else:
            reload_note = '（脚本已更新；重启该服务后生效）'
    if refreshed: audit('units-refreshed')
    if refreshed: result_ok('已刷新服务单元：' + '、'.join(refreshed) + reload_note)
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
    mirror = os.environ.get('FRP_MIRROR_URL', 'https://download.sueiyuan.top').rstrip('/')
    if mirror in ('', 'off', 'OFF'):
        raise ValueError('升级需要 HTTPS 镜像；FRP_MIRROR_URL=off 时无法下载管理器发布包。')
    if not mirror.startswith('https://') or any(c.isspace() for c in mirror):
        raise ValueError('FRP_MIRROR_URL 必须是合法 HTTPS 地址。')
    if target is None:
        # Discover the newest version from the mirror (the local hard-coded
        # version is the CURRENT one, never the target).
        result_info('正在检查镜像站最新版本…')
        try:
            target = _latest_manager_version(mirror)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ValueError('无法获取镜像站最新版本：' + str(exc) + '；可指定版本：frpc upgrade <version>')
    if not target.startswith('v') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in target):
        err('E017', '非法的管理器版本号：' + str(target))
    if target == before:
        result_info('当前已是最新版本：' + before + '，无需升级。')
        return False
    result_info('将更新管理器至：' + target)
    if not yes and not confirm('更新管理器脚本（保留 frp 二进制与配置）？'): return False
    snapshot = None
    try: snapshot = upgrade_snapshot()
    except (OSError, ValueError) as exc:
        if not yes and not confirm('升级前备份失败（' + str(exc) + '），仍要继续？'): return False
    try:
        bundle_url = f'{mirror}/frp-simple-bash/releases/{target}/source.tar.gz'
        sha_url = f'{mirror}/frp-simple-bash/releases/{target}/SHA256SUMS.txt'
        bundle = _https_get(bundle_url)
        manifest = _https_get(sha_url).decode('utf-8', 'replace')
    except (OSError, ValueError, urllib.error.URLError) as exc:
        err('E003', '无法下载管理器发布包：' + str(exc))
    expected = ''
    for line in manifest.splitlines():
        cols = line.split()
        if len(cols) == 2 and cols[1] in ('source.tar.gz', '*source.tar.gz'):
            expected = cols[0]
            break
    if not expected or len(expected) != 64:
        err('E004', '发布包清单中没有有效的 source.tar.gz SHA256。')
    if hashlib.sha256(bundle).hexdigest() != expected:
        err('E004', '管理器发布包 SHA256 不匹配，已拒绝更新。')
    import tarfile, io as _io, tempfile as _tempfile
    manager_files = (
        'lib/manager.py', 'lib/download.sh', 'lib/webui.py', 'install-common.sh',
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
            if not (rel in manager_files or rel.startswith('bin/')
                    or rel.startswith('templates/') or rel.startswith('web/dist/')):
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
    return True


CLI_FLAGS = ('--json', '--yes', '--purge', '--follow', '--no-start', '--non-interactive', '--force')

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
        if item == '--ssh-port' and i + 1 < len(argv):
            flags['ssh_port'] = argv[i + 1]; i += 2; continue
        if item in CLI_FLAGS:
            # Normalize flag key: strip leading dashes, and map '-'-joined
            # names to '_' so readers can use a single stable key.
            flags[item.lstrip('-').replace('-', '_')] = True; i += 1; continue
        if item.startswith('-') and item != '-':
            # 未知参数必须报错，否则 `frpc start --bogus` 会被静默接受（BUG-1-1）
            raise ValueError('未知参数：' + item + '；可用参数：' + ', '.join(CLI_FLAGS))
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
    role = argv.pop(0)
    # 配置目录 /etc/frp 权限 0700（仅 root），普通用户运行会得到裸
    # Permission denied。在 initialize() 之前拦截，给出明确、可操作的提示。
    if not TEST:
        if os.name == 'nt':
            try:
                import ctypes
                if not ctypes.windll.shell32.IsUserAnAdmin():
                    err('E002', f'需要管理员权限；请以管理员身份运行 {role}（配置目录 ACL 仅允许管理员）。')
            except (AttributeError, OSError):
                pass
        elif os.geteuid() != 0:
            err('E002', f'配置目录 {ROOT} 仅 root 可访问；请用 sudo {role} 运行。')
    # macOS 只提供客户端 frpc，不提供服务端 frps。
    if sys.platform == 'darwin' and role == 'frps':
        err('E001', 'macOS 仅支持客户端 frpc，不提供 frps 服务端；如需 frps 请在 Linux 服务器上运行。')
    initialize()
    refresh_pending(role)      # 清掉「重启发生在菜单外」留下的过期 pending
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
        _args, flags = _parse_flags(argv)
        if not flags.get('yes'):
            raise ValueError('logs-clean 会删除全部轮转归档：请追加 --yes 明确授权。')
        removed = 0
        for item in log_files(role):
            if item.name == role + '.log': continue
            unlink_quiet(item); removed += 1
        print(json.dumps({'removed': removed}, ensure_ascii=False)); return
    if cmd == 'diagnose': print(json.dumps(diagnostics_report(role), ensure_ascii=False, indent=2)); return
    if cmd == 'backups': print(json.dumps([{k: str(v) if k == 'path' else v for k, v in item.items()} for item in list_backups()], ensure_ascii=False, indent=2)); return
    if cmd == 'restore':
        argv, flags = _parse_flags(argv)
        if not argv: raise ValueError('用法：restore <备份目录名> [--yes]')
        name = argv[0]
        if not re.fullmatch(r'[A-Za-z0-9._-]+', name) or name in ('.', '..'):
            raise ValueError('restore 只接受 backups 下的目录名，不接受路径')
        directory = ROOT / 'backups' / name
        # Upgrade snapshots store files under install-root/ and restore manager
        # scripts in INSTALL_ROOT; ordinary backups store files at the backup
        # root and restore into ROOT. Detect which kind and dispatch.
        manifest = directory / 'manifest.json'
        reason = read_json(manifest, {}).get('reason', '') if manifest.exists() else ''
        if not flags.get('yes'):
            raise ValueError('restore 会覆盖当前配置：请追加 --yes 明确授权。')
        if reason != 'upgrade':
            # 与菜单保持一致：恢复前先留一份当前状态的快照，便于反悔
            try: backup(managed_paths(), 'pre-restore')
            except (ValueError, OSError): pass
        if reason == 'upgrade':
            restored = restore_upgrade_snapshot(directory)
        else:
            restored = restore_backup(directory)
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
        install(role, yes=bool(flags.get('yes')), no_start=bool(flags.get('no_start')),
                non_interactive=bool(flags.get('non_interactive')))
        return
    if cmd == 'uninstall':
        _a, flags = _parse_flags(argv)
        uninstall(role, purge=bool(flags.get('purge')), yes=bool(flags.get('yes')))
        return
    if cmd in ('upgrade','update'):
        a, flags = _parse_flags(argv)
        # 兼容文档写法：frpc upgrade <version>（位置参数）与 --version 两种
        target = flags.get('version') or (a[0] if a else None)
        upgrade(role, target, yes=bool(flags.get('yes')))
        return
    if cmd in ('refresh-units', 'refresh_units'):
        _a, flags = _parse_flags(argv)
        done = refresh_units(('frps', 'frpc'), yes=bool(flags.get('yes')))
        print(json.dumps({'ok': True, 'code': 0, 'refreshed': done, 'error': None},
                         ensure_ascii=False, indent=2))
        return
    if cmd == 'ports':
        if role != 'frps': err('E001', '端口自动开通仅适用于服务端 frps；客户端如需固定公网端口请使用映射。')
        sub, flags = None, {}
        if argv:
            sub = [argv[0]]
            rest, flags = _parse_flags(argv[1:])
            sub = [argv[0]] + rest
        else:
            flags = {}
        ports_command(sub or ['status'], flags)
        return
    if cmd == 'web':
        if role != 'frpc':
            err('E001', 'Web 管理端目前只支持客户端 frpc。')
        _webui_module().main(argv)
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
        args, flags = _parse_flags(argv)
        mapping_command(args, flags)
        return
    raise ValueError('未知命令；无参数打开数字菜单')

if __name__ == '__main__':
    try: run()
    except QuitProgram: print('\n已退出。')
    except (EOFError, KeyboardInterrupt): print('\n已退出。')
    except Exception as exc: print('错误：' + str(exc), file=sys.stderr); sys.exit(1)
