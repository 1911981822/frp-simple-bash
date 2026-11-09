#!/usr/bin/env bash
# Installs native binaries separately from the management commands.
set -Eeuo pipefail
component="${1:?需要 frps 或 frpc}"
[[ "$component" == frps || "$component" == frpc ]] || exit 2
[[ "$(uname -s)" == Linux ]] || { echo '此安装器仅支持 Linux；Windows 请使用 PowerShell 安装器。' >&2; exit 1; }
[[ "${EUID}" == 0 ]] || { echo '请使用 root/sudo 安装。' >&2; exit 1; }
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$source_root/lib/download.sh"
install_root="/opt/frp-simple-bash"
config_root="/etc/frp"
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
  read -r -p '确认安装依赖？[y/N] ' answer
  [[ "$answer" =~ ^[Yy]$ ]] || exit 1
  if command -v apt-get >/dev/null; then apt-get update; apt-get install -y curl tar coreutils python3
  elif command -v dnf >/dev/null; then dnf install -y curl tar coreutils python3
  elif command -v yum >/dev/null; then yum install -y curl tar coreutils python3
  elif command -v apk >/dev/null; then apk add curl tar coreutils python3
  else echo '无法识别包管理器，请手动安装。' >&2; exit 1; fi
fi
python3 -c 'import sys; assert sys.version_info >= (3,9), "需要 Python 3.9+，请安装后重试"'
if ! python3 -c 'try: import tomllib
except ImportError: import tomli' 2>/dev/null; then
  echo 'Python 低于 3.11，需要发行版 TOML 解析包 python3-tomli。'
  read -r -p '确认通过包管理器安装 TOML 依赖？[y/N] ' answer
  [[ "$answer" =~ ^[Yy]$ ]] || exit 1
  if command -v apt-get >/dev/null; then apt-get install -y python3-tomli
  elif command -v dnf >/dev/null; then dnf install -y python3-tomli
  elif command -v yum >/dev/null; then yum install -y python3-tomli
  elif command -v apk >/dev/null; then apk add py3-tomli
  else echo '请先安装 tomli。' >&2; exit 1; fi
fi
python3 -c 'try: import tomllib
except ImportError: import tomli'
manager=none
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then manager=systemd
elif command -v rc-service >/dev/null && command -v rc-update >/dev/null; then manager=openrc
else echo '未检测到 systemd/OpenRC；不能完成后台与自启动安装。' >&2; exit 1; fi
work="$(mktemp -d)"
trap '[[ -n "${work:-}" && -d "$work" ]] && rm -rf -- "$work"' EXIT
asset="frp_${version}_linux_${arch}.tar.gz"
if [[ -n "${FRP_ARCHIVE_PATH:-}" ]]; then
  [[ -f "$FRP_ARCHIVE_PATH" ]] || { echo '本地 frp 安装包不存在。' >&2; exit 1; }
  cp -- "$FRP_ARCHIVE_PATH" "$work/$asset"
else
  case "$arch" in amd64) asset_id=710291514 ;; arm64) asset_id=710291691 ;; esac
  frp_download_asset "$work/$asset" "$expected" "frp/v${version}/$asset" "https://github.com/fatedier/frp/releases/download/v${version}/${asset}" "https://api.github.com/repos/fatedier/frp/releases/assets/$asset_id"
fi
if ! printf '%s  %s\n' "$expected" "$work/$asset" | sha256sum -c - >/dev/null; then
  echo 'frp 安装包 SHA256 校验失败，未执行二进制、未修改现有服务。' >&2
  exit 1
fi
tar -xzf "$work/$asset" -C "$work"
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
  systemctl daemon-reload; systemctl enable "$component"
else
  install -m 755 "$source_root/templates/$component.openrc" "/etc/init.d/$component"
  rc-update add "$component" default
fi
if [[ ! -f "$config_root/$component.toml" ]]; then
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
