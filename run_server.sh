#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PY="$SCRIPT_DIR/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi

if [ -z "$PY" ]; then
  echo "[ERR] python3 not found"
  exit 1
fi

exec "$PY" "$SCRIPT_DIR/run_api.py"
