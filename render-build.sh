#!/usr/bin/env bash
set -euo pipefail

# --- System dependencies needed by Chromium / Playwright ---
apt-get update
apt-get install -y --no-install-recommends \
  libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
  libdrm2 libgbm1 libxdamage1 libxfixes3 libxcomposite1 \
  libxrandr2 libxkbcommon0 libpango-1.0-0 libpangocairo-1.0-0 \
  libcairo2 libgtk-3-0 fonts-liberation \
  ca-certificates curl wget xz-utils

# --- Python dependencies ---
pip install -r requirements.txt

# --- Playwright browser installation ---
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
python -m playwright install --with-deps chromium

# --- Optional cleanup to save build space ---
pip cache purge || true
