# safepay_service.py
from __future__ import annotations
import os
import json
import time
from contextlib import contextmanager
from typing import Dict, Any, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# Where to persist session cookies so we don’t re-login each time
_SAFEPAY_STATE_PATH = os.environ.get("SAFEPAY_STATE_PATH", ".safepay_state.json")

class SafePayError(Exception):
    pass


def _get_cfg(app_cfg) -> dict:
    """
    Pulls needed settings from Flask's current_app.config (what you already pass in).
    Required: SAFE_PAY_USERNAME, SAFE_PAY_PASSWORD
    Optional: SAFEPAY_LOGIN_URL, SAFEPAY_COLLECT_URL
    """
    cfg = {
        "username": (app_cfg.get("SAFE_PAY_USERNAME") or "").strip(),
        "password": (app_cfg.get("SAFE_PAY_PASSWORD") or "").strip(),
        # default paths (these work for the common merchant UI; you can adjust if your menu differs)
        "login_url": app_cfg.get("SAFEPAY_LOGIN_URL", "https://www.safepayin.com/#/mch/login"),
        "collect_url": app_cfg.get("SAFEPAY_COLLECT_URL", "https://www.safepayin.com/#/mch/order/collect"),
        "headless": bool(int(str(app_cfg.get("SAFEPAY_HEADLESS", "1")).strip() or "1")),
        "slowmo": int(str(app_cfg.get("SAFEPAY_SLOWMO_MS", "0")).strip() or 0),
        "viewport": app_cfg.get("SAFEPAY_VIEWPORT", {"width": 1280, "height": 900}),
        # maximum time we allow for the whole run
        "total_timeout_ms": int(str(app_cfg.get("SAFEPAY_TOTAL_TIMEOUT_MS", "45000")) or "45000"),
    }
    if not cfg["username"] or not cfg["password"]:
        raise SafePayError("SAFE_PAY_USERNAME or SAFE_PAY_PASSWORD not configured")
    return cfg


