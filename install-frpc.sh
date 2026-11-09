#!/usr/bin/env bash
# A complete checkout is not required: fetch the pinned manager source bundle.
set -Eeuo pipefail
base="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$base/install-common.sh" && -f "$base/lib/manager.py" ]]; then
  exec bash "$base/install-common.sh" frpc "$@"
fi
command -v curl >/dev/null || { printf '缺少 curl，请先安装 curl。\n' >&2; exit 1; }
work="$(mktemp -d)"
trap '[[ -n "${work:-}" && -d "$work" ]] && rm -rf -- "$work"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 'https://codeload.github.com/1911981822/frp-simple-bash/tar.gz/refs/heads/main' -o "$work/source.tar.gz"
tar -xzf "$work/source.tar.gz" -C "$work"
bash "$work/frp-simple-bash-main/install-common.sh" frpc "$@"
