#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import playwright.sync_api' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python -m playwright install chromium
status=0
.venv/bin/python watcher.py || status=$?
if [ "$status" -ne 0 ]; then
  echo 'Could not complete the run. Check the message above and README.md.'
fi
read -r -p 'Press Enter to close.'
exit "$status"
