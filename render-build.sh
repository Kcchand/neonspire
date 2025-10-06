#!/usr/bin/env bash
set -euo pipefail

# Create and use our own venv (avoids PEP 668)
python3 -m venv .venv
source .venv/bin/activate

# Python deps into the venv
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright **browsers only** (NO --with-deps on Render)
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
python -m playwright install chromium

echo "✅ Build complete!"
