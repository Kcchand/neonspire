#!/usr/bin/env bash
set -euo pipefail

# 1) Create & activate our own virtualenv (avoid PEP 668)
python3 -m venv .venv
source .venv/bin/activate

# 2) Upgrade pip and install Python deps into .venv
pip install --upgrade pip
pip install -r requirements.txt

# 3) Install Playwright Chromium into Render's cache
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
python -m playwright install --with-deps chromium

echo "✅ Build complete!"
