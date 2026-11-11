#!/usr/bin/env python3
"""Build a deterministic manager source bundle without native binaries or local data."""
import argparse
import gzip
import hashlib
import io
import time
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]

def _default_version():
    """Read the pinned version from the bootstrap so it never goes stale."""
    import re
    text = (ROOT / 'install-frpc.sh').read_text(encoding='utf-8')
    match = re.search(r"FRP_MANAGER_VERSION='([^']+)'", text)
    if not match: raise ValueError('Cannot determine version from install-frpc.sh')
    return match.group(1)

DEFAULT_VERSION = _default_version()

# Files that must exist in every release bundle (shared contract with
# manager.upgrade().manager_files; keep both in sync).
RUNTIME_FILES = (
    'install-common.sh', 'install-frps.sh', 'install-frpc.sh',
    'install-frps.ps1', 'install-frpc.ps1', 'install-windows.ps1',
    'uninstall.sh', 'uninstall-frps.ps1', 'uninstall-frpc.ps1', 'uninstall-windows.ps1',
    'lib/manager.py', 'lib/download.sh',
)
RUNTIME_DIRS = ('bin/', 'templates/')

def _is_runtime(name):
    return name in RUNTIME_FILES or name.startswith(RUNTIME_DIRS)

# (file, pattern-prefix, closing-quote) for every place the manager release
# version is pinned. Missing markers are tolerated (file may not carry it).
_VERSION_MARKERS = (
    ('lib/manager.py', b"MANAGER_RELEASE_VERSION = '", b"'"),
    ('lib/download.sh', b"FRP_MANAGER_VERSION='", b"'"),
    ('install-frpc.sh', b"FRP_MANAGER_VERSION='", b"'"),
    ('install-frps.sh', b"FRP_MANAGER_VERSION='", b"'"),
    ('install-frpc.ps1', b"$ManagerVersion='", b"'"),
    ('install-frps.ps1', b"$ManagerVersion='", b"'"),
    ('install-frpc.ps1', b"frp-simple-bash/v", b"'"),
    ('install-frps.ps1', b"frp-simple-bash/v", b"'"),
    ('install-windows.ps1', b"$ManagerVersion = '", b"'"),
    ('install-windows.ps1', b"frp-simple-bash/v", b"'"),
)

def _inject_version(name, data, version):
    """Pin every embedded manager version marker to the built version."""
    fresh = version.encode('ascii')
    for target, prefix, quote in _VERSION_MARKERS:
        if target != name: continue
        start = 0
        while True:
            idx = data.find(prefix, start)
            if idx < 0: break
            body = idx + len(prefix)
            end = data.find(quote, body)
            if end < 0: break
            data = data[:body] + fresh + data[end:]
            start = body + len(fresh) + len(quote)
    if name == 'lib/manager.py' and b"MANAGER_RELEASE_VERSION = '" not in data:
        raise ValueError('Could not inject MANAGER_RELEASE_VERSION into manager.py')
    return data

def build(version=DEFAULT_VERSION):
    if not version.startswith('v') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in version):
        raise ValueError('Invalid release version')
    output = ROOT / 'dist' / version
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'source.tar.gz'
    if archive.exists(): raise ValueError('Release output already exists; use a new version or explicitly remove an unpublished build')
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT, capture_output=True, check=True)
    names = sorted(set(filter(None, result.stdout.decode('utf-8').split('\0'))))
    files = []
    for name in names:
        # Allow-list only: internal docs/tests/tools/CI must never ship.
        if not _is_runtime(name): continue
        path = ROOT / name
        if not path.is_file() or path.is_symlink(): raise ValueError('Expected a regular source file: ' + name)
        if not str(path.resolve()).startswith(str(ROOT.resolve()) + '/'): raise ValueError('Source escapes repository')
        files.append((name, path))
    missing = [n for n in RUNTIME_FILES if n not in {name for name, _ in files}]
    if missing: raise ValueError('Source bundle is missing runtime components: ' + ', '.join(missing))
    for prefix in RUNTIME_DIRS:
        if not any(n.startswith(prefix) for n, _ in files):
            raise ValueError('Source bundle has no files under ' + prefix)
    with archive.open('xb') as raw:
        # Fixed mtime keeps the bundle reproducible (same input -> same sha256).
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as tar:
                for name, path in files:
                    data=path.read_bytes()
                    data = _inject_version(name, data, version)
                    entry=tarfile.TarInfo('frp-simple-bash/' + name)
                    entry.size=len(data); entry.mtime=0; entry.uid=entry.gid=0
                    entry.mode=0o755 if path.stat().st_mode & 0o111 else 0o644
                    tar.addfile(entry, io.BytesIO(data))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    (output/'SHA256SUMS.txt').write_text(digest+'  source.tar.gz\n',encoding='ascii')
    print(f'{archive}\nSHA256 {digest}\nFiles {len(files)}')
    return archive

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default=DEFAULT_VERSION)
    build(parser.parse_args().version)
