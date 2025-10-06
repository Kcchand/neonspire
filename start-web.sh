#!/usr/bin/env bash
set -euo pipefail

# Runtime environment (Render-safe)
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
export HEADLESS=true
export MW_HEADLESS=1
export GV_HEADLESS=1
export JUWA_HEADLESS=1

# Start Flask app using Gunicorn thread workers (eventlet removed)
exec python -m gunicorn -k gthread --threads 8 -w 1 -b 0.0.0.0:$PORT app:app