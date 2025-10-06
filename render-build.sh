#!/usr/bin/env bash
set -euo pipefail

# Use Render's virtualenv (it's already active during build)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Install Playwright browsers to a persistent path
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
python -m playwright install --with-deps chromium