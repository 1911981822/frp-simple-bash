#!/usr/bin/env bash
# Installs native binaries separately from the management commands.
set -Eeuo pipefail
component="${1:?需要 frps 或 frpc}"
[[ "$component" == frps || "$component" == frpc ]] || exit 2
shift
version='0.72.0'; install_root='/opt/frp-simple-bash'; config_root='/etc/frp'; archive_path=''; mirror=''; yes=0; start=1; autostart=1; non_interactive=0
usage() { cat <<'EOF'
用法: install-frps.sh|install-frpc.sh [选项]
  --version VERSION       版本（当前仅支持已核验的 0.72.0）
  --install-dir DIR       安装目录（默认 /opt/frp-simple-bash）
  --config-dir DIR        配置目录（默认 /etc/frp）
  --archive-path FILE     使用本地已下载包并校验 SHA256
  --mirror URL            使用 HTTPS 国内镜像
  --yes                   自动确认依赖和危险操作
  --no-start              注册服务但不启动
  --no-autostart          不设置开机自启动
  --non-interactive       缺少配置时停止，不进入向导
  --list-versions         列出已核验版本
  --help                  显示帮助
EOF
}
while (($#)); do case "$1" in
  --version) version="${2:?--version 需要值}"; shift 2;;
  --install-dir) install_root="${2:?--install-dir 需要值}"; shift 2;;
  --config-dir) config_root="${2:?--config-dir 需要值}"; shift 2;;
  --archive-path|--local-archive) archive_path="${2:?--archive-path 需要值}"; shift 2;;
  --mirror) mirror="${2:?--mirror 需要值}"; shift 2;;
  --yes) yes=1; shift;;
  --no-start) start=0; shift;;
  --no-autostart) autostart=0; shift;;
  --non-interactive) non_interactive=1; shift;;
  --list-versions) printf '0.72.0\n'; exit 0;;
  --help|-h) usage; exit 0;;
  *) echo "未知参数：$1" >&2; usage >&2; exit 2;;
esac; done
[[ "$version" == '0.72.0' ]] || { echo "版本 $version 尚未完成配置与 SHA256 验证，拒绝安装/升级。已核验：0.72.0" >&2; exit 1; }
if [[ "$install_root" != /opt/frp-simple-bash || "$config_root" != /etc/frp ]]; then
  echo '自定义安装/配置目录当前尚未接入 systemd、命令入口和 manager 环境，请使用默认目录 /opt/frp-simple-bash 与 /etc/frp。' >&2
  exit 2
