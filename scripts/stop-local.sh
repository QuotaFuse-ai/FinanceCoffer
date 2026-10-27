#!/usr/bin/env sh
# Stop only the API process started by start-local.sh.
# Set PYTHON to a Python 3.11+ interpreter. Machine-specific settings belong in
# scripts/local-env.local.sh, which is gitignored.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$here/local-env.local.sh" ]; then . "$here/local-env.local.sh"; fi
if [ -z "${PYTHON:-}" ]; then
  echo "ERROR: PYTHON is not set. Point it at a Python 3.11+ interpreter." >&2
  exit 2
fi
exec "$PYTHON" "$here/local_runner.py" stop
