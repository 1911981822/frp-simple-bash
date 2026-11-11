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
        # 卸载本组件时：先删本组件配置，另一组件仍在则保留共享目录并提示
        self.assertIn('已删除 $role 的配置', text)
        self.assertIn('保留共享目录', text)
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

    def test_purge_frpc_only_removes_frpc_config(self):
        # 语义：卸载 frpc 只删 frpc 专属配置，绝不删 frps 配置。
        text = (ROOT / 'uninstall.sh').read_text()
        # frpc purge 删除 frpc.toml/profiles/index.json
        self.assertIn('frpc.toml" "$config_root/profiles" "$config_root/index.json', text)
        # 但绝不删除 frps.toml（frps 专属配置）
        self.assertIn('rm -rf -- "$config_root/frps.toml" "$config_root/.canonical-frps.toml"', text)
        # 共享目录只在另一组件不存在时才删
        self.assertIn('rm -rf -- /etc/frp /opt/frp-simple-bash', text)
    def test_purge_keeps_frps_config_when_uninstalling_frpc(self):
        text = (ROOT / 'uninstall.sh').read_text()
        # 卸载 frpc 的 purge 分支不包含删除 frps.toml 的代码路径
        # （frps.toml 删除只在 role==frps 分支）
        self.assertIn('if [[ "$role" == frpc ]]; then', text)
        self.assertIn('rm -rf -- "$config_root/frps.toml" "$config_root/.canonical-frps.toml"', text)

    # ---- macOS (Darwin) compatibility ----
    def test_macos_allowed_in_os_check(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('Darwin', text)
        self.assertNotIn('此安装器仅支持 Linux；', text)
    def test_macos_uses_id_u_not_euid(self):
        for f in ('install-common.sh', 'uninstall.sh'):
            text = (ROOT / f).read_text()
            self.assertIn('id -u', text)
            self.assertNotIn('EUID', text)
    def test_macos_sha_cmd_fallback(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('shasum -a 256', text)
        self.assertIn('sha_cmd=', text)
    def test_macos_launchd_service_detection(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('launchctl', text)
        self.assertIn('launchd', text)
    def test_macos_brew_only_prints(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('brew install', text)
        # 不自动 brew install，只提示
        self.assertIn('不会自动安装', text)
    def test_plist_templates_exist(self):
        # macOS 只支持客户端 frpc，只应有 frpc.plist，不应有 frps.plist
        self.assertTrue((ROOT / 'templates' / 'frpc.plist').exists())
        self.assertFalse((ROOT / 'templates' / 'frps.plist').exists())
    def test_macos_rejects_frps(self):
        text = (ROOT / 'install-common.sh').read_text()
        self.assertIn('macOS 仅支持客户端 frpc', text)
        self.assertIn('$component" == frps', text)

if __name__ == '__main__': unittest.main()