fi
os_name="$(uname -s)"
[[ "$os_name" == Linux || "$os_name" == Darwin ]] || { echo '此安装器仅支持 Linux 与 macOS；Windows 请使用 PowerShell 安装器。' >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || { echo '请使用 root/sudo 安装。' >&2; exit 1; }
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$source_root/lib/download.sh"
[[ -n "$mirror" ]] && export FRP_MIRROR_URL="$mirror"
[[ "$install_root" = /* && "$config_root" = /* ]] || { echo '安装目录和配置目录必须是绝对路径。' >&2; exit 2; }
if [[ "$install_root" != /opt/frp-simple-bash && "$install_root" == /opt ]]; then echo '拒绝使用系统根目录作为安装目录。' >&2; exit 2; fi
if [[ "$config_root" != /etc/frp && "$config_root" == /etc ]]; then echo '拒绝使用系统根目录作为配置目录。' >&2; exit 2; fi
[[ "$install_root" != / && "$config_root" != / ]] || { echo '拒绝使用 / 作为目录。' >&2; exit 2; }
for role in frps frpc; do
  if [[ -e "/usr/local/bin/$role" && ! -L "/usr/local/bin/$role" ]]; then
    echo "已有 /usr/local/bin/$role，请先手动迁移，未修改现有安装。" >&2; exit 1
  fi
  if [[ -L "/usr/local/bin/$role" && "$(readlink "/usr/local/bin/$role")" != "$install_root/bin/$role" ]]; then
    echo "已有非本项目命令链接 /usr/local/bin/$role，未覆盖。" >&2; exit 1
  fi
done
version='0.72.0'
case "$(uname -m)" in
  x86_64|amd64) arch=amd64; expected=daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804 ;;
  aarch64|arm64) arch=arm64; expected=02180b857480302fd934e9ddbd50fe2f73229f99062f88756abc6de9df5e95cf ;;
  *) echo '首期只支持 amd64/arm64。' >&2; exit 1 ;;
esac
missing=()
for tool in curl tar python3; do command -v "$tool" >/dev/null || missing+=("$tool"); done
# SHA256 checker differs by platform: Linux has sha256sum, macOS has shasum.
if command -v sha256sum >/dev/null; then sha_cmd='sha256sum'
elif command -v shasum >/dev/null; then sha_cmd='shasum'
else missing+=(sha256sum); sha_cmd=''
fi

# Detect the package manager and give each family its own non-interactive,
# lock-aware invocation. Ubuntu/Debian needs DEBIAN_FRONTEND=noninteractive,
# Fedora/RHEL uses dnf, CentOS 7 uses yum, Alpine uses apk.
pkg_manager=''
if [[ "$os_name" == Darwin ]]; then
  # macOS: Homebrew is the standard package manager but installing via brew
  # interactively is non-deterministic; we only ever PRINT the command.
  command -v brew >/dev/null 2>&1 && pkg_manager=brew
else
  for candidate in apt-get dnf yum apk; do
    if command -v "$candidate" >/dev/null 2>&1; then pkg_manager=$candidate; break; fi
  done
fi

pkg_names() {
  # Map logical names to the distro-specific packages (ss lives in iproute2).
  local out=()
  for name in "$@"; do
    case "$name" in
      sha256sum) out+=(coreutils);;
      ss) if [[ "$pkg_manager" == apk ]]; then out+=(iproute2); else out+=(iproute2); fi;;
      *) out+=("$name");;
    esac
  done
  printf '%s ' "${out[@]}"
}

check_dpkg_lock() {
  # A running unattended-upgrade holds the lock; fail with an actionable hint.
  [[ "$pkg_manager" == apt-get ]] || return 0
  local locks=(/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock)
  for lock in "${locks[@]}"; do
    if [[ -e "$lock" ]] && command -v fuser >/dev/null 2>&1 && fuser "$lock" >/dev/null 2>&1; then
      echo "apt 正被其他进程占用（常见于 Ubuntu/Debian 的自动更新）。" >&2
      echo "请等待其完成后重试，或手动执行：" >&2
      echo "  sudo systemctl stop unattended-upgrades; sudo apt-get install -y $(pkg_names "$@")" >&2
      return 1
    fi
  done
  return 0
}

run_pkg_install() {
  local packages
  packages=$(pkg_names "$@")
  case "$pkg_manager" in
    apt-get)
      check_dpkg_lock "$@" || return 1
      DEBIAN_FRONTEND=noninteractive apt-get update && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y $packages
      ;;
    dnf) dnf install -y $packages ;;
    yum) yum install -y $packages ;;
    apk) apk add --no-cache $packages ;;
    *) return 1 ;;
  esac
}

if ((${#missing[@]})); then
  # macOS: never auto-install via brew (interactive, non-deterministic). Print
  # the exact commands and exit so the user runs them deliberately.
  if [[ "$os_name" == Darwin ]]; then
    echo '缺少依赖：'"${missing[*]}"'。macOS 不会自动安装，请手动执行：' >&2
    for dep in "${missing[@]}"; do
      echo "  brew install $dep" >&2
    done
    echo '  （coreutils 提供 sha256sum；或已有 shasum 则无需额外安装）' >&2
    exit 1
  fi
  printf '缺少依赖：%s。将通过系统包管理器安装：%s\n' "${missing[*]}" "$(pkg_names "${missing[@]}")"
  if [[ -z "$pkg_manager" ]]; then
    echo '无法识别包管理器，请手动安装：' >&2
    echo "  curl tar coreutils python3  # 对应发行版的包名可能不同" >&2
    exit 1
  fi
  if ((yes == 0)); then read -r -p '确认安装依赖？[y/N] ' answer; [[ "$answer" =~ ^[Yy]$ ]] || exit 1; fi
  if ! run_pkg_install "${missing[@]}"; then
    echo '依赖安装失败。请手动执行（按你的发行版选择）：' >&2
    echo "  Debian/Ubuntu:  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y $(pkg_names "${missing[@]}")" >&2
    echo "  Fedora/RHEL:    sudo dnf install -y $(pkg_names "${missing[@]}")" >&2
    echo "  CentOS 7:       sudo yum install -y $(pkg_names "${missing[@]}")" >&2
    echo "  Alpine:         sudo apk add $(pkg_names "${missing[@]}")" >&2
    exit 1
  fi
fi
# Python 3.6+ is enough: the manager carries its own TOML fallback parser.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,6) else 1)' 2>/dev/null; then
  echo "检测到 $(python3 -V 2>&1 || echo '未安装 python3')，本项目需要 Python 3.6 及以上。"
  if [[ -z "$pkg_manager" ]]; then echo '请先安装 Python 3.6+（未找到包管理器）。' >&2; exit 1; fi
  if ((yes == 0)); then read -r -p '确认通过包管理器安装 python3？[y/N] ' answer; [[ "$answer" =~ ^[Yy]$ ]] || exit 1; fi
  if ! run_pkg_install python3; then
    echo 'Python 安装失败。请手动执行：' >&2
    echo "  Debian/Ubuntu: sudo apt-get install -y python3" >&2
    echo "  Fedora/RHEL:   sudo dnf install -y python3" >&2
    echo "  CentOS 7:      sudo yum install -y python3" >&2
    echo "  Alpine:        sudo apk add python3" >&2
    exit 1
  fi
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,6) else 1)' || { echo 'Python 版本仍不满足 3.6+。' >&2; exit 1; }
python3 -c 'import json,hashlib,ipaddress,urllib.request' || { echo 'Python 标准库不完整。' >&2; exit 1; }
# ss/iproute2 只用于端口占用检测：缺失时降级为「无法检测」而不是阻断安装。
if ! command -v ss >/dev/null 2>&1; then
  echo '提示：未找到 ss（iproute2），端口占用检测将不可用；如需该功能请安装：' >&2
  echo "  Debian/Ubuntu: sudo apt-get install -y iproute2" >&2
  echo "  Fedora/RHEL:   sudo dnf install -y iproute" >&2
  echo "  CentOS 7:      sudo yum install -y iproute" >&2
  echo "  Alpine:        sudo apk add iproute2" >&2
fi
manager=none
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then manager=systemd
elif command -v rc-service >/dev/null && command -v rc-update >/dev/null; then manager=openrc
elif [[ "$os_name" == Darwin ]] && command -v launchctl >/dev/null; then manager=launchd
else echo '未检测到 systemd/OpenRC/launchd；不能完成后台与自启动安装。' >&2; exit 1; fi
work="$(mktemp -d)"
trap '[[ -n "${work:-}" && -d "$work" ]] && rm -rf -- "$work"' EXIT
asset="frp_${version}_linux_${arch}.tar.gz"
if [[ -n "$archive_path" ]]; then
  [[ -f "$archive_path" ]] || { echo '本地 frp 安装包不存在。' >&2; exit 1; }
  cp -- "$archive_path" "$work/$asset"
else
  case "$arch" in amd64) asset_id=710291514 ;; arm64) asset_id=710291691 ;; esac
  frp_download_asset "$work/$asset" "$expected" "frp/v${version}/$asset" "https://github.com/fatedier/frp/releases/download/v${version}/${asset}" "https://api.github.com/repos/fatedier/frp/releases/assets/$asset_id"
fi
if [[ -z "$sha_cmd" ]]; then
  echo '缺少 sha256sum/shasum，无法校验下载包。' >&2; exit 1
fi
if [[ "$sha_cmd" == sha256sum ]]; then
  printf '%s  %s\n' "$expected" "$work/$asset" | sha256sum -c - >/dev/null || { echo 'frp 安装包 SHA256 校验失败，未执行二进制、未修改现有服务。' >&2; exit 1; }
else
  actual="$(shasum -a 256 "$work/$asset" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || { echo 'frp 安装包 SHA256 校验失败，未执行二进制、未修改现有服务。' >&2; exit 1; }
fi
tar -xzf "$work/$asset" -C "$work" 2>/dev/null || tar -xzf "$work/$asset" -C "$work"
"$work/frp_${version}_linux_${arch}/$component" --version
# Pre-flight checks that must NOT stop a running service before failing.
for role in frps frpc; do
  if [[ -e "/usr/local/bin/$role" && ! -L "/usr/local/bin/$role" ]]; then
    echo "已有 /usr/local/bin/$role，请先手动迁移，未修改现有安装。" >&2; exit 1
  fi
  if [[ -L "/usr/local/bin/$role" && "$(readlink "/usr/local/bin/$role")" != "$install_root/bin/$role" ]]; then
    echo "已有非本项目命令链接 /usr/local/bin/$role，未覆盖。" >&2; exit 1
  fi
done
mkdir -p "$install_root/native" "$install_root/bin" "$install_root/lib" "$install_root/templates" "$config_root" /usr/local/bin
chmod 700 "$config_root"
# Snapshot every file/state we are about to touch, so a failed install can be
# fully rolled back. EXIT trap (not ERR) also covers explicit `exit N`.
rollback_dir="$work/rollback"
mkdir -p "$rollback_dir"
snapshot_file() {
  local src="$1" rel="$2"
  if [[ -e "$src" || -L "$src" ]]; then
    mkdir -p "$(dirname "$rollback_dir/$rel")"
    cp -a -- "$src" "$rollback_dir/$rel"
  fi
}
snapshot_file "$install_root/native/$component" "native/$component"
for rel in bin/frps bin/frpc lib/manager.py lib/download.sh install-common.sh install-frps.sh install-frpc.sh uninstall.sh; do
  snapshot_file "$install_root/$rel" "$rel"
done
for rel in templates/frps.service templates/frpc.service templates/frps.openrc templates/frpc.openrc; do
  snapshot_file "$install_root/$rel" "$rel"
done
if [[ "$manager" == systemd ]]; then snapshot_file "/etc/systemd/system/$component.service" "unit/$component.service"; fi
if [[ "$manager" == openrc ]]; then snapshot_file "/etc/init.d/$component" "unit/$component.openrc"; fi
was_running=0
install_ok=0
rollback_install() {
  local exit_code=$?
  trap - EXIT
  if ((install_ok == 0)); then
    for rel in native/$component bin/frps bin/frpc lib/manager.py lib/download.sh \
               install-common.sh install-frps.sh install-frpc.sh uninstall.sh \
               templates/frps.service templates/frpc.service templates/frps.openrc templates/frpc.openrc; do
      local saved="$rollback_dir/$rel" dest="$install_root/$rel"
      if [[ -e "$saved" || -L "$saved" ]]; then
        mkdir -p "$(dirname "$dest")" || true; cp -a -- "$saved" "$dest" || true
      fi
    done
    if [[ "$manager" == systemd && -f "$rollback_dir/unit/$component.service" ]]; then
      mkdir -p /etc/systemd/system || true; cp -a -- "$rollback_dir/unit/$component.service" "/etc/systemd/system/$component.service" || true
    elif [[ "$manager" == openrc && -f "$rollback_dir/unit/$component.openrc" ]]; then
      mkdir -p /etc/init.d || true; cp -a -- "$rollback_dir/unit/$component.openrc" "/etc/init.d/$component" || true
    fi
    if ((was_running == 1)); then
      if [[ "$manager" == systemd ]]; then systemctl start "$component" || true; else rc-service "$component" start || true; fi
    fi
    echo '安装失败，已尝试恢复旧文件及先前运行状态；配置已保留。' >&2
  fi
  exit "$exit_code"
}
trap rollback_install EXIT
if [[ "$manager" == systemd ]] && systemctl is-active --quiet "$component"; then was_running=1; systemctl stop "$component"; fi
if [[ "$manager" == openrc ]] && rc-service "$component" status >/dev/null 2>&1; then was_running=1; rc-service "$component" stop; fi
# Stage then atomically replace the executable (avoids ETXTBSY).
install -m 755 "$work/frp_${version}_linux_${arch}/$component" "$install_root/native/$component.new"
mv -f "$install_root/native/$component.new" "$install_root/native/$component"
if [[ "$source_root" != "$install_root" ]]; then
  for file in frps frpc; do install -m 755 "$source_root/bin/$file" "$install_root/bin/$file"; done
  install -m 644 "$source_root/lib/manager.py" "$install_root/lib/manager.py"
  install -m 644 "$source_root/lib/download.sh" "$install_root/lib/download.sh"
  for file in install-common.sh install-frps.sh install-frpc.sh uninstall.sh; do install -m 755 "$source_root/$file" "$install_root/$file"; done
  cp "$source_root/templates/"* "$install_root/templates/"
fi
# Only the component being installed gets its command symlink. The other
# component's bin/ entry exists (shared menu shell) but is NOT exposed as a
# command, so users cannot invoke a not-installed component by mistake.
ln -sfn "$install_root/bin/$component" "/usr/local/bin/$component"
if [[ "$manager" == systemd ]]; then
  install -m 644 "$source_root/templates/$component.service" "/etc/systemd/system/$component.service"
  systemctl daemon-reload
  if ((autostart == 1)); then systemctl enable "$component"; else systemctl disable "$component" || true; fi
elif [[ "$manager" == openrc ]]; then
  install -m 755 "$source_root/templates/$component.openrc" "/etc/init.d/$component"
  if ((autostart == 1)); then rc-update add "$component" default; else rc-update del "$component" default || true; fi
else
  # launchd (macOS): plist in /Library/LaunchDaemons, label frp-simple-bash.<role>.
  plist_label="frp-simple-bash.$component"
  plist_path="/Library/LaunchDaemons/$plist_label.plist"
  install -m 644 "$source_root/templates/$component.plist" "$plist_path"
  # Boot policy: `enable` persists auto-start; `disable` turns it off so the
  # RunAtLoad plist does NOT restart at next boot. load/unload would only affect
  # the current session and be undone by a reboot.
  if ((autostart == 1)); then launchctl enable "system/$plist_label"; else launchctl disable "system/$plist_label"; fi
fi
if [[ ! -f "$config_root/$component.toml" ]]; then
  if ((non_interactive == 1)); then echo "配置不存在，--non-interactive 不进入配置向导：$config_root/$component.toml" >&2; exit 2; fi
  FRP_CONFIG_DIR="$config_root" FRP_BINARY_DIR="$install_root/native" python3 "$install_root/lib/manager.py" "$component" configure
fi
if [[ -f "$config_root/$component.toml" ]]; then
  if "$install_root/native/$component" verify -c "$config_root/$component.toml" >/dev/null 2>&1; then
    if ((start == 1)); then
      if [[ "$manager" == systemd ]]; then systemctl start "$component"
      elif [[ "$manager" == openrc ]]; then rc-service "$component" start
      else launchctl kickstart "system/frp-simple-bash.$component"; fi
    else
      echo '服务已注册但按 --no-start 保持停止。'
    fi
  else
    echo '配置校验失败，服务未启动。' >&2
    false
  fi
else echo '配置尚未创建，服务已注册但保持停止。'; fi
install_ok=1
trap - EXIT
printf '安装完成。输入 %s 打开数字菜单。防火墙只检测提示，请自行检查业务端口和映射端口规则。\n' "$component"
