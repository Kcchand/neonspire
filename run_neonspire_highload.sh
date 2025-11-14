#!/usr/bin/env bash
# High-load Gunicorn launcher for NeonSpire (no Docker)

set -e

# Always run from project root
cd "$(dirname "$0")"

# Activate venv (adjust if your venv path is different)
source venv/bin/activate

# Optional: set environment for production-ish mode
export FLASK_ENV=production
export NEONSPIRE_HIGHLOAD=1

# Run Gunicorn with threaded workers on PORT 5100 (NOT 5000!)
gunicorn \
  -w 4 \
  -k gthread \
  --threads 40 \
  --worker-class gthread \
  --timeout 60 \
  --bind 0.0.0.0:5100 \
  "app:create_app()"