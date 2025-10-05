#!/usr/bin/env bash
set -euo pipefail

# 1) OS packages for Chromium
apt-get update
apt-get install -y --no-install-recommends \
  libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
  libdrm2 libgbm1 libxdamage1 libxfixes3 libxcomposite1 \
  libxrandr2 libxkbcommon0 libpango-1.0-0 libpangocairo-1.0-0 \
  libcairo2 libgtk-3-0 fonts-liberation \
  ca-certificates curl wget xz-utils

# 2) Install Python deps
pip install -r requirements.txt

# 3) Configure Playwright path for Render
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright

# 4) Install Chromium
python -m playwright install --with-deps chromium

# Optional cleanup
pip cache purge || true