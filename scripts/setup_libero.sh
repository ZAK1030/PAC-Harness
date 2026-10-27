#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Isolated environment: leave system Torch / accelerator packages unchanged.
LIBERO_VENV_DIR="${LIBERO_VENV_DIR:-.venv}"
if [ ! -x "$LIBERO_VENV_DIR/bin/python" ]; then
  uv venv --python 3.11 "$LIBERO_VENV_DIR"
fi
uv pip install --python "$LIBERO_VENV_DIR/bin/python" -e . 'numpy<2' 'robosuite==1.4.1' 'mujoco==3.2.7' 'bddl==1.0.1' easydict cloudpickle gym PyYAML pillow matplotlib future
uv pip install --python "$LIBERO_VENV_DIR/bin/python" "torch==2.14.1" --index-url https://download.pytorch.org/whl/cpu
mkdir -p .deps
if [ ! -d .deps/LIBERO ]; then
  git init .deps/LIBERO
  git -C .deps/LIBERO remote add origin https://github.com/Lifelong-Robot-Learning/LIBERO.git
  git -C .deps/LIBERO fetch --depth 1 origin 8f1084e3132a39270c3a13ebe37270a43ece2a01
  git -C .deps/LIBERO checkout --detach FETCH_HEAD
fi
if [ ! -f .deps/LIBERO/libero/libero/benchmark/__init__.py ]; then
  printf 'LIBERO checkout is incomplete: .deps/LIBERO\n' >&2
  exit 1
fi
uv pip install --python "$LIBERO_VENV_DIR/bin/python" --no-deps -e .deps/LIBERO
# Upstream setup.py uses find_packages() and misses its namespace root on PEP 660.
"$LIBERO_VENV_DIR/bin/python" - <<'PYTHON'
import sysconfig
from pathlib import Path
(Path(sysconfig.get_paths()["purelib"]) / "libero_source.pth").write_text(str(Path(".deps/LIBERO").resolve()) + "\n")
PYTHON
printf '\nReady: MUJOCO_GL=egl .venv/bin/python -m pac_harness.libero_eval --smoke\n'
