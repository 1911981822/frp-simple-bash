#!/usr/bin/env bash
set -Eeuo pipefail
FRP_MANAGER_VERSION='v1.0.0-rc.11'
base="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [[ -f "$base/install-common.sh" && -f "$base/lib/manager.py" && -f "$base/lib/download.sh" ]]; then
  exec bash "$base/install-common.sh" frpc "$@"
fi
for tool in curl tar; do command -v "$tool" >/dev/null || { echo "Missing dependency: $tool" >&2; exit 1; }; done
# macOS ships shasum but not sha256sum; either one is fine.
if command -v sha256sum >/dev/null; then sha_cmd=sha256sum
elif command -v shasum >/dev/null; then sha_cmd=shasum
else echo "Missing dependency: sha256sum or shasum" >&2; exit 1; fi
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
  if [[ "$sha_cmd" == sha256sum ]]; then
    printf '%s  %s\n' "$expected" "$work/source.tar.gz" | sha256sum -c - >/dev/null || { echo 'Source SHA256 mismatch; stopped without fallback.' >&2; exit 1; }
  else
    actual="$(shasum -a 256 "$work/source.tar.gz" | awk '{print $1}')"
    [[ "$actual" == "$expected" ]] || { echo 'Source SHA256 mismatch; stopped without fallback.' >&2; exit 1; }
  fi
  source_kind=mirror
else
  if [[ "${FRP_ALLOW_UNVERIFIED:-0}" != "1" ]]; then
    echo 'Mirror unavailable. Falling back to GitHub main has NO pinned SHA256,' >&2
    echo 'so execution is refused by default for safety.' >&2
    echo 'To allow it explicitly: sudo FRP_ALLOW_UNVERIFIED=1 bash -c "$script"' >&2
    exit 1
  fi
  echo 'WARNING: using UNVERIFIED GitHub main source (allowed by FRP_ALLOW_UNVERIFIED=1).' >&2
  fetch 'https://codeload.github.com/1911981822/frp-simple-bash/tar.gz/refs/heads/main' "$work/source.tar.gz" || { echo 'All source downloads failed.' >&2; exit 1; }
fi
# Only a completed, verified (for mirror) archive reaches extraction.
tar -xzf "$work/source.tar.gz" -C "$work"
if [[ "$source_kind" == mirror ]]; then source_dir="$work/frp-simple-bash"; else source_dir="$work/frp-simple-bash-main"; fi
[[ -f "$source_dir/install-common.sh" && -f "$source_dir/lib/download.sh" && -f "$source_dir/lib/manager.py" ]] || { echo 'Unexpected source archive layout.' >&2; exit 1; }
bash "$source_dir/install-common.sh" frpc "$@"
