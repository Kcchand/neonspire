#!/usr/bin/env bash
set -euo pipefail

# Make sure pip can install on Render's PEP 668 image
python3 -m pip install --upgrade pip --break-system-packages
python3 -m pip install -r requirements.txt --no-cache-dir --break-system-packages

# Put user-local scripts (gunicorn, playwright, etc.) on PATH
export PATH="$PATH:/opt/render/.local/bin"

# Tell Playwright where to cache the browser, and install Chromium
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
python3 -m playwright install --with-deps chromium

echo "✅ Build finished"