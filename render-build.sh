#!/usr/bin/env bash
set -euo pipefail

# Upgrade pip that belongs to this Python
python3 -m pip install --upgrade pip

# Install deps (PEP 668 safe)
python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

# Install Playwright Chromium to a persistent path
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
python3 -m playwright install --with-deps chromium