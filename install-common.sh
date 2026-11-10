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
[[ "$(uname -s)" == Linux ]] || { echo '此安装器仅支持 Linux；Windows 请使用 PowerShell 安装器。' >&2; exit 1; }
[[ "${EUID}" == 0 ]] || { echo '请使用 root/sudo 安装。' >&2; exit 1; }
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
  *) echo '首期只支持 Linux amd64/arm64。' >&2; exit 1 ;;
esac
missing=()
for tool in curl tar sha256sum python3; do command -v "$tool" >/dev/null || missing+=("$tool"); done
if ((${#missing[@]})); then
  printf '缺少依赖：%s。将通过系统包管理器安装 curl tar coreutils python3。\n' "${missing[*]}"
  if ((yes == 0)); then read -r -p '确认安装依赖？[y/N] ' answer; [[ "$answer" =~ ^[Yy]$ ]] || exit 1; fi
  if command -v apt-get >/dev/null; then apt-get update; apt-get install -y curl tar coreutils python3
  elif command -v dnf >/dev/null; then dnf install -y curl tar coreutils python3
  elif command -v yum >/dev/null; then yum install -y curl tar coreutils python3
  elif command -v apk >/dev/null; then apk add curl tar coreutils python3
  else echo '无法识别包管理器，请手动安装。' >&2; exit 1; fi
fi
# Python 3.6+ is enough: the manager carries its own TOML fallback parser.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,6) else 1)'; then
  echo "检测到 $(python3 -V 2>&1)，本项目需要 Python 3.6 及以上。"
  if ((yes == 0)); then read -r -p '确认通过包管理器安装 python3？[y/N] ' answer; [[ "$answer" =~ ^[Yy]$ ]] || exit 1; fi
  if command -v apt-get >/dev/null; then apt-get update; apt-get install -y python3
  elif command -v dnf >/dev/null; then dnf install -y python3
  elif command -v yum >/dev/null; then yum install -y python3
  elif command -v apk >/dev/null; then apk add python3
  else echo '请先安装 Python 3.6+。' >&2; exit 1; fi
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,6) else 1)' || { echo 'Python 版本仍不满足 3.6+。' >&2; exit 1; }
python3 -c 'import json,hashlib,ipaddress,urllib.request' || { echo 'Python 标准库不完整。' >&2; exit 1; }
manager=none
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then manager=systemd
elif command -v rc-service >/dev/null && command -v rc-update >/dev/null; then manager=openrc
else echo '未检测到 systemd/OpenRC；不能完成后台与自启动安装。' >&2; exit 1; fi
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
if ! printf '%s  %s\n' "$expected" "$work/$asset" | sha256sum -c - >/dev/null; then
  echo 'frp 安装包 SHA256 校验失败，未执行二进制、未修改现有服务。' >&2
  exit 1
fi
tar -xzf "$work/$asset" -C "$work" 2>/dev/null || tar -xzf "$work/$asset" -C "$work"
"$work/frp_${version}_linux_${arch}/$component" --version
mkdir -p "$install_root/native" "$install_root/bin" "$install_root/lib" "$install_root/templates" "$config_root" /usr/local/bin
chmod 700 "$config_root"
# Preserve the previous binary for reinstall/upgrade rollback.
if [[ -f "$install_root/native/$component" ]]; then cp -p "$install_root/native/$component" "$work/previous"; fi
was_running=0
rollback_install() {
  local exit_code=$?
  trap - ERR
  if [[ -f "$work/previous" ]]; then
    install -m 755 "$work/previous" "$install_root/native/$component.restore" || true
    mv -f "$install_root/native/$component.restore" "$install_root/native/$component" || true
  fi
  if [[ "$was_running" == 1 ]]; then
    if [[ "$manager" == systemd ]]; then systemctl start "$component" || true; else rc-service "$component" start || true; fi
  fi
  echo '安装失败，已尝试恢复旧二进制及先前运行状态；配置已保留。' >&2
  exit "$exit_code"
}
trap rollback_install ERR
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
for role in frps frpc; do
  if [[ -e "/usr/local/bin/$role" && ! -L "/usr/local/bin/$role" ]]; then
    echo "已有 /usr/local/bin/$role，请手动迁移，安装未替换该命令。" >&2; exit 1
  fi
  ln -sfn "$install_root/bin/$role" "/usr/local/bin/$role"
done
if [[ "$manager" == systemd ]]; then
  install -m 644 "$source_root/templates/$component.service" "/etc/systemd/system/$component.service"
  systemctl daemon-reload
  ((autostart == 1)) && systemctl enable "$component" || systemctl disable "$component" || true
else
  install -m 755 "$source_root/templates/$component.openrc" "/etc/init.d/$component"
  if ((autostart == 1)); then rc-update add "$component" default; else rc-update del "$component" default || true; fi
fi
if [[ ! -f "$config_root/$component.toml" ]]; then
  if ((non_interactive == 1)); then echo "配置不存在，--non-interactive 不进入配置向导：$config_root/$component.toml" >&2; exit 2; fi
  FRP_CONFIG_DIR="$config_root" FRP_BINARY_DIR="$install_root/native" python3 "$install_root/lib/manager.py" "$component" configure
fi
if [[ -f "$config_root/$component.toml" ]]; then
  if "$install_root/native/$component" verify -c "$config_root/$component.toml" >/dev/null 2>&1; then
    if [[ "$manager" == systemd ]]; then systemctl start "$component"; else rc-service "$component" start; fi
  else
    echo '配置校验失败，服务未启动。' >&2
    false
  fi
else echo '配置尚未创建，服务已注册但保持停止。'; fi
printf '安装完成。输入 %s 打开数字菜单。防火墙只检测提示，请自行检查业务端口和映射端口规则。\n' "$component"
