#!/usr/bin/env bash
set -euo pipefail

# 1) pip inside Render (PEP 668 safe)
python -m pip install --upgrade pip
python -m pip install --no-cache-dir --break-system-packages -r requirements.txt

# 2) Install Playwright Chromium into a persistent path
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
python -m playwright install --with-deps chromium