@contextmanager
def _browser(cfg: dict):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg["headless"], slow_mo=cfg["slowmo"])
        context = browser.new_context(viewport=cfg["viewport"])
        # Try to restore session (keeps you logged in)
        try:
            if os.path.exists(_SAFEPAY_STATE_PATH):
                context.storage_state_path = _SAFEPAY_STATE_PATH  # playwright saves on close; we’ll load manually
                with open(_SAFEPAY_STATE_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
                context = browser.new_context(storage_state=state, viewport=cfg["viewport"])
        except Exception:
            # ignore corrupt state
            pass
        page = context.new_page()
        try:
            yield page, context, browser
        finally:
            # Save state for future runs
            try:
                state = context.storage_state()
                with open(_SAFEPAY_STATE_PATH, "w", encoding="utf-8") as f:
                    f.write(state)
            except Exception:
                pass
            context.close()
            browser.close()


def _ensure_logged_in(page, cfg: dict):
    """
    If we’re still logged in, dashboard will load; otherwise we’ll land on login page.
    This function is robust: tries multiple selector fallbacks.
    """
    page.set_default_timeout(15000)

    # Quick test: go to a page that requires auth; if it loads, we’re already logged in.
    page.goto("https://www.safepayin.com/#/mch/agent", wait_until="domcontentloaded")

    # If a logout/user chip is visible, we assume we’re authenticated.
    try:
        # Some dashboards render a user chip with avatar icon
        page.get_by_text("My Agent").wait_for(timeout=3000)
        return
    except PwTimeout:
        pass  # not logged in

    # Go to explicit login page
    page.goto(cfg["login_url"], wait_until="domcontentloaded")

    # Try a bunch of common input selectors
    user_selectors = [
        "input[name='username']",
        "input[placeholder*='username' i]",
        "input[placeholder*='account' i]",
        "input[type='text']",
        "//*[@placeholder and (contains(translate(., 'USERNAM','usernam'),'user') or contains(translate(., 'ACC','acc'),'acc'))]",
    ]
    pass_selectors = [
        "input[name='password']",
        "input[placeholder*='password' i]",
        "input[type='password']",
    ]
    login_btns = [
        "button[type='submit']",
        "//button[contains(., 'Login')]",
        "//button[contains(., 'Sign in')]",
        "text=Login",
    ]

    # Fill username
    last_err = "unknown"
    for sel in user_selectors:
        try:
            page.wait_for_selector(sel, timeout=8000)
            page.fill(sel, cfg["username"])
            break
        except PwTimeout as e:
            last_err = f"username field not found via {sel}: {e}"
    else:
        raise SafePayError(f"Login failed: {last_err}")

    # Fill password
    for sel in pass_selectors:
        try:
            page.wait_for_selector(sel, timeout=8000)
            page.fill(sel, cfg["password"])
            break
        except PwTimeout as e:
            last_err = f"password field not found via {sel}: {e}"
    else:
        raise SafePayError(f"Login failed: {last_err}")

    # Click login
    clicked = False
    for sel in login_btns:
        try:
            page.click(sel, timeout=8000)
            clicked = True
            break
        except PwTimeout:
            continue
    if not clicked:
        # Press Enter as a fallback
        page.keyboard.press("Enter")

    # Wait for a post-login UI element
    try:
        page.wait_for_timeout(500)  # brief pause for navigation
        page.wait_for_load_state("networkidle", timeout=15000)
        # Something only visible after login
        page.get_by_text("Merchant Operations", exact=False).wait_for(timeout=12000)
    except PwTimeout:
        # capture screen for debugging on server
        try:
            page.screenshot(path="safepay_login_error.png")
        except Exception:
            pass
        raise SafePayError("Login did not complete (check credentials or 2FA/captcha).")


def _create_order_and_get_link(page, amount: float, cfg: dict) -> str:
    """
    Navigates to the 'Collect' page, fills the amount, submits, and extracts the pay URL.
    NOTE: Selectors below are generic and use text-based fallbacks.
    You can tweak them to match your exact SafePay screen.
    """
    # Go to Collect/Deposit page
    page.goto(cfg["collect_url"], wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=15000)

    # Typical label/placeholder patterns
    amount_selectors = [
        "input[name='amount']",
        "input[placeholder*='amount' i]",
        "//input[@type='number']",
        "//input[contains(@placeholder,'Amount') or contains(@placeholder,'amount')]",
    ]

    filled = False
    for sel in amount_selectors:
        try:
            page.wait_for_selector(sel, timeout=8000)
            # Clear then type
            page.fill(sel, "")
            page.type(sel, str(amount))
            filled = True
            break
        except PwTimeout:
            continue

    if not filled:
        # Save a screenshot for debugging
        try:
            page.screenshot(path="safepay_amount_error.png")
        except Exception:
            pass
        raise SafePayError("Could not locate amount field on Collect page")

    # Submit / Create / Generate button fallbacks
    submit_buttons = [
        "button[type='submit']",
        "//button[contains(., 'Create')]",
        "//button[contains(., 'Generate')]",
        "//button[contains(., 'Submit')]",
        "text=Create",
        "text=Generate",
        "text=Submit",
    ]

    clicked = False
    for sel in submit_buttons:
        try:
            page.click(sel, timeout=6000)
            clicked = True
            break
        except PwTimeout:
            continue

    if not clicked:
        try:
            page.screenshot(path="safepay_submit_error.png")
        except Exception:
            pass
        raise SafePayError("Could not find the Create/Submit button")

    # Wait for result panel/table to render with the payment URL
    # Common patterns: a table row with a 'Payment Link' column or a modal with a copyable URL
    url_candidates = [
        "//a[contains(@href,'http') and (contains(.,'pay') or contains(.,'cash') or contains(.,'app'))]",
        "text=/https?:\\/\\/[^\\s]+/i",
        "a[href^='http']",
        "input[readonly]",
    ]

    pay_url: Optional[str] = None
    end_time = time.time() + 20  # up to ~20s for the link to appear
    while time.time() < end_time and not pay_url:
        page.wait_for_timeout(800)
        for sel in url_candidates:
            try:
                el = page.query_selector(sel)
                if not el:
                    continue
                # Try href or value or inner text
                href = el.get_attribute("href")
                val = el.get_attribute("value")
                text = (el.inner_text() or "").strip()
                for cand in (href, val, text):
                    if cand and cand.startswith("http"):
                        pay_url = cand
                        break
                if pay_url:
                    break
            except Exception:
                continue

    if not pay_url:
        try:
            page.screenshot(path="safepay_link_error.png")
        except Exception:
            pass
        raise SafePayError("Order created but payment link was not found")

    return pay_url


def create_cashapp_invoice(amount: float, memo: str, app_cfg) -> Dict[str, Any]:
    """
    Public function your Flask code calls.
    Returns: {"pay_url": "...", "provider_order_id": "", "backend_url": ""}

    If it can’t find elements, it raises SafePayError with a human-readable message.
    """
    cfg = _get_cfg(app_cfg)
    # Basic sanity
    try:
        amt = float(amount)
        if amt <= 0:
            raise ValueError
    except Exception:
        raise SafePayError("Invalid amount")

    with _browser(cfg) as (page, context, browser):
        # Total watchdog timeout
        start = time.time()
        try:
            _ensure_logged_in(page, cfg)
            if (time.time() - start) * 1000 > cfg["total_timeout_ms"]:
                raise SafePayError("Timeout during login")

            pay_url = _create_order_and_get_link(page, amt, cfg)
            if (time.time() - start) * 1000 > cfg["total_timeout_ms"]:
                raise SafePayError("Timeout creating order")

            # Optional: Some UIs show an order ID near the link
            provider_order_id = ""
            try:
                # crude text scrape near the link
                provider_order_id = page.locator("text=Order").first.inner_text(timeout=1500)
            except Exception:
                provider_order_id = ""

            return {
                "pay_url": pay_url,
                "backend_url": "",           # not available from UI
                "provider_order_id": provider_order_id,
            }

        except SafePayError:
            raise
        except PwTimeout as e:
            raise SafePayError(f"Timeout in SafePay automation: {e}")
        except Exception as e:
            raise SafePayError(f"SafePay automation failed: {e}")