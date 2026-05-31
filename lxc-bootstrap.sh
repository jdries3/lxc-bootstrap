#!/bin/sh
set -eu

PROJECT_URL="https://raw.githubusercontent.com/jdries3/lxc-bootstrap/main/lxc-bootstrap.py"
UV_BIN="${UV_BIN:-${HOME}/.local/bin/uv}"
PROFILE_D_DIR="/etc/profile.d"
ALIAS_SH="${PROFILE_D_DIR}/lxc-bootstrap-rerun.sh"

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_uv() {
  if have_cmd uv; then
    UV_BIN="$(command -v uv)"
    return 0
  fi

  if [ -x "${UV_BIN}" ]; then
    return 0
  fi

  if have_cmd curl; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif have_cmd wget; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    printf '%s\n' 'ERROR: neither curl nor wget is available to install uv' >&2
    exit 1
  fi

  if have_cmd uv; then
    UV_BIN="$(command -v uv)"
  elif [ -x "${HOME}/.local/bin/uv" ]; then
    UV_BIN="${HOME}/.local/bin/uv"
  else
    printf '%s\n' 'ERROR: uv installation completed but uv was not found on PATH' >&2
    exit 1
  fi
}

install_alias() {
  if [ "$(id -u)" -ne 0 ]; then
    return 0
  fi

  mkdir -p "${PROFILE_D_DIR}"
  cat >"${ALIAS_SH}" <<EOF
# Re-run lxc-bootstrap from GitHub using uv.
alias lxc-bootstrap='${UV_BIN} run --refresh ${PROJECT_URL}'
EOF
  chmod 644 "${ALIAS_SH}"
}

install_uv
install_alias
exec "${UV_BIN}" run --refresh "${PROJECT_URL}" "$@"

