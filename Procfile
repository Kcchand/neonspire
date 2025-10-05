web: PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright \
     HEADLESS=true MW_HEADLESS=1 GV_HEADLESS=1 JUWA_HEADLESS=1 \
     gunicorn -k eventlet -w 1 -b 0.0.0.0:$PORT app:app