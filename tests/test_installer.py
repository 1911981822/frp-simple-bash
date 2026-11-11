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
    def test_no_start_flag_is_consumed(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('--no-start) start=0', text)
        self.assertIn('((start == 1))', text)  # actually consulted before starting
    def test_exit_trap_covers_explicit_exits(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('trap rollback_install EXIT', text)
        self.assertIn('install_ok=1', text)
        self.assertIn('trap - EXIT', text)
        self.assertNotIn('trap rollback_install ERR', text)
    def test_enable_failure_not_swallowed(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('if ((autostart == 1)); then systemctl enable', text)
        self.assertNotIn('&& systemctl enable "$component" || systemctl disable', text)
    def test_purge_checks_openrc_and_bin(self):
        text = (ROOT / 'uninstall.sh').read_text()
        # OpenRC 安装痕迹必须纳入判据
        self.assertIn('/etc/init.d/$other', text)
        # 但 bin/{frps,frpc} 是组件共享入口，绝不能作为「另一组件仍安装」的判据
        self.assertNotIn('/opt/frp-simple-bash/bin/$other', text)
    def test_rollback_snapshots_manager_and_unit(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('snapshot_file "$install_root/$rel" "$rel"', text)
        self.assertIn('lib/manager.py', text)
        self.assertIn('snapshot_file "/etc/systemd/system/$component.service"', text)
    def test_preflight_checks_before_stopping_service(self):
        text = (ROOT / 'install-common.sh').read_text()
        preflight = text.index('Pre-flight checks')
        stop = text.index('was_running=1; systemctl stop')
        self.assertLess(preflight, stop)

    # ---- cross-distro dependency installation ----
    def test_dependency_install_is_noninteractive_on_debian(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('DEBIAN_FRONTEND=noninteractive apt-get', text)
    def test_every_supported_package_manager_is_handled(self):
        text = (ROOT / 'install-common.sh').read_text()
        for manager in ('apt-get', 'dnf', 'yum', 'apk'):
            self.assertIn(manager, text)
        self.assertIn('apk add --no-cache', text)
    def test_dpkg_lock_conflict_gives_actionable_hint(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('check_dpkg_lock', text)
        self.assertIn('lock-frontend', text)
        self.assertIn('unattended-upgrades', text)
    def test_failed_install_prints_manual_commands_per_distro(self):
        text = (ROOT / 'install-common.sh').read_text()
        for hint in ('Debian/Ubuntu:', 'Fedora/RHEL:', 'CentOS 7:', 'Alpine:'):
            self.assertIn(hint, text)
    def test_sha256sum_maps_to_coreutils(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('sha256sum) out+=(coreutils)', text)
    def test_missing_ss_is_a_warning_not_a_blocker(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('端口占用检测将不可用', text)
        self.assertIn('iproute2', text)

    def test_purge_ignores_shared_bin_entries(self):
        text = (ROOT / 'uninstall.sh').read_text()
        self.assertNotIn('"/opt/frp-simple-bash/bin/$other"', text)
        self.assertIn('other_traces', text)
        for real_marker in ('/usr/local/bin/$other', '/opt/frp-simple-bash/native/$other',
                            '/etc/systemd/system/$other.service', '/etc/init.d/$other'):
            self.assertIn(real_marker, text)
    def test_purge_reports_which_traces_block_it(self):
        text = (ROOT / 'uninstall.sh').read_text()
        self.assertIn('另一个组件 $other 仍安装', text)
        self.assertIn('发现残留', text)
        self.assertIn('uninstall.sh $other', text)
    def test_purge_success_message_present(self):
        text = (ROOT / 'uninstall.sh').read_text()
        self.assertIn('彻底清理完成', text)

    def test_install_only_links_current_component(self):
        text = (ROOT / 'install-common.sh').read_text()
        # 建链接处只建当前组件，不再无条件建两个（预检查的 for role 循环保留）
        self.assertIn('ln -sfn "$install_root/bin/$component"', text)
        self.assertNotIn('ln -sfn "$install_root/bin/$role" "/usr/local/bin/$role"', text)
    def test_uninstall_noise_reduced_for_missing_unit(self):
        text = (ROOT / 'uninstall.sh').read_text()
        # systemctl stop/disable 只在 unit 存在时执行，且 stderr 静默
        self.assertIn('if [[ -f "$unit" ]]; then', text)
        self.assertIn('systemctl stop "$role" >/dev/null 2>&1 || true', text)
        self.assertIn('systemctl disable "$role" >/dev/null 2>&1 || true', text)
    def test_purge_still_checks_other_component(self):
        text = (ROOT / 'uninstall.sh').read_text()
        # purge 仍检查另一组件的真实痕迹
        for marker in ('/usr/local/bin/$other', 'native/$other', '$other.service', 'init.d/$other'):
            self.assertIn(marker, text)

if __name__ == '__main__': unittest.main()
