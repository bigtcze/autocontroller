#!/usr/bin/env bash
# Offline syntax gate: byte-compile tracked Python (no __pycache__ litter), shell
# syntax and JSON validity. Uses the filesystem view of git-tracked paths.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd -- "$ROOT"

PYCACHE="$(mktemp -d)"
trap 'rm -rf -- "$PYCACHE"' EXIT
export PYTHONPYCACHEPREFIX="$PYCACHE"

python3 --version
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "python3 >= 3.11 required" >&2
  exit 2
fi

# Tracked plus untracked-but-not-ignored, so new files are checked before commit.
mapfile -t PY_FILES < <(git ls-files --cached --others --exclude-standard -- '*.py')
if [[ ${#PY_FILES[@]} -eq 0 ]]; then
  echo "no tracked Python files found" >&2
  exit 1
fi
for file in "${PY_FILES[@]}"; do
  python3 -m py_compile -- "$file"
  echo "py_compile ok: $file"
done

for file in install.sh scripts/*.sh; do
  bash -n -- "$file"
  echo "bash -n ok: $file"
done

for file in renovate.json config.example.json; do
  python3 -m json.tool --no-ensure-ascii -- "$file" > /dev/null
  echo "json ok: $file"
done

echo "syntax checks passed"
