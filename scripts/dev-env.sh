#!/usr/bin/env bash
# Create (or refresh) this worktree's development environment: .venv with Python >= 3.11 and the package
# installed editable with its [dev] extra, so `nanosoc-rm` is on PATH inside the venv. Idempotent: run it again
# after pulling; it reuses .venv and reinstalls the package.
#
#   scripts/dev-env.sh                 # uses $PYTHON, else /usr/bin/python3.11, else python3.11 on PATH
#
# Then run the tests with the command it prints. Never source a set_env.sh for this repo: it needs none.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

py="${PYTHON:-}"
if [ -z "$py" ]; then
    if [ -x /usr/bin/python3.11 ]; then py=/usr/bin/python3.11; else py="$(command -v python3.11 || true)"; fi
fi
if [ -z "$py" ] || ! "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "dev-env: need Python >= 3.11 (set PYTHON=/path/to/python3.11)" >&2
    exit 3
fi

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    rm -rf .venv
    "$py" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e '.[dev]'

grep -qxF '.venv/' .gitignore 2>/dev/null || grep -qxF '.venv' .gitignore 2>/dev/null || echo '.venv/' >> .gitignore

echo "dev-env: $(.venv/bin/python --version) in $root/.venv; $(.venv/bin/nanosoc-rm --version)"
echo "Run the tests (off the service VM, live tests deselected):"
echo "  cd $root && .venv/bin/python -m pytest -m \"not live\" -n 6 tests"
