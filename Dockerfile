# -------- Base (Python 3.11 + Debian slim) --------
FROM python:3.11-slim-bullseye

# OS libs for Chromium/Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget unzip gnupg2 \
    libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
    libdrm2 libgbm1 libxdamage1 libxfixes3 libxcomposite1 \
    libxrandr2 libxkbcommon0 libpango-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libgtk-3-0 fonts-liberation libwoff1 \
  && rm -rf /var/lib/apt/lists/*

# App
WORKDIR /app
COPY . /app

# Python venv inside image
ENV VENV_PATH=/opt/venv
RUN python -m venv ${VENV_PATH}
ENV PATH="${VENV_PATH}/bin:${PATH}"

# Python deps
RUN pip install --upgrade pip setuptools wheel
RUN pip install -r requirements.txt

# Playwright browsers (Chromium only)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install chromium

# Runtime env (can be overridden in Render if you want)
ENV HEADLESS=true
ENV MW_HEADLESS=1
ENV GV_HEADLESS=1
ENV JUWA_HEADLESS=1

# Expose default internal port
EXPOSE 10000
ENV PORT=10000

# Start (bind to $PORT if provided by platform)
CMD ["bash","-lc","python -m gunicorn -k gthread --threads 8 -w 1 -b 0.0.0.0:${PORT:-10000} app:app"]
