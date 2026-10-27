#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
LIBERO_PRO_VENV="${LIBERO_PRO_VENV:-.venv-libero-pro}"
if [ ! -x "$LIBERO_PRO_VENV/bin/python" ]; then
  printf 'Run bash scripts/setup_libero_pro.sh first.\n' >&2
  exit 1
fi
export LIBERO_CONFIG_PATH="${LIBERO_PRO_CONFIG_PATH:-$PWD/maintenance/libero-pro-config}"
export MUJOCO_GL="${MUJOCO_GL:-egl}"
CUBENCE_API_KEY_FILE="${CUBENCE_API_KEY_FILE:-/opt/pac-libero/cubence-api-key}"
if [ -z "${OPENAI_API_KEY:-}" ] && [ -r "$CUBENCE_API_KEY_FILE" ]; then
  IFS= read -r OPENAI_API_KEY < "$CUBENCE_API_KEY_FILE"
  export OPENAI_API_KEY
fi
exec "$LIBERO_PRO_VENV/bin/python" -m pac_harness.libero_pro "$@"
