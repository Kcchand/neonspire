#!/usr/bin/env bash
set -euo pipefail

export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
export HEADLESS=true
export MW_HEADLESS=1
export GV_HEADLESS=1
export JUWA_HEADLESS=1

exec python -m gunicorn -k eventlet -w 1 -b 0.0.0.0:$PORT app:app