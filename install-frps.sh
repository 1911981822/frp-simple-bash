#!/usr/bin/env bash
set -Eeuo pipefail
FRP_MANAGER_VERSION='v0.1.23-purge.1'
base="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [[ -f "$base/install-common.sh" && -f "$base/lib/manager.py" && -f "$base/lib/download.sh" ]]; then
  exec bash "$base/install-common.sh" frps "$@"
fi
for tool in curl tar sha256sum; do command -v "$tool" >/dev/null || { echo "Missing dependency: $tool" >&2; exit 1; }; done
work="$(mktemp -d)"
cleanup() { [[ -n "${work:-}" && "$work" != / && -d "$work" ]] && rm -rf -- "$work"; }
trap cleanup EXIT
fetch() {
  if curl --silent --show-error --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 --retry 2 "$1" -o "$2.partial"; then mv -f -- "$2.partial" "$2"; else return 1; fi
}
mirror="${FRP_MIRROR_URL-https://download.sueiyuan.top}"
case "$mirror" in ''|off|OFF) mirror='' ;; https://*) mirror="${mirror%/}" ;; *) echo 'Mirror URL must use HTTPS, or be empty/off.' >&2; exit 1 ;; esac
source_kind=official
if [[ -n "$mirror" ]] && fetch "$mirror/frp-simple-bash/releases/$FRP_MANAGER_VERSION/source.tar.gz" "$work/source.tar.gz"; then
  # Same-origin manifest checks transfer integrity; it is not an independent signature.
  fetch "$mirror/frp-simple-bash/releases/$FRP_MANAGER_VERSION/SHA256SUMS.txt" "$work/SHA256SUMS.txt" || { echo 'Mirror manifest unavailable; refusing unchecked bundle.' >&2; exit 1; }
  expected="$(awk '$2 == "source.tar.gz" || $2 == "*source.tar.gz" {print $1}' "$work/SHA256SUMS.txt")"
  [[ "$expected" =~ ^[a-fA-F0-9]{64}$ ]] || { echo 'Invalid source bundle manifest.' >&2; exit 1; }
  printf '%s  %s\n' "$expected" "$work/source.tar.gz" | sha256sum -c - >/dev/null || { echo 'Source SHA256 mismatch; stopped without fallback.' >&2; exit 1; }
  source_kind=mirror
else
  fetch 'https://codeload.github.com/1911981822/frp-simple-bash/tar.gz/refs/heads/main' "$work/source.tar.gz" || { echo 'All source downloads failed.' >&2; exit 1; }
fi
# Only a completed, verified (for mirror) archive reaches extraction.
tar -xzf "$work/source.tar.gz" -C "$work"
if [[ "$source_kind" == mirror ]]; then source_dir="$work/frp-simple-bash"; else source_dir="$work/frp-simple-bash-main"; fi
[[ -f "$source_dir/install-common.sh" && -f "$source_dir/lib/download.sh" && -f "$source_dir/lib/manager.py" ]] || { echo 'Unexpected source archive layout.' >&2; exit 1; }
bash "$source_dir/install-common.sh" frps "$@"
