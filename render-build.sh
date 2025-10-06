#!/usr/bin/env bash
set -euo pipefail

echo "🔧 Installing Python dependencies..."
python3 -m pip install --upgrade pip --break-system-packages
python3 -m pip install -r requirements.txt --no-cache-dir --break-system-packages

# Fix PATH so Render can find playwright/gunicorn later
export PATH="$PATH:/opt/render/.local/bin"

# Set Playwright cache location
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright

# ✅ Install only Chromium (no system packages)
echo "🌐 Installing Playwright Chromium..."
python3 -m playwright install chromium --force

echo "✅ Build complete!"