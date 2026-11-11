#!/usr/bin/env bash
# Download helpers: a successful HTTP transfer is not a successful checksum.
FRP_MANAGER_VERSION='v0.1.24-units.1'
frp_mirror_url() {
  local value="${FRP_MIRROR_URL-https://download.sueiyuan.top}"
  case "$value" in ''|off|OFF) return 0 ;; esac
  [[ "$value" == https://* && "$value" != *[[:space:]]* ]] || { echo 'FRP_MIRROR_URL must use HTTPS (or empty/off).' >&2; return 1; }
  printf '%s' "${value%/}"
}
frp_fetch() {
  local url="$1" target="$2" partial="${2}.partial"
  case "$url" in https://*) ;; *) echo 'Refusing non-HTTPS download.' >&2; return 1 ;; esac
  if curl --silent --show-error --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 10 --max-time 1200 --retry 2 "$url" -o "$partial"; then
    mv -f -- "$partial" "$target"
    return 0
  fi
  [[ ! -e "$partial" ]] || : > "$partial"
  return 1
}
frp_download_asset() {
  local target="$1" expected="$2" relative="$3" official="$4" api_url="${5:-}" mirror
  mirror="$(frp_mirror_url)" || return
  if [[ -n "$mirror" ]] && frp_fetch "$mirror/$relative?mirror=$FRP_MANAGER_VERSION" "$target"; then
    echo 'Downloaded from domestic mirror.' >&2
  elif frp_fetch "$official" "$target"; then
    echo 'Downloaded from official release.' >&2
  elif [[ -n "$api_url" ]] && curl --silent --show-error --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 10 --max-time 1200 --retry 2 -H 'Accept: application/octet-stream' "$api_url" -o "${target}.partial"; then
    mv -f -- "${target}.partial" "$target"
  else
    echo 'All download sources failed; no archive will be extracted.' >&2
    return 1
  fi
  if ! printf '%s  %s\n' "$expected" "$target" | sha256sum -c - >/dev/null; then
    echo 'SHA256 mismatch; refusing archive. No fallback after checksum failure.' >&2
    return 1
  fi
}
