#!/usr/bin/env bash
# Install track, and optionally its web view, asking what it needs to know.
#
# Run by `ownbox install track`, which executes setup commands with its own
# stdin/stdout inherited (ownbox/store.py:244 -- subprocess.run with neither
# capture_output nor a stdin redirect). So in a terminal this can genuinely
# prompt. It must equally never BLOCK: ownbox's COMMAND_TIMEOUT is 1800s, so a
# prompt with nobody there does not fail fast, it hangs for half an hour and
# then dies. Every question is therefore guarded by `[ -t 0 ]` and every answer
# has an environment-variable override, which is also what makes this scriptable.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(dirname -- "${here}")
cd -- "${repo}"

# shellcheck source=/dev/null
source "${here}/project.conf"

VENV="${repo}/.venv"
TRACK_BIN="${VENV}/bin/track"

say() { printf '%s\n' "$*" >&2; }
note() { printf '  %s\n' "$*" >&2; }

interactive=false
if [ -t 0 ] && [ "${TRACK_INSTALL_NONINTERACTIVE:-0}" != "1" ]; then
  interactive=true
fi

ask() {
  # ask <var-name> <prompt> <default> [valid ...]
  local __var=$1 prompt=$2 default=$3; shift 3
  local valid=("$@") reply=""
  local override="${!__var:-}"

  if [ -n "${override}" ]; then
    say "${prompt} ${override}   (from \$${__var})"
    printf -v "${__var}" '%s' "${override}"
    return
  fi
  if ! ${interactive}; then
    say "${prompt} ${default}   (no terminal; using the default)"
    printf -v "${__var}" '%s' "${default}"
    return
  fi
  while true; do
    printf '%s [%s] ' "${prompt}" "${default}" >&2
    read -r reply || reply=""
    reply=${reply:-${default}}
    if [ ${#valid[@]} -eq 0 ]; then break; fi
    for option in "${valid[@]}"; do
      [ "${reply}" = "${option}" ] && break 2
    done
    say "Please answer one of: ${valid[*]}"
  done
  printf -v "${__var}" '%s' "${reply}"
}

# ---------------------------------------------------------------- the tool
say "Installing track into ${repo}"
# TRACK_INSTALL_SKIP_VENV exists for tests/test_web_deploy.py, which drives this
# script eleven times to prove it never hangs without a TTY. Building a venv and
# pip-installing each time would make that suite minutes long and would be
# testing pip, not the installer.
if [ "${TRACK_INSTALL_SKIP_VENV:-0}" = "1" ]; then
  say "Skipping the venv (TRACK_INSTALL_SKIP_VENV=1)"
else
  python3 -m venv "${VENV}" 2>/dev/null || true
  "${VENV}/bin/python" -m pip install --quiet --upgrade pip
  "${VENV}/bin/python" -m pip install --quiet -e ".[web]"
  say "track installed: $("${TRACK_BIN}" --version 2>&1 | head -1)"
fi

# ---------------------------------------------------------------- the web view
say ""
ask TRACK_WEB "Serve the findings as a web page?" "yes" yes no

if [ "${TRACK_WEB}" != "yes" ]; then
  say ""
  say "Done. No web view; run 'track web serve' by hand any time, or re-run"
  say "this installer to set it up as a service."
  exit 0
fi

ask TRACK_WEB_PORT "  Port?" "${DEFAULT_PORT}"
case "${TRACK_WEB_PORT}" in
  ''|*[!0-9]*) say "Not a port number: ${TRACK_WEB_PORT}"; exit 2 ;;
esac

# A straight binary, in his words: "when i mean bind to the interntet i mean
# binding to 0.0.0.0". He knows what that does, so the prompt asks and does not
# argue. What it means for who can read the page is stated once in the README,
# which is documentation's job rather than a question's.
ask TRACK_WEB_BIND "  Bind to 0.0.0.0 (open) or 127.0.0.1 (local only)?" "local" open local

case "${TRACK_WEB_BIND}" in
  open)  hosts="0.0.0.0" ;;
  local) hosts="127.0.0.1" ;;
esac

mkdir -p "${CONFIG_DIR}" "${UNIT_DIR}"
if [ -f "${ENV_FILE}" ] && [ "${TRACK_INSTALL_FORCE:-0}" != "1" ]; then
  say ""
  say "Keeping the existing ${ENV_FILE}:"
  sed 's/^/  /' "${ENV_FILE}" >&2
  say "(delete it, or set TRACK_INSTALL_FORCE=1, to have this rewritten)"
else
  umask 077
  {
    printf '# Written by deploy/install.sh. Re-run the installer to change it.\n'
    printf 'TRACK_WEB_PORT=%s\n' "${TRACK_WEB_PORT}"
    printf '# bind=%s\n' "${TRACK_WEB_BIND}"
    printf 'TRACK_WEB_HOSTS=%s\n' "${hosts}"
  } > "${ENV_FILE}"
  say ""
  say "Wrote ${ENV_FILE}"
fi

sed -e "s|@ENV_FILE@|${ENV_FILE}|g" -e "s|@TRACK_BIN@|${TRACK_BIN}|g" \
  "${here}/track-web.service.in" > "${UNIT_FILE}"

# Everything below is best-effort on purpose. The tool is installed and the
# config is written by this point; if systemd will not co-operate -- no user
# session, a unit directory it does not scan, a port already taken -- that is
# worth reporting loudly, but it is not worth failing an install that otherwise
# succeeded and leaving ownbox to mark the whole tool broken.
service_ok=false
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user daemon-reload || true
  if systemctl --user enable "${SERVICE_NAME}" >/dev/null 2>&1 &&
     systemctl --user restart "${SERVICE_NAME}" >/dev/null 2>&1; then
    service_ok=true
  fi
else
  say "No user systemd session here; the unit is written but not started."
fi

if ${service_ok}; then
  say "Enabled ${SERVICE_NAME}"
  sleep 1
  state=$(systemctl --user show -p SubState --value "${SERVICE_NAME}" 2>/dev/null || echo unknown)
  say "  service: ${state}"
  journalctl --user -u "${SERVICE_NAME}" -n 20 --no-pager -o cat 2>/dev/null |
    grep -i "listening" | tail -3 | sed 's/^/  /' >&2 || true
  if [ "${state}" != "running" ]; then
    say "  The service is not running. Its log:"
    journalctl --user -u "${SERVICE_NAME}" -n 10 --no-pager -o cat 2>/dev/null |
      sed 's/^/    /' >&2 || true
  fi
elif command -v systemctl >/dev/null 2>&1; then
  say "Could not enable ${SERVICE_NAME}; the unit is at ${UNIT_FILE}."
  say "Serve it by hand with: ${TRACK_BIN} web serve"
fi

say ""
say "Done. Uninstall with 'ownbox uninstall track', which removes the service"
say "and the config but keeps ${DATA_DIR} unless you pass --purge."
