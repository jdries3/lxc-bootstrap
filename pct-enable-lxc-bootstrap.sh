#!/usr/bin/env bash
set -euo pipefail

PVE_LXC_CONFIG_DIR="${PVE_LXC_CONFIG_DIR:-/etc/pve/lxc}"
PCT_START_TIMEOUT_SECONDS="${PCT_START_TIMEOUT_SECONDS:-30}"

usage() {
  local prog_name
  prog_name="$(basename "$0")"
  cat <<EOF
Usage: ./${prog_name} [-y|--yes] [-r|--restart] <CTID>

Applies the required Proxmox LXC host-side settings for lxc-bootstrap:
- validates the CT exists via pct
- shows container metadata before making changes
- pct set <CTID> --features nesting=1,keyctl=1
- lxc.apparmor.profile: unconfined
- lxc.cap.drop:
- lxc.cgroup.relative: 0

Options:
  -y, --yes       Skip the confirmation prompt
  -r, --restart   Restart/start the CT after updating config, then prove it is running
  -h, --help      Show this help text
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

extract_pct_config_value() {
  local config_text="$1"
  local key="$2"

  printf '%s\n' "${config_text}" |
    awk -v key="${key}" '
index($0, key ":") == 1 {
  value = substr($0, length(key) + 2)
  sub(/^[[:space:]]+/, "", value)
  print value
  exit
}
'
}

extract_pct_status_value() {
  local status_text="$1"

  printf '%s\n' "${status_text}" |
    awk '
index($0, "status:") == 1 {
  value = substr($0, length("status:") + 1)
  sub(/^[[:space:]]+/, "", value)
  print value
  exit
}
'
}

ensure_config_line() {
  local file_path="$1"
  local key_prefix="$2"
  local replacement="$3"
  local temp_file

  temp_file="$(mktemp)"

  awk -v key_prefix="${key_prefix}" -v replacement="${replacement}" '
BEGIN {
  seen = 0
}
index($0, key_prefix) == 1 {
  if (!seen) {
    print replacement
    seen = 1
  }
  next
}
{
  print
}
END {
  if (!seen) {
    print replacement
  }
}
' "${file_path}" >"${temp_file}"

  cat "${temp_file}" >"${file_path}"
  rm -f "${temp_file}"
}

confirm_or_exit() {
  local prompt="$1"
  local reply

  printf '%s' "${prompt}" >&2
  read -r reply
  case "${reply}" in
    y | Y | yes | YES)
      return 0
      ;;
    *)
      printf 'Aborted.\n' >&2
      exit 1
      ;;
  esac
}

wait_for_pct_status() {
  local ctid="$1"
  local expected_status="$2"
  local timeout_seconds="$3"
  local elapsed=0
  local status_text
  local current_status

  while [ "${elapsed}" -lt "${timeout_seconds}" ]; do
    if status_text="$(pct status "${ctid}" 2>/dev/null)"; then
      current_status="$(extract_pct_status_value "${status_text}")"
      if [ "${current_status}" = "${expected_status}" ]; then
        printf '%s\n' "${status_text}"
        return 0
      fi
    fi

    sleep 1
    elapsed=$((elapsed + 1))
  done

  return 1
}

restart_or_start_ct() {
  local ctid="$1"
  local prior_status="$2"
  local final_status_text

  if [ "${prior_status}" = "running" ]; then
    printf 'Restarting CT %s...\n' "${ctid}"
    pct stop "${ctid}"
  else
    printf 'CT %s is currently %s; starting it now...\n' "${ctid}" "${prior_status}"
  fi

  pct start "${ctid}"

  if ! final_status_text="$(wait_for_pct_status "${ctid}" 'running' "${PCT_START_TIMEOUT_SECONDS}")"; then
    die "CT ${ctid} did not reach running state within ${PCT_START_TIMEOUT_SECONDS}s after restart/start"
  fi

  printf 'Proof that CT %s is back up and running:\n' "${ctid}"
  printf '%s\n' "${final_status_text}"
}

