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
[[ "$(id -u)" == 0 ]] || { echo '需要 root 权限' >&2; exit 1; }
if ((yes == 0)); then
  read -r -p "卸载 $role（默认保留配置和日志）？输入 YES 确认：" answer
  [[ "$answer" == YES ]] || { echo '已取消。'; exit 0; }
fi
unit="/etc/systemd/system/$role.service"
if [[ -f "$unit" ]]; then
  grep -Fq "ExecStart=/opt/frp-simple-bash/native/$role -c /etc/frp/$role.toml" "$unit" || { echo "拒绝删除非本项目服务文件：$unit" >&2; exit 1; }
fi
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  if [[ -f "$unit" ]]; then
    # Only stop/disable when the unit actually exists; otherwise the errors
    # "Unit not loaded" / "does not exist" leak to the terminal and mislead the
    # user into thinking uninstall failed (the component was never installed).
    systemctl stop "$role" >/dev/null 2>&1 || true
    systemctl disable "$role" >/dev/null 2>&1 || true
    systemctl reset-failed "$role" >/dev/null 2>&1 || true
    rm -f -- "$unit"; systemctl daemon-reload
  fi
elif command -v rc-service >/dev/null; then
  unit="/etc/init.d/$role"
  if [[ -f "$unit" ]]; then
    grep -Fq "/opt/frp-simple-bash/native/$role" "$unit" || { echo "拒绝删除非本项目 OpenRC 文件：$unit" >&2; exit 1; }
    rc-service "$role" stop >/dev/null 2>&1 || true
    rc-update del "$role" default >/dev/null 2>&1 || true
    rm -f -- "$unit"
  fi
elif [[ "$(uname -s)" == Darwin ]] && command -v launchctl >/dev/null; then
  plist="/Library/LaunchDaemons/frp-simple-bash.$role.plist"
  if [[ -f "$plist" ]]; then
    # disable clears the persistent boot policy, unload stops it now.
    launchctl disable "system/frp-simple-bash.$role" >/dev/null 2>&1 || true
    launchctl unload "$plist" >/dev/null 2>&1 || true
    rm -f -- "$plist"
  fi
fi
command="/usr/local/bin/$role"
if [[ -L "$command" ]]; then [[ "$(readlink "$command")" == "/opt/frp-simple-bash/bin/$role" ]] || { echo "拒绝删除非本项目命令链接" >&2; exit 1; }; rm -f -- "$command"; fi
native="/opt/frp-simple-bash/native/$role"
[[ "$native" == /opt/frp-simple-bash/native/frps || "$native" == /opt/frp-simple-bash/native/frpc ]] || exit 1
rm -f -- "$native"
if ((purge == 1)); then
  ((yes == 1)) || { echo '--purge 必须同时提供 --yes，避免误删。' >&2; exit 2; }
  other=$([[ "$role" == frps ]] && echo frpc || echo frps)
  config_root=/etc/frp
  [[ -d "$config_root" && -f "$config_root/state.json" ]] || { echo '配置目录不是本项目目录，拒绝清理。' >&2; exit 1; }

  # 1) Always delete THIS component's own config — purge means "彻底清理 frpc",
  #    and must never be blocked by the other component still being installed.
  if [[ "$role" == frpc ]]; then
    rm -rf -- "$config_root/frpc.toml" "$config_root/profiles" "$config_root/index.json"
    # state.json holds both components' runtime markers; drop the frpc-specific
    # keys but keep frps keys intact.
    if [[ -f "$config_root/state.json" ]]; then
      python3 - "$config_root/state.json" <<'PY'
import json, sys
p = sys.argv[1]
try:
    with open(p, encoding='utf-8') as f: data = json.load(f)
except Exception: data = {}
for key in ('active_profile_id', 'pending', 'frpc_hash', 'frpc_log_offset'):
    data.pop(key, None)
with open(p, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)
PY
    fi
  else
    rm -rf -- "$config_root/frps.toml" "$config_root/.canonical-frps.toml"
    if [[ -f "$config_root/state.json" ]]; then
      python3 - "$config_root/state.json" <<'PY'
import json, sys
p = sys.argv[1]
try:
    with open(p, encoding='utf-8') as f: data = json.load(f)
except Exception: data = {}
data.pop('frps_hash', None)
with open(p, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)
PY
    fi
  fi

  # 2) The shared directory (/etc/frp + /opt/frp-simple-bash) can only be
  #    removed when the OTHER component is also gone. If it is still installed,
  #    keep the shared dirs and only drop this component's own files.
  other_traces=()
  for candidate in "/usr/local/bin/$other" "/opt/frp-simple-bash/native/$other" \
                   "/etc/systemd/system/$other.service" "/etc/init.d/$other"; do
    [[ -e "$candidate" || -L "$candidate" ]] && other_traces+=("$candidate")
  done
  if ((${#other_traces[@]})); then
    echo "已删除 $role 的配置；$other 仍安装，保留共享目录（state.json/logs/backups）。" >&2
    printf '  %s 残留：%s\n' "$other" "${other_traces[*]}" >&2
    echo "  若也要移除 $other，请执行：sudo bash /opt/frp-simple-bash/uninstall.sh $other --purge --yes" >&2
    exit 0
  fi

  # 3) Neither component remains: remove the shared dirs entirely.
  rm -rf -- /etc/frp /opt/frp-simple-bash
  echo '彻底清理完成：两个组件的配置、映射、日志和备份均已删除。'
else
  echo '卸载完成，配置、映射、日志和备份已保留。'
fi
