#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
LIBERO_VENV_DIR="${LIBERO_VENV_DIR:-.venv}"
CUBENCE_API_KEY_FILE="${CUBENCE_API_KEY_FILE:-/opt/pac-libero/cubence-api-key}"
if [ -z "${OPENAI_API_KEY:-}" ] && [ -r "$CUBENCE_API_KEY_FILE" ]; then
  IFS= read -r OPENAI_API_KEY < "$CUBENCE_API_KEY_FILE"
  export OPENAI_API_KEY
fi
exec "$LIBERO_VENV_DIR/bin/python" -u -m pac_harness.post_run "$@"
