#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export LIBERO_CONFIG_PATH="${LIBERO_PRO_CONFIG_PATH:-$PWD/maintenance/libero-pro-config}"
export MUJOCO_GL="${MUJOCO_GL:-egl}"
LIBERO_PRO_VENV="${LIBERO_PRO_VENV:-.venv-libero-pro}"
CUBENCE_API_KEY_FILE="${CUBENCE_API_KEY_FILE:-/opt/pac-libero/cubence-api-key}"
if [ -z "${OPENAI_API_KEY:-}" ] && [ -r "$CUBENCE_API_KEY_FILE" ]; then
  IFS= read -r OPENAI_API_KEY < "$CUBENCE_API_KEY_FILE"
  export OPENAI_API_KEY
fi
exec "$LIBERO_PRO_VENV/bin/python" -u -m pac_harness.libero_pro_adapt "$@"
