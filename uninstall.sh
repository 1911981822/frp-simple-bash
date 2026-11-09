#!/usr/bin/env bash
set -Eeuo pipefail
role="${1:?frps 或 frpc}"
[[ "$role" == frps || "$role" == frpc ]] || exit 2
[[ "${EUID}" == 0 ]] || { echo '需要 root 权限' >&2; exit 1; }
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  systemctl stop "$role" || true
  systemctl disable "$role" || true
  systemctl reset-failed "$role" >/dev/null 2>&1 || true
  target="/etc/systemd/system/$role.service"
  [[ "$target" == /etc/systemd/system/frps.service || "$target" == /etc/systemd/system/frpc.service ]] || exit 1
  rm -f -- "$target"
  systemctl daemon-reload
elif command -v rc-service >/dev/null; then
  rc-service "$role" stop || true
  rc-update del "$role" default || true
  target="/etc/init.d/$role"
  [[ "$target" == /etc/init.d/frps || "$target" == /etc/init.d/frpc ]] || exit 1
  rm -f -- "$target"
fi
target="/usr/local/bin/$role"
if [[ -L "$target" && "$(readlink "$target")" == "/opt/frp-simple-bash/bin/$role" ]]; then rm -- "$target"; fi
target="/opt/frp-simple-bash/native/$role"
[[ "$target" == /opt/frp-simple-bash/native/frpc || "$target" == /opt/frp-simple-bash/native/frps ]] || exit 1
rm -f -- "$target"
echo '卸载完成，/etc/frp 中的配置和日志已保留。'
