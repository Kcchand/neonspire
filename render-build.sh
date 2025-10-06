#!/usr/bin/env bash
set -euo pipefail

# === STEP 1: Upgrade pip safely (Render image uses PEP 668) ===
python3 -m pip install --upgrade pip --break-system-packages

# === STEP 2: Install dependencies from requirements.txt ===
python3 -m pip install -r requirements.txt --no-cache-dir --break-system-packages

# === STEP 3: Prepare Playwright ===
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
python3 -m playwright install --with-deps chromium --break-system-packages

# === STEP 4: Clean up pip cache (optional) ===
pip cache purge || true

echo "✅ Render build completed successfully."