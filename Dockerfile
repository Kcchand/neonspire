FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HEADLESS=true \
    MW_HEADLESS=1 \
    GV_HEADLESS=1 \
    JUWA_HEADLESS=1

# System libs needed by Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
  libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
  libdrm2 libgbm1 libxdamage1 libxfixes3 libxcomposite1 \
  libxrandr2 libxkbcommon0 libpango-1.0-0 libpangocairo-1.0-0 \
  libcairo2 libgtk-3-0 fonts-liberation ca-certificates wget \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt /app/
RUN python -m pip install --upgrade pip && \
    pip install -r requirements.txt

# Install Playwright Chromium inside the image
RUN python -m playwright install --with-deps chromium

# Copy the app
COPY . /app

# Expose the app port
EXPOSE 8000

# Start the app (threaded gunicorn; no eventlet)
CMD ["bash","-lc","gunicorn -k gthread --threads 8 -w 1 -b 0.0.0.0:8000 app:app"]