main() {
  local assume_yes=0
  local restart_after_update=0
  local ctid=""
  local config_path
  local pct_config_text
  local pct_status_text=""
  local hostname=""
  local status_value="unknown"
  local ostype=""
  local arch=""
  local unprivileged=""
  local memory=""
  local rootfs=""
  local net0=""

  while [ $# -gt 0 ]; do
    case "$1" in
      -y | --yes)
        assume_yes=1
        shift
        ;;
      -r | --restart)
        restart_after_update=1
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      -*)
        die "Unknown option: $1"
        ;;
      *)
        if [ -n "${ctid}" ]; then
          die "Only one CTID may be specified"
        fi
        ctid="$1"
        shift
        ;;
    esac
  done

  if [ $# -gt 0 ]; then
    die "Unexpected extra arguments: $*"
  fi

  if [ -z "${ctid}" ]; then
    usage
    exit 1
  fi

  case "${ctid}" in
    *[!0-9]* | '')
      die "CTID must be numeric"
      ;;
  esac

  command -v pct >/dev/null 2>&1 || die "pct command not found"

  config_path="${PVE_LXC_CONFIG_DIR}/${ctid}.conf"
  [ -f "${config_path}" ] || die "Config not found: ${config_path}"

  if ! pct_config_text="$(pct config "${ctid}" 2>/dev/null)"; then
    die "pct does not recognize CT ${ctid}; nothing changed"
  fi

  if pct_status_text="$(pct status "${ctid}" 2>/dev/null)"; then
    status_value="$(extract_pct_status_value "${pct_status_text}")"
    if [ -z "${status_value}" ]; then
      status_value="unknown"
    fi
  fi

  hostname="$(extract_pct_config_value "${pct_config_text}" 'hostname')"
  ostype="$(extract_pct_config_value "${pct_config_text}" 'ostype')"
  arch="$(extract_pct_config_value "${pct_config_text}" 'arch')"
  unprivileged="$(extract_pct_config_value "${pct_config_text}" 'unprivileged')"
  memory="$(extract_pct_config_value "${pct_config_text}" 'memory')"
  rootfs="$(extract_pct_config_value "${pct_config_text}" 'rootfs')"
  net0="$(extract_pct_config_value "${pct_config_text}" 'net0')"

  printf 'About to update Proxmox CT metadata:\n'
  printf '  CTID: %s\n' "${ctid}"
  printf '  Hostname: %s\n' "${hostname:-<unset>}"
  printf '  Status: %s\n' "${status_value}"
  printf '  OSType: %s\n' "${ostype:-<unset>}"
  printf '  Arch: %s\n' "${arch:-<unset>}"
  printf '  Unprivileged: %s\n' "${unprivileged:-<unset>}"
  printf '  Memory (MiB): %s\n' "${memory:-<unset>}"
  printf '  Rootfs: %s\n' "${rootfs:-<unset>}"
  printf '  Net0: %s\n' "${net0:-<unset>}"
  printf '  Config path: %s\n' "${config_path}"
  printf '\nPlanned changes:\n'
  printf '  - pct set %s --features nesting=1,keyctl=1\n' "${ctid}"
  printf '  - ensure lxc.apparmor.profile: unconfined\n'
  printf '  - ensure lxc.cap.drop: <blank value with trailing space>\n'
  printf '  - ensure lxc.cgroup.relative: 0\n'
  if [ "${restart_after_update}" = "1" ]; then
    printf '  - restart/start CT %s and wait for status: running\n' "${ctid}"
  fi
  printf '\n'

  if [ "${assume_yes}" != "1" ]; then
    confirm_or_exit "Proceed with CT ${ctid} (${hostname:-<unset>})? [y/N] "
  fi

  pct set "${ctid}" --features nesting=1,keyctl=1

  ensure_config_line "${config_path}" 'lxc.apparmor.profile:' 'lxc.apparmor.profile: unconfined'
  ensure_config_line "${config_path}" 'lxc.cap.drop:' 'lxc.cap.drop: '
  ensure_config_line "${config_path}" 'lxc.cgroup.relative:' 'lxc.cgroup.relative: 0'

  printf 'Updated %s\n' "${config_path}"

  if [ "${restart_after_update}" = "1" ]; then
    restart_or_start_ct "${ctid}" "${status_value}"
  else
    printf 'Restart the container to apply all changes: pct stop %s && pct start %s\n' "${ctid}" "${ctid}"
  fi
}

main "$@"
