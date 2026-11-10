#!/usr/bin/env bash
# Re-push the current release to the domestic mirror when SSH is available again.
# Usage: bash tools/push_mirror.sh v0.1.15-ux.1 [alias]
set -Eeuo pipefail
version="${1:?用法: push_mirror.sh <版本> [SSH别名]}"
alias="${2:-134.175.183.101}"
root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
remote="/www/wwwroot/download.sueiyuan.top/frp-simple-bash"
echo "目标: $alias:$remote  版本: $version"
ssh "$alias" "mkdir -p '$remote/releases/$version'"
for f in source.tar.gz SHA256SUMS.txt; do
  scp "$root/dist/$version/$f" "$alias:$remote/releases/$version/$f"
done
for f in install-frpc.sh install-frps.sh; do
  scp "$root/$f" "$alias:$remote/$f"
done
echo "完成。验证：curl -fsSL https://download.sueiyuan.top/frp-simple-bash/install-frpc.sh | grep FRP_MANAGER_VERSION"
