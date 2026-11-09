import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]

class InstallerInterfaceTests(unittest.TestCase):
    def run_script(self, args):
        return subprocess.run(['bash', str(ROOT / 'install-common.sh'), 'frps', *args], capture_output=True, text=True, env=dict(os.environ, FRP_MIRROR_URL='off'))
    def test_help_does_not_install(self):
        result = self.run_script(['--help'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--version', result.stdout)
        self.assertIn('--non-interactive', result.stdout)
    def test_list_versions_is_explicitly_pinned(self):
        result = self.run_script(['--list-versions'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '0.72.0')
    def test_unknown_option_stops_before_system_change(self):
        result = self.run_script(['--not-a-real-option'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未知参数', result.stderr + result.stdout)
    def test_uninstall_help_is_non_destructive(self):
        result = subprocess.run(['bash', str(ROOT / 'uninstall.sh'), 'frps', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--purge', result.stdout)

if __name__ == '__main__': unittest.main()
