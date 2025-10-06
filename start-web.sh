#!/usr/bin/env bash
set -euo pipefail

# Activate the same venv we created at build
source .venv/bin/activate

# Runtime env for headless automation
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
export HEADLESS=true
export MW_HEADLESS=1
export GV_HEADLESS=1
export JUWA_HEADLESS=1

# Launch the app
exec python -m gunicorn -k eventlet -w 1 -b 0.0.0.0:$PORT app:app
