#!/usr/bin/env bash
# Remove what install.sh created. Safe to run twice, and safe to run when the
# install never completed. Without --purge the findings database survives: it is
# the result of every run ever made and losing it silently would be the worst
# thing this script could do.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
source "${here}/project.conf"

purge=false
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) purge=true; shift ;;
    -h|--help) echo "Usage: bash deploy/uninstall.sh [--purge]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*" >&2; }

refuse_unsafe() {
  case "$2" in
    ''|'/'|"${HOME}"|'/usr'|'/etc'|'/var'|'/opt'|'/home')
      say "Refusing to remove $1=$2"; exit 1 ;;
  esac
}

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user disable --now "${SERVICE_NAME}" >/dev/null 2>&1 || true
fi
if [ -e "${UNIT_FILE}" ]; then
  rm -f "${UNIT_FILE}"
  say "Removed ${UNIT_FILE}"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

if [ -f "${ENV_FILE}" ]; then
  refuse_unsafe CONFIG_DIR "${CONFIG_DIR}"
  rm -f "${ENV_FILE}"
  say "Removed ${ENV_FILE}"
  rmdir "${CONFIG_DIR}" 2>/dev/null || true
fi

if ${purge}; then
  refuse_unsafe DATA_DIR "${DATA_DIR}"
  if [ -d "${DATA_DIR}" ]; then
    rm -rf "${DATA_DIR}"
    say "Purged ${DATA_DIR} -- every finding track ever collected is gone."
  fi
else
  [ -d "${DATA_DIR}" ] && say "Kept ${DATA_DIR} (pass --purge to delete the findings too)."
fi

say "track uninstalled."
