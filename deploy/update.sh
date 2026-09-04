#!/usr/bin/env bash
# Reinstall the package in place and restart the web service if it is running.
# Deliberately asks nothing: an update must not block on a question, and the
# answers from install time are already in the env file.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(dirname -- "${here}")
cd -- "${repo}"
# shellcheck source=/dev/null
source "${here}/project.conf"

"${repo}/.venv/bin/python" -m pip install --quiet -e ".[web]"
echo "track updated: $("${repo}/.venv/bin/track" --version 2>&1 | head -1)" >&2

if systemctl --user is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
  systemctl --user restart "${SERVICE_NAME}"
  echo "Restarted ${SERVICE_NAME}" >&2
fi
