#!/usr/bin/env bash
set -euo pipefail

# Use the venv you created during the build
source .venv/bin/activate

# Point runtime to the baked-in browser dir
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright
export HEADLESS=true
export MW_HEADLESS=1
export GV_HEADLESS=1
export JUWA_HEADLESS=1

# Start Flask app using Gunicorn thread workers (Python 3.13-safe)
exec python -m gunicorn -k gthread --threads 8 -w 1 -b 0.0.0.0:$PORT app:app