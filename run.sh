#!/usr/bin/env bash
# Starts the Document Redactor on http://127.0.0.1:1961
set -euo pipefail
cd "$(dirname "$0")"
source packaging/find_python.sh
if ! python_ok "$(uname -m)" .venv/bin/python; then
  BASE="$(find_python "$(uname -m)")" || { echo "$PYTHON_HELP"; exit 1; }
  echo "First run: setting up a private Python environment..."
  rm -rf .venv
  "$BASE" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m redactor "$@"
