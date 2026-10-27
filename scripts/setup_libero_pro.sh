#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
LIBERO_BASE_VENV="${LIBERO_BASE_VENV:-.venv}"
LIBERO_PRO_VENV="${LIBERO_PRO_VENV:-.venv-libero-pro}"
LIBERO_PRO_REPO="${LIBERO_PRO_REPO:-.deps/LIBERO-PRO}"
LIBERO_PRO_DATA="${LIBERO_PRO_DATA:-.deps/libero-pro-data}"
LIBERO_PRO_REVISION=eafdb809426b13153aa1e4c42d6601844217dfec
if [ ! -x "$LIBERO_BASE_VENV/bin/python" ]; then
  printf 'Prepare simulator dependencies first: bash scripts/setup_libero.sh\n' >&2
  exit 1
fi
if [ ! -d "$LIBERO_PRO_REPO/.git" ]; then
  git init "$LIBERO_PRO_REPO"
  git -C "$LIBERO_PRO_REPO" remote add origin https://github.com/Zxy-MLlab/LIBERO-PRO.git
  git -C "$LIBERO_PRO_REPO" fetch --depth 1 origin "$LIBERO_PRO_REVISION"
  git -C "$LIBERO_PRO_REPO" checkout --detach FETCH_HEAD
fi
if [ "$(git -C "$LIBERO_PRO_REPO" rev-parse HEAD)" != "$LIBERO_PRO_REVISION" ]; then
  printf 'LIBERO-Pro checkout differs from the pinned revision; select another LIBERO_PRO_REPO directory.\n' >&2
  exit 1
fi
if [ ! -x "$LIBERO_PRO_VENV/bin/python" ]; then
  "$LIBERO_BASE_VENV/bin/python" -m venv --without-pip "$LIBERO_PRO_VENV"
fi
# Reuse installed simulator dependencies read-only, while resolving LIBERO solely
# from the Pro checkout. Do not install Pro over the classic LIBERO package.
"$LIBERO_BASE_VENV/bin/python" - "$LIBERO_PRO_VENV" "$LIBERO_PRO_REPO" "$LIBERO_PRO_DATA" <<'PY'
from pathlib import Path
import subprocess
import sys
import sysconfig
import yaml
venv, repo, data = (Path(s).resolve() for s in sys.argv[1:])
project = Path.cwd()
site = Path(subprocess.check_output([str(venv/'bin/python'), '-c',
    'import sysconfig; print(sysconfig.get_paths()["purelib"])'], text=True).strip())
site.mkdir(parents=True, exist_ok=True)
(site/'pac_libero_pro.pth').write_text('\n'.join([str(repo), str(project), sysconfig.get_paths()['purelib']])+'\n')
config = project/'maintenance/libero-pro-config'
config.mkdir(parents=True, exist_ok=True)
package = repo/'libero/libero'
data.mkdir(parents=True, exist_ok=True)
(data/'datasets').mkdir(exist_ok=True)
(config/'config.yaml').write_text(yaml.safe_dump({'benchmark_root':str(package),
    'bddl_files':str(data/'bddl_files'), 'init_states':str(data/'init_files'),
    'assets':str(package/'assets'), 'datasets':str(data/'datasets')}))
PY
"$LIBERO_PRO_VENV/bin/python" -m pac_harness.libero_pro_setup --repo "$LIBERO_PRO_REPO" --data "$LIBERO_PRO_DATA"
printf '\nReady: bash scripts/run_libero_pro.sh --check\n'
