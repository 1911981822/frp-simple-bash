#!/usr/bin/env python3
"""Build a deterministic manager source bundle without native binaries or local data."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VERSION = 'v0.1.1-menu.1'

def build(version=DEFAULT_VERSION):
    if not version.startswith('v') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in version):
        raise ValueError('Invalid release version')
    output = ROOT / 'dist' / version
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'source.tar.gz'
    if archive.exists(): raise ValueError('Release output already exists; use a new version or explicitly remove an unpublished build')
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT, capture_output=True, check=True)
    names = sorted(set(filter(None, result.stdout.decode('utf-8').split('\0'))))
    blocked = ('.git/', '.test-', '.gopath/', '.gocache/', 'dist/')
    files = []
    for name in names:
        path = ROOT / name
        if name.startswith(blocked) or '__pycache__' in path.parts: continue
        if not path.is_file() or path.is_symlink(): raise ValueError('Expected a regular source file: ' + name)
        if not path.resolve().is_relative_to(ROOT): raise ValueError('Source escapes repository')
        files.append((name, path))
    required = {'install-common.sh','lib/manager.py','install-windows.ps1','install-frps.sh','install-frpc.sh','lib/download.sh'}
    if not required.issubset({name for name, _ in files}): raise ValueError('Source bundle is missing runtime components')
    with archive.open('xb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as tar:
                for name, path in files:
                    data=path.read_bytes()
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
