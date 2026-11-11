#!/usr/bin/env bash
set -Eeuo pipefail
role="${1:?用法：uninstall.sh frps|frpc [--purge] [--yes]}"
[[ "$role" == frps || "$role" == frpc ]] || exit 2
shift
purge=0; yes=0
while (($#)); do
  case "$1" in
    --purge) purge=1; shift;;
    --yes) yes=1; shift;;
    --help|-h) echo "用法：uninstall.sh $role [--purge] [--yes]"; exit 0;;
    *) echo "未知参数：$1" >&2; exit 2;;
  esac
done
[[ "${EUID}" == 0 ]] || { echo '需要 root 权限' >&2; exit 1; }
if ((yes == 0)); then
  read -r -p "卸载 $role（默认保留配置和日志）？输入 YES 确认：" answer
  [[ "$answer" == YES ]] || { echo '已取消。'; exit 0; }
fi
unit="/etc/systemd/system/$role.service"
if [[ -f "$unit" ]]; then
  grep -Fq "ExecStart=/opt/frp-simple-bash/native/$role -c /etc/frp/$role.toml" "$unit" || { echo "拒绝删除非本项目服务文件：$unit" >&2; exit 1; }
fi
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  systemctl stop "$role" || true; systemctl disable "$role" || true; systemctl reset-failed "$role" >/dev/null 2>&1 || true
  rm -f -- "$unit"; systemctl daemon-reload
elif command -v rc-service >/dev/null; then
  unit="/etc/init.d/$role"
  if [[ -f "$unit" ]]; then grep -Fq "/opt/frp-simple-bash/native/$role" "$unit" || { echo "拒绝删除非本项目 OpenRC 文件：$unit" >&2; exit 1; }; fi
  rc-service "$role" stop || true; rc-update del "$role" default || true; rm -f -- "$unit"
fi
command="/usr/local/bin/$role"
if [[ -L "$command" ]]; then [[ "$(readlink "$command")" == "/opt/frp-simple-bash/bin/$role" ]] || { echo "拒绝删除非本项目命令链接" >&2; exit 1; }; rm -f -- "$command"; fi
native="/opt/frp-simple-bash/native/$role"
[[ "$native" == /opt/frp-simple-bash/native/frps || "$native" == /opt/frp-simple-bash/native/frpc ]] || exit 1
rm -f -- "$native"
if ((purge == 1)); then
  ((yes == 1)) || { echo '--purge 必须同时提供 --yes，避免误删。' >&2; exit 2; }
  other=$([[ "$role" == frps ]] && echo frpc || echo frps)
  # NOTE: /opt/frp-simple-bash/bin/{frps,frpc} 是组件共享的菜单入口，安装任一组件
  # 都会同时放置两个，不能作为「另一组件仍安装」的判据（否则 purge 永远失败）。
  # 只有 native 二进制、用户命令链接、服务单元才代表该组件真实存在。
  other_traces=()
  for candidate in "/usr/local/bin/$other" "/opt/frp-simple-bash/native/$other" \
                   "/etc/systemd/system/$other.service" "/etc/init.d/$other"; do
    [[ -e "$candidate" || -L "$candidate" ]] && other_traces+=("$candidate")
  done
  if ((${#other_traces[@]})); then
    echo "另一个组件 $other 仍安装，拒绝清理共享目录。" >&2
    printf '  发现残留：%s\n' "${other_traces[@]}" >&2
    echo "  如确认不再需要，请先执行：sudo bash /opt/frp-simple-bash/uninstall.sh $other" >&2
    exit 1
  fi
  [[ -d /etc/frp && -f /etc/frp/state.json ]] || { echo '配置目录不是本项目目录，拒绝清理。' >&2; exit 1; }
  rm -rf -- /etc/frp /opt/frp-simple-bash
  echo '彻底清理完成：配置、映射、日志和备份均已删除。'
else
  echo '卸载完成，配置、映射、日志和备份已保留。'
fi
