"""Offline download tests. Mock curl only; real tar and SHA256 tools verify fixtures."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(shutil.which('bash') and shutil.which('sha256sum'), 'Bash and sha256sum required')
class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.log = self.root / 'requests.jsonl'; self.fixtures = {}
        mock = self.bin / 'curl'
        mock.write_text('''#!/usr/bin/env python3
import json,os,sys,shutil
args=sys.argv[1:]; url=next(x for x in args if x.startswith('https://')); dest=args[args.index('-o')+1]
with open(os.environ['MOCK_LOG'],'a') as f: f.write(json.dumps(url)+'\\n')
fixtures=json.loads(os.environ['MOCK_FIXTURES'])
if url not in fixtures:
    open(dest,'w').write('partial-transfer-must-not-run')
    sys.exit(22)
shutil.copyfile(fixtures[url],dest)
''')
        mock.chmod(0o755)
    def tearDown(self): self.tmp.cleanup()
    def env(self, mirror='https://mirror.invalid'):
        return dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'], FRP_MIRROR_URL=mirror,
                    MOCK_LOG=str(self.log), MOCK_FIXTURES=json.dumps(self.fixtures), TMPDIR=str(self.root))
    def requests(self): return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
    def file(self, name, content):
        p=self.root/name; p.write_bytes(content); return str(p)
    def asset_call(self, checksum):
        return subprocess.run(['bash','-c', 'source "$1/lib/download.sh"; frp_download_asset "$2" "$3" frp/v0.72.0/test.tar.gz https://official.invalid/test.tar.gz', '_',str(ROOT),str(self.root/'asset'),checksum], env=self.env(),capture_output=True,text=True)
    def test_mirror_preferred(self):
        content=b'good'; checksum=hashlib.sha256(content).hexdigest()
        self.fixtures['https://mirror.invalid/frp/v0.72.0/test.tar.gz?mirror=v0.1.3-feedback.1']=self.file('good',content)
        r=self.asset_call(checksum); self.assertEqual(r.returncode,0,r.stderr); self.assertEqual(len(self.requests()),1)
        self.assertTrue(self.requests()[0].endswith('?mirror=v0.1.3-feedback.1'))
    def test_unavailable_mirror_falls_back(self):
        content=b'official'; self.fixtures['https://official.invalid/test.tar.gz']=self.file('official',content)
        r=self.asset_call(hashlib.sha256(content).hexdigest()); self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(len(self.requests()),2); self.assertEqual((self.root/'asset').read_bytes(),content)
    def test_checksum_mismatch_stops_without_fallback(self):
        self.fixtures['https://mirror.invalid/frp/v0.72.0/test.tar.gz?mirror=v0.1.3-feedback.1']=self.file('bad',b'tampered')
        r=self.asset_call('0'*64); self.assertNotEqual(r.returncode,0); self.assertEqual(len(self.requests()),1)
    def make_bundle(self, layout):
        p=self.root/('bundle-'+layout+'.tar.gz')
        with tarfile.open(p,'w:gz') as tar:
            for relative,data in {'install-common.sh':b'#!/bin/bash\nprintf "role=%s input=" "$1"\nread -r answer\nprintf "%s" "$answer"\n', 'lib/download.sh':b'# fixture', 'lib/manager.py':b'# fixture'}.items():
                item=tarfile.TarInfo(layout+'/'+relative); item.size=len(data); tar.addfile(item,io.BytesIO(data))
        return p
    def bootstrap(self, role='frps', mirror='https://mirror.invalid'):
        target=self.root/('install-'+role+'.sh'); target.write_bytes((ROOT/target.name).read_bytes())
        return subprocess.run(['bash',str(target)],input='interactive-answer\n',env=self.env(mirror),capture_output=True,text=True)
    def test_mirror_bundle_layout_and_terminal_input(self):
        bundle=self.make_bundle('frp-simple-bash'); base='https://mirror.invalid/frp-simple-bash/releases/v0.1.3-feedback.1/'
        self.fixtures[base+'source.tar.gz']=str(bundle)
        self.fixtures[base+'SHA256SUMS.txt']=self.file('manifest',(hashlib.sha256(bundle.read_bytes()).hexdigest()+'  source.tar.gz\n').encode())
        result=self.bootstrap(); self.assertEqual(result.returncode,0,result.stderr); self.assertIn('role=frps input=interactive-answer',result.stdout)
        self.assertEqual(len(self.requests()),2)
    def test_official_bundle_layout_when_mirror_unavailable(self):
        bundle=self.make_bundle('frp-simple-bash-main')
        self.fixtures['https://codeload.github.com/1911981822/frp-simple-bash/tar.gz/refs/heads/main']=str(bundle)
        result=self.bootstrap('frpc'); self.assertEqual(result.returncode,0,result.stderr); self.assertIn('role=frpc input=interactive-answer',result.stdout)
    def test_empty_and_off_disable_mirror(self):
        bundle=self.make_bundle('frp-simple-bash-main')
        self.fixtures['https://codeload.github.com/1911981822/frp-simple-bash/tar.gz/refs/heads/main']=str(bundle)
        for value in ('','off'):
            if self.log.exists(): self.log.unlink()
            result=self.bootstrap(mirror=value); self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(self.requests()),1); self.assertIn('codeload.github.com',self.requests()[0])
    def test_bad_manifest_never_executes_or_falls_back(self):
        bundle=self.make_bundle('frp-simple-bash'); base='https://mirror.invalid/frp-simple-bash/releases/v0.1.3-feedback.1/'
        self.fixtures[base+'source.tar.gz']=str(bundle); self.fixtures[base+'SHA256SUMS.txt']=self.file('manifest',('0'*64+'  source.tar.gz\n').encode())
        result=self.bootstrap(); self.assertNotEqual(result.returncode,0); self.assertNotIn('role=',result.stdout)
        self.assertEqual(len(self.requests()),2)
    def test_failed_partial_download_never_extracts(self):
        result=self.bootstrap(); self.assertNotEqual(result.returncode,0); self.assertNotIn('role=',result.stdout)
        self.assertFalse(any(p.is_dir() and p.name.startswith('tmp.') for p in self.root.iterdir()))
    def test_inline_bash_c_entry_keeps_interactive_input(self):
        bundle=self.make_bundle('frp-simple-bash-main')
        self.fixtures['https://codeload.github.com/1911981822/frp-simple-bash/tar.gz/refs/heads/main']=str(bundle)
        result=subprocess.run(['bash','-c',(ROOT/'install-frps.sh').read_text()],cwd=self.root,input='inline-answer\n',env=self.env('off'),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr); self.assertIn('role=frps input=inline-answer',result.stdout)
    def test_default_mirror_and_https_only(self):
        env=self.env(); env.pop('FRP_MIRROR_URL')
        result=subprocess.run(['bash','-c','source "$1/lib/download.sh"; frp_mirror_url','_',str(ROOT)],env=env,capture_output=True,text=True)
        self.assertEqual(result.stdout,'https://download.sueiyuan.top')
        env['FRP_MIRROR_URL']='http://unsafe.invalid'
        result=subprocess.run(['bash','-c','source "$1/lib/download.sh"; frp_mirror_url','_',str(ROOT)],env=env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
    def test_missing_manifest_stops_without_official_execution(self):
        bundle=self.make_bundle('frp-simple-bash'); base='https://mirror.invalid/frp-simple-bash/releases/v0.1.3-feedback.1/'
        self.fixtures[base+'source.tar.gz']=str(bundle)
        result=self.bootstrap(); self.assertNotEqual(result.returncode,0)
        self.assertEqual(len(self.requests()),2); self.assertNotIn('role=',result.stdout)
    def test_api_final_fallback_is_still_checked(self):
        self.fixtures['https://api.invalid/asset']=self.file('api',b'api-result')
        command='source "$1/lib/download.sh"; frp_download_asset "$2" "$3" asset https://official.invalid/asset https://api.invalid/asset'
        checksum=hashlib.sha256(b'api-result').hexdigest()
        result=subprocess.run(['bash','-c',command,'_',str(ROOT),str(self.root/'api-output'),checksum],env=self.env(),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr); self.assertEqual(len(self.requests()),3)
    def test_windows_pins_and_no_metadata_dependency(self):
        shared=(ROOT/'install-windows.ps1').read_text()
        self.assertIn('05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da',shared)
        self.assertNotIn('Invoke-RestMethod',shared)
        self.assertIn('SHA256 mismatch; no source fallback',shared)
        self.assertIn("$mirrorSource += '?mirror=' + $ManagerVersion",shared)
        for path in [ROOT/'install-windows.ps1',ROOT/'install-frps.ps1',ROOT/'install-frpc.ps1']:
            path.read_bytes().decode('ascii'); self.assertIn('v0.1.3-feedback.1',path.read_text())

if __name__=='__main__': unittest.main()
