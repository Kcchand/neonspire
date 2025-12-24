# rpa/browser.py
from contextlib import contextmanager
import os
from playwright.sync_api import sync_playwright

@contextmanager
def browser_context(headless: bool | None = None):
    """
    Minimal Playwright context manager w/ env overrides:
      GV_HEADLESS (true/false), GV_SLOWMO_MS
    """
    env_headless = (os.getenv("GV_HEADLESS", "true").lower() != "false")
    use_headless = env_headless if headless is None else headless
    slowmo = int(os.getenv("GV_SLOWMO_MS", "0") or "0")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=use_headless,
        slow_mo=slowmo,
        args=["--no-sandbox"],
    )
    ctx = browser.new_context(viewport={"width": 1366, "height": 900})
    try:
        yield ctx
    finally:
        try: ctx.close()
        except Exception: pass
        try: browser.close()
        except Exception: pass
        pw.stop()