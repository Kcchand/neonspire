"""
UltraPanda UI Bot (Playwright)
- login
- create user (auto-retry on duplicate, auto-sanitize username to alphanumeric 7–16)
- recharge (Search User -> Player account -> Set Score +amount)
- redeem   (Search User -> Player account -> Set Score -amount)

ENV (.env in project root or automation/):
  ULTRAPANDA_BASE_URL=https://ht.ultrapanda.mobi
  ULTRAPANDA_USERNAME=rob1820
  ULTRAPANDA_PASSWORD=rob1820
  HEADLESS=false
  SLOW_MO_MS=0

Optional defaults for generated players:
  ULTRAPANDA_USERNAME_PREFIX=up
  ULTRAPANDA_USERNAME_SUFFIX=
  ULTRAPANDA_USERNAME_LEN=10
  ULTRAPANDA_DEFAULT_PLAYER_PASSWORD=Abc12345

CLI:
  python -m automation.ultrapanda_ui_bot login
  python -m automation.ultrapanda_ui_bot create [account?] [password?]
  python -m automation.ultrapanda_ui_bot recharge <account> <amount> [remark?]
  python -m automation.ultrapanda_ui_bot redeem  <account> <amount> [remark?]
  python -m automation.ultrapanda_ui_bot test
"""

from __future__ import annotations
import os, sys, asyncio, random, string, re, json, math
from typing import Optional, Tuple
from pathlib import Path

# ---------------- .env loader ----------------
try:
    from dotenv import load_dotenv, find_dotenv
except Exception:
    load_dotenv = None
    find_dotenv = None

def _load_env():
    if not load_dotenv:
        print("[ultrapanda_ui_bot] WARNING: python-dotenv not installed; skipping .env load")
        return
    here = Path(__file__).resolve()
    tried = []
    for p in (here.parents[1] / ".env", here.parent / ".env"):
        tried.append(str(p))
        if p.exists():
            load_dotenv(p, override=True)
            print(f"[ultrapanda_ui_bot] .env loaded from {p}")
            return
    found = find_dotenv(usecwd=True) if find_dotenv else ""
    if found:
        load_dotenv(found, override=True)
        print(f"[ultrapanda_ui_bot] .env loaded from {found}")
    else:
        print(f"[ultrapanda_ui_bot] .env not found (tried {', '.join(tried)}); relying on OS env")

_load_env()

# ---------------- Config ----------------
BASE = (os.getenv("ULTRAPANDA_BASE_URL", "https://ht.ultrapanda.mobi") or "").rstrip("/")
ADMIN_USER = os.getenv("ULTRAPANDA_USERNAME", "")
ADMIN_PASS = os.getenv("ULTRAPANDA_PASSWORD", "")

HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1","true","yes","on")
SLOW_MO_MS = int(os.getenv("SLOW_MO_MS", "0") or "0")

# IMPORTANT: underscores are NOT allowed by UP ("Must be combination of alphanumeric!")
UP_PREFIX = os.getenv("ULTRAPANDA_USERNAME_PREFIX", "up")
UP_SUFFIX = os.getenv("ULTRAPANDA_USERNAME_SUFFIX", "")  # sanitized away anyway
UP_LEN    = int(os.getenv("ULTRAPANDA_USERNAME_LEN", "10") or "10")
UP_DEFAULT_PWD = os.getenv("ULTRAPANDA_DEFAULT_PLAYER_PASSWORD", "Abc12345")

LOGIN_URL  = f"{BASE}/#/login"
HOME_URL   = f"{BASE}/#/index"
UM_URL     = f"{BASE}/#/manage-user/account"
SEARCH_URL = f"{BASE}/#/manage-user/search"

# ---------------- Playwright ----------------
from playwright.async_api import async_playwright, Page, Locator

def _rand(n:int)->str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))

def _sanitize_username(name:str, min_len:int=7, max_len:int=16)->str:
    # keep alphanumeric only
    clean = re.sub(r"[^a-zA-Z0-9]", "", name or "")
    if len(clean) < min_len:
        clean += _rand(min_len - len(clean))
    if len(clean) > max_len:
        clean = clean[:max_len]
    return clean

async def _launch() -> Tuple:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"]
    )
    ctx = await browser.new_context(
        viewport={"width": 1368, "height": 900},
        ignore_https_errors=True,
        java_script_enabled=True,
    )
    page = await ctx.new_page()
    # Keep default timeout moderate; we rely on specific waits for speed
    page.set_default_timeout(10_000)
    return pw, browser, ctx, page

async def _close(bundle: Tuple):
    if not bundle: return
    pw, b, ctx, page = bundle
    for fn in (ctx.close, b.close, pw.stop):
        try: await fn()
        except Exception: pass

# ---------------- Helpers ----------------
async def _dismiss_any_blocking_dialog(page: Page):
    """
    Close "Hint/confirm" overlays that block clicks (seen in your screenshots).
    """
    try:
        # try common buttons first
        for sel in (
            "button:has-text('confirm')",
            "button:has-text('Confirm')",
            "button:has-text('OK')",
            "button:has-text('Ok')",
        ):
            btn = page.locator(sel).first
            if await btn.count():
                try:
                    await btn.click()
                    await page.wait_for_timeout(100)
                except Exception:
                    pass

        # close any modal close buttons
        for sel in (".ant-modal .ant-modal-close", ".el-dialog__headerbtn"):
            x = page.locator(sel).first
            if await x.count():
                try:
                    await x.click()
                    await page.wait_for_timeout(100)
                except Exception:
                    pass

        # some tenants wrap with a modal wrapper that intercepts clicks
        wrap = page.locator(".m-remote-login-alert, .el-dialog__wrapper").first
        if await wrap.count():
            ok = wrap.locator("button:has-text('confirm'), button:has-text('OK'), button:has-text('Confirm')").first
            if await ok.count():
                try:
                    await ok.click()
                    await page.wait_for_timeout(100)
                except Exception:
                    pass
    except Exception:
        pass

async def _wait_spa(page: Page, url_sub: str, timeout: int = 12_000):
    try:
        if url_sub not in page.url:
            await page.wait_for_url(f"**{url_sub}**", timeout=timeout)
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            await page.wait_for_timeout(200)
    except Exception:
        # still continue; SPA sometimes doesn't emit clean events
        await page.wait_for_timeout(200)

async def _safe_click(page: Page, text: str, timeout: int = 3000) -> bool:
    for sel in (
        f"role=link[name='{text}']",
        f"role=button[name='{text}']",
        f"text={text}",
        f"a:has-text('{text}')",
        f"button:has-text('{text}')",
        f"li:has-text('{text}')",
        f"span:has-text('{text}')",
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False

async def _scroll_until_visible(page: Page, locator: Locator, steps: int = 18, dy: int = 800) -> bool:
    for _ in range(steps):
        if await locator.count():
            try:
                await locator.scroll_into_view_if_needed()
                return True
            except Exception:
                pass
        await page.mouse.wheel(0, dy)
        await asyncio.sleep(0.12)
    return False

# ---------------- Toast/Error helpers ----------------
async def _read_any_error_text(page: Page) -> str:
    for sel in (
        ".el-message--error",
        ".ant-message-error",
        ".ant-notification-notice-error",
        ".el-form-item__error",
        ".ant-form-item-explain-error",
        ".el-message__content",
    ):
        loc = page.locator(sel)
        if await loc.count():
            try:
                t = (await loc.first.inner_text()).strip()
                if t:
                    return t
            except Exception:
                pass
    return ""

async def _wait_success_or_point_change(page: Page, prev_point: Optional[int], timeout_ms: int = 8000) -> bool:
    """
    Confirm success by either:
      1) seeing a success toast, OR
      2) seeing the 'Current Point' number update on the Set Score panel.
    """
    # success toast?
    for _ in range(max(1, timeout_ms // 200)):
        if await page.locator(".el-message--success, .ant-message-success").count():
            return True

        # check point change (Current Point X)
        try:
            lbl = page.locator("text=Current Point").first
            if await lbl.count():
                # number is usually near it
                parent = lbl.locator("xpath=..")
                number = None
                for probe in (
                    parent.locator("text=/^\\s*\\d+\\s*$/"),
                    page.locator("text=/^\\s*\\d+\\s*$/").nth(0),
                ):
                    if await probe.count():
                        raw = (await probe.first.text_content()) or ""
                        m = re.search(r"\d+", raw)
                        if m:
                            number = int(m.group(0))
                            break
                if number is not None and prev_point is not None and number != prev_point:
                    return True
        except Exception:
            pass

        await asyncio.sleep(0.2)
    return False

# --- Helpers used by _submit_set_score ---------------------------------------

async def _read_current_point(page: Page) -> Optional[int]:
    """Reads the integer Current Point shown on the Set Score panel."""
    try:
        lab = page.locator("text=Current Point").first
        if not await lab.count():
            return None
        # the number is typically in the next sibling (badge-like).
        # grab nearest number after the label
        container = lab.locator("xpath=..")
        txt = (await container.inner_text() or "").strip()
        # fallback: scan whole panel line
        if not re.search(r"\b\d+\b", txt):
            txt = (await page.locator("section,div").filter(has_text="Current Point").first.inner_text() or "").strip()
        m = re.search(r"Current\s*Point\s*([\-]?\d+)", txt, flags=re.I)
        if not m:
            m = re.search(r"\b(\d+)\b", txt)
        return int(m.group(1)) if m else None
    except Exception:
        return None

async def _panel_gone(page: Page) -> bool:
    """True if the Set Score panel is no longer visible (some skins auto-close)."""
    try:
        title = page.locator("text=Set Score").first
        return not await title.is_visible()
    except Exception:
        return False

async def _read_any_error_text(page: Page) -> str:
    """Extracts any visible inline or toast error text."""
    for sel in (
        ".el-message--error, .ant-message-error",
        ".el-form-item__error, .ant-form-item-explain-error",
        ".el-message-box__message, .ant-modal .ant-modal-body",
    ):
        loc = page.locator(sel).first
        if await loc.count():
            try:
                t = (await loc.text_content() or "").strip()
                if t:
                    return t
            except Exception:
                pass
    return ""

async def _wait_success_or_point_change(page: Page, prev_point: Optional[int], timeout_ms: int = 12000) -> tuple[bool, Optional[int]]:
    """Waits for either a success toast, a point change, or panel auto-close."""
    deadline = timeout_ms / 1000.0
    step = 0.25
    waited = 0.0
    while waited < deadline:
        # toast success?
        if await page.locator(".el-message--success, .ant-message-success").count():
            return True, await _read_current_point(page)
        # point changed?
        now = await _read_current_point(page)
        if prev_point is not None and now is not None and now != prev_point:
            return True, now
        # panel auto-closed?
        if await _panel_gone(page):
            return True, now
        await asyncio.sleep(step)
        waited += step
    return False, await _read_current_point(page)

# --- Final submit function ----------------------------------------------------

async def _submit_set_score(page: Page, amount: float, remark: str = "") -> dict:
    """Submits Set Score and confirms success robustly (toast OR point change OR panel close)."""
    # record current point first
    prev_point = await _read_current_point(page)

    # normalize amount string (avoid '10.0' when not needed)
    amt_val = int(amount) if float(amount).is_integer() else float(amount)
    amt_str = str(amt_val)

    # amount field
    amt = page.locator("input[placeholder*='ie 100' i], input[type='number']").first
    await amt.scroll_into_view_if_needed()
    await amt.click()
    await amt.fill("")         # clear
    await page.keyboard.type(amt_str)  # typing triggers oninput in this UI

    # optional remark (only if visible to avoid timeouts)
    if remark:
        rem = page.locator("textarea").first
        if await rem.count():
            # only touch if visible & enabled; otherwise skip
            try:
                if await rem.is_visible():
                    await rem.click()
                    await rem.fill("")
                    await page.keyboard.type(remark)
            except Exception:
                pass

    # primary OK in the form
    ok1 = page.locator("form button:has-text('OK'), button:has-text('OK')").first
    await ok1.scroll_into_view_if_needed()
    await ok1.click()

    # secondary confirm dialog(s), if any
    for _ in range(3):
        dialog_ok = page.locator(
            ".el-message-box__btns button:has-text('OK'), "
            ".ant-modal .ant-btn-primary:has-text('OK'), "
            ".el-dialog__wrapper button:has-text('OK')"
        ).first
        if await dialog_ok.count():
            try:
                await dialog_ok.click()
                await asyncio.sleep(0.25)
            except Exception:
                pass

    # wait for success signal
    ok, now_point = await _wait_success_or_point_change(page, prev_point, timeout_ms=12000)
    if ok:
        return {
            "ok": True,
            "amount": float(amt_val),
            "current_point": now_point,
            "prev_point": prev_point,
        }

    # if we’re here, no explicit success. surface any visible error.
    err = await _read_any_error_text(page)
    raise RuntimeError(err or "No success confirmation")

# ---------------- Login ----------------
async def up_login(page: Page) -> dict:
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await _wait_spa(page, "#/login", 8000)

        # Find inputs robustly
        user_in = None
        pass_in = None
        for loc in (
            page.locator("input[placeholder*='account' i]").first,
            page.locator("input[type='text']").first,
        ):
            if await loc.count():
                user_in = loc
                break
        for loc in (
            page.locator("input[type='password']").first,
            page.locator("input[placeholder*='password' i]").first,
        ):
            if await loc.count():
                pass_in = loc
                break

        # If login inputs are not present but we're already in #/index, accept it
        if not (user_in and pass_in):
            await _wait_spa(page, "#/index", 6000)
            await _dismiss_any_blocking_dialog(page)
            if "#/index" in page.url:
                return {"ok": True, "stage": "login", "url": page.url}
            return {"ok": False, "stage": "prelogin", "error": "username/password input not found"}

        await user_in.click()
        await user_in.fill(ADMIN_USER)
        await pass_in.click()
        await pass_in.fill(ADMIN_PASS)

        # Click Login button
        for sel in ("button:has-text('Login')", "button[type='submit']"):
            b = page.locator(sel).first
            if await b.count():
                await b.click()
                break

        await _wait_spa(page, "#/index", 20000)
        await _dismiss_any_blocking_dialog(page)

        if "#/index" in page.url:
            return {"ok": True, "stage": "login", "url": page.url}
        return {"ok": False, "stage": "login", "error": "did not reach index", "url": page.url}
    except Exception as e:
        return {"ok": False, "stage": "login", "error": str(e)}

# ---------------- Navigation ----------------
async def _open_user_management(page: Page):
    # ensure on home shell
    if "#/index" not in page.url:
        await page.goto(HOME_URL, wait_until="domcontentloaded")
    await _wait_spa(page, "#/index", 8000)
    await _dismiss_any_blocking_dialog(page)

    # open "User management" in left nav
    await _safe_click(page, "User management")
    await page.wait_for_timeout(120)

async def _goto_players_list(page: Page):
    # left nav: User management -> User Management (the page with Players list + Add Player)
    await _open_user_management(page)
    ok = False
    for sel in (
        "a:has-text('User Management')",
        "button:has-text('User Management')",
        "li:has-text('User Management')",
        "span:has-text('User Management')",
    ):
        loc = page.locator(sel).first
        if await loc.count():
            try:
                await loc.click()
                ok = True
                break
            except Exception:
                pass
    if not ok:
        await page.goto(UM_URL, wait_until="domcontentloaded")
    await _wait_spa(page, "#/manage-user/account", 10000)

async def _goto_search_user(page: Page):
    # left nav: User management -> Search User
    await _open_user_management(page)
    ok = False
    for sel in (
        "a:has-text('Search User')",
        "button:has-text('Search User')",
        "li:has-text('Search User')",
        "span:has-text('Search User')",
    ):
        loc = page.locator(sel).first
        if await loc.count():
            try:
                await loc.click()
                ok = True
                break
            except Exception:
                pass
    if not ok:
        await page.goto(SEARCH_URL, wait_until="domcontentloaded")
    await _wait_spa(page, "#/manage-user/search", 10000)
    await _dismiss_any_blocking_dialog(page)

# ---------------- Add Player page (NOT a modal) ----------------
async def _open_add_player_form(page: Page):
    # Make sure we're on the User Management (players list) page
    await _goto_players_list(page)

    add = page.locator("a:has-text('Add Player'), button:has-text('Add Player')").first
    if not await add.count():
        raise RuntimeError("Could not find 'Add Player' on User Management")
    await add.click()

    # After click, it renders an editor form inline; wait for username/password inputs
    form_ready = page.locator(
        "input[placeholder*='account name' i], input[placeholder*='user' i], input[type='password']"
    )
    await form_ready.first.wait_for(timeout=10000)

# ---------------- Create user ----------------
async def create_user(page: Page, account: Optional[str], password: Optional[str]) -> dict:
    password = password or UP_DEFAULT_PWD

    # construct and sanitize base name (no underscores, 7–16)
    base = account or (UP_PREFIX + _rand(max(7, UP_LEN)) + UP_SUFFIX)
    base = _sanitize_username(base)

    last_err = ""
    attempts = 0
    while attempts < 5:
        attempts += 1
        candidate = base if attempts == 1 else _sanitize_username(base + _rand(random.randint(3,5)))

        await _open_add_player_form(page)

        # username input – UltraPanda placeholder is like "Player’s account name (7-16 characters)"
        uname = None
        for sel in (
            "input[placeholder*='account name' i]",
            "input[placeholder*='user' i]",
            "form input[type='text']",
            "input[type='text']",
        ):
            loc = page.locator(f"{sel}:not([disabled])").first
            if await loc.count():
                uname = loc
                break
        if not uname:
            return {"ok": False, "created": False, "error": "Cannot find username field"}

        pw_inputs = page.locator("input[type='password']")
        if await pw_inputs.count() == 0:
            return {"ok": False, "created": False, "error": "Password fields not found"}

        # fill
        await uname.click()
        await uname.fill("")
        await uname.type(candidate)

        await pw_inputs.nth(0).fill("")
        await pw_inputs.nth(0).type(password)
        if await pw_inputs.count() >= 2:
            await pw_inputs.nth(1).fill("")
            await pw_inputs.nth(1).type(password)

        # Click OK / Save
        clicked = False
        for sel in (
            "button:has-text('OK')",
            "button:has-text('Ok')",
            "button:has-text('Confirm')",
            "button:has-text('Save')",
        ):
            b = page.locator(sel).first
            if await b.count():
                await b.click()
                clicked = True
                break
        if not clicked:
            return {"ok": False, "created": False, "error": "Submit button not found"}

        # Wait for success toast or the field error to appear
        success = False
        for _ in range(30):
            if await page.locator(".el-message--success, .ant-message-success").count():
                success = True
                break
            # inline username error appears immediately if invalid
            inline_err = await _read_any_error_text(page)
            if inline_err:
                last_err = inline_err
                break
            await asyncio.sleep(0.2)

        if success:
            return {"ok": True, "created": True, "account": candidate, "attempts": attempts}

        # Retry on duplicate/used name or invalid username pattern
        if last_err:
            low = last_err.lower()
            if any(k in low for k in ("already", "exist", "used", "duplicate")) or "alphanumeric" in low:
                base = _sanitize_username(base)
                continue

        # unknown state -> one soft retry
        base = _sanitize_username(base)
        last_err = last_err or "unknown UI state"
        continue

    return {"ok": False, "created": False, "account": base, "error": last_err or "unable to create user"}

# ---------------- Player search + Set Score ----------------
async def _open_set_score_via_search_user(page: Page, account: str):
    # EXACT flow in your screenshots:
    # User management -> Search User -> Player account -> search -> scroll to "Set Score" -> click
    await _goto_search_user(page)

    # Select "Player account" radio
    radio = page.locator("label:has-text('Player account'), span:has-text('Player account')").first
    if await radio.count():
        try:
            await radio.click()
        except Exception:
            pass

    # Search box + OK
    box = None
    for sel in (
        "input[placeholder*='search here' i]",
        "input[placeholder*='account' i]",
        "input[placeholder*='user' i]",
    ):
        loc = page.locator(sel).first
        if await loc.count():
            box = loc
            break
    if not box:
        raise RuntimeError("Search input not found")
    await box.fill("")
    await box.type(account)
    okb = page.locator("button:has-text('OK')").first
    await okb.click()

    # wait for table with the player row to appear
    row = page.locator("table tr").filter(has_text=account).first
    await row.wait_for(timeout=12_000)

    # Scroll until "Set Score" button shows up (bottom card is lazy-rendered)
    set_btn = page.locator("button:has-text('Set Score'), a:has-text('Set Score')").first
    await _scroll_until_visible(page, set_btn, steps=20, dy=900)
    await set_btn.wait_for(timeout=10_000)
    await set_btn.click()

    # Wait the Set Score input to appear
    inp = page.locator("input[placeholder*='ie 100' i], input[type='number']").first
    await inp.wait_for(timeout=10_000)

# --- helpers (put these above _submit_set_score) ---
async def _click_all_ok_confirms(page: Page):
    # Click any follow-up confirm dialogs (Element-UI / Ant design)
    for _ in range(4):
        dlg_ok = page.locator(
            ".el-message-box__btns button:has-text('OK'), "
            ".el-dialog__wrapper button:has-text('OK'), "
            ".ant-modal .ant-btn-primary:has-text('OK'), "
            ".ant-modal button:has-text('OK')"
        ).first
        if await dlg_ok.count():
            try:
                await dlg_ok.click()
                await asyncio.sleep(0.25)
                continue
            except Exception:
                pass
        break

async def _ensure_commit(page: Page, locator_str: str):
    # Blur/commit the input to trigger form validation
    inp = page.locator(locator_str).first
    try:
        await inp.press("Enter")
    except Exception:
        pass
    try:
        await inp.press("Tab")
    except Exception:
        pass
    await asyncio.sleep(0.15)

# --- REPLACE your existing _submit_set_score with this one ---
async def _submit_set_score(page: Page, amount: float, remark: str = "") -> dict:
    """
    Submit Set Score reliably:
      - Target the OK inside the Set Score form (not the Search User OK).
      - Handle optional secondary confirm dialog.
      - Consider success either via toast or Current Point change.
    """
    # --- snapshot current point (for change detection) ---
    try:
        prev_point = await _read_current_point(page)
    except Exception:
        prev_point = None

    # UltraPanda prefers integers; keep decimals if provided explicitly
    amt_val = int(amount) if float(amount).is_integer() else float(amount)
    amt_str = str(amt_val)

    # Find the Set Score amount input and its owning <form>
    amt_input = page.locator("input[placeholder*='ie 100' i], input[type='number']").first
    await amt_input.wait_for(timeout=10_000)
    await amt_input.scroll_into_view_if_needed()
    await amt_input.click()
    await amt_input.fill("")
    await page.keyboard.type(amt_str)

    # Resolve the closest form to scope later actions (so we don't hit the page-level OK)
    form = amt_input.locator("xpath=ancestor::form[1]")
    # Some tenants wrap controls without a <form>; in that case fall back to the panel that contains "Set Score"
    if not await form.count():
        form = page.locator("section:has-text('Set Score'), .el-card:has-text('Set Score'), div:has(> *:has-text('Set Score'))").first

    # Optional remark: only interact if the field is visible to avoid Playwright "not visible" timeouts
    if remark:
        rem = form.locator("textarea, textarea[placeholder]").first
        if await rem.count():
            try:
                await rem.scroll_into_view_if_needed()
                await rem.fill("")  # some skins require clearing first
                await rem.type(remark)
            except Exception:
                # If the remarks box is collapsed/hidden in this skin, just skip it
                pass

    # Click the OK INSIDE THE FORM (not the top Search User OK)
    ok_in_form = form.locator("button:has-text('OK'), .ant-btn-primary:has-text('OK')")
    await ok_in_form.first.click()

    # Some skins raise a confirm modal; accept up to 2 times if it appears
    for _ in range(2):
        dialog_ok = page.locator(
            ".el-message-box__btns button:has-text('OK'), "
            ".ant-modal .ant-btn-primary:has-text('OK'), "
            ".el-dialog__wrapper button:has-text('OK')"
        ).first
        if await dialog_ok.count():
            try:
                await dialog_ok.click()
            except Exception:
                pass
            await asyncio.sleep(0.25)
        else:
            break

    # Wait for explicit success toast
    for _ in range(30):
        if await page.locator(".el-message--success, .ant-message-success").count():
            # re-read current point for return metadata (best-effort)
            new_point = await _read_current_point(page)
            return {"ok": True, "amount": float(amt_val), "current_point": new_point, "note": "toast success"}
        await asyncio.sleep(0.2)

    # Or wait for Current Point to change (implicit success)
    try:
        if await _wait_success_or_point_change(page, prev_point, timeout_ms=10_000):
            new_point = await _read_current_point(page)
            return {"ok": True, "amount": float(amt_val), "current_point": new_point, "note": "confirmed via Current Point"}
    except Exception:
        pass

    # Bubble up any visible error
    err_sel = ".el-message--error, .ant-message-error, .el-form-item__error, .ant-form-item-explain-error"
    if await page.locator(err_sel).count():
        msg = (await page.locator(err_sel).first.text_content() or "").strip()
        raise RuntimeError(msg or "Set Score failed")

    # As a last check, if the input field cleared itself (validation), surface that
    try:
        val_now = (await amt_input.input_value()).strip()
        if not val_now:
            raise RuntimeError("Amount input cleared by validation (possibly requires integer or min value).")
    except Exception:
        pass

    raise RuntimeError("No success confirmation")

async def recharge_user(page: Page, account: str, amount: float, remark: str = "") -> dict:
    await _open_set_score_via_search_user(page, account)
    return await _submit_set_score(page, abs(float(amount)), remark or "recharge")

async def redeem_user(page: Page, account: str, amount: float, remark: str = "") -> dict:
    await _open_set_score_via_search_user(page, account)
    return await _submit_set_score(page, -abs(float(amount)), remark or "redeem")

# ---------------- Sync wrappers (optional convenience) ----------------
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)

def recharge_sync(account: str, amount: float, remark: str = "") -> dict:
    async def _inner():
        bundle = await _launch()
        _, _, _, page = bundle
        try:
            lr = await up_login(page)
            if not lr.get("ok"): return {"ok": False, "error": f"login failed: {lr}"}
            return await recharge_user(page, account, amount, remark)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            await _close(bundle)
    return _run(_inner())

def redeem_sync(account: str, amount: float, remark: str = "") -> dict:
    async def _inner():
        bundle = await _launch()
        _, _, _, page = bundle
        try:
            lr = await up_login(page)
            if not lr.get("ok"): return {"ok": False, "error": f"login failed: {lr}"}
            return await redeem_user(page, account, amount, remark)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            await _close(bundle)
    return _run(_inner())

# ---------------- CLI ----------------
async def main():
    if not (ADMIN_USER and ADMIN_PASS):
        print("Missing ULTRAPANDA_USERNAME / ULTRAPANDA_PASSWORD in .env")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m automation.ultrapanda_ui_bot login")
        print("  python -m automation.ultrapanda_ui_bot create [account?] [password?]")
        print("  python -m automation.ultrapanda_ui_bot recharge <account> <amount> [remark?]")
        print("  python -m automation.ultrapanda_ui_bot redeem  <account> <amount> [remark?]")
        print("  python -m automation.ultrapanda_ui_bot test")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    bundle = await _launch()
    _, _, _, page = bundle
    try:
        if cmd in ("login","create","recharge","redeem","test"):
            lr = await up_login(page)
            print("[login]", json.dumps(lr, ensure_ascii=False, indent=2))
            if not lr.get("ok"): sys.exit(1)

        if cmd == "login":
            return

        if cmd == "create":
            acct = sys.argv[2] if len(sys.argv) >= 3 else _sanitize_username(UP_PREFIX + _rand(UP_LEN) + UP_SUFFIX)
            pwd  = sys.argv[3] if len(sys.argv) >= 4 else UP_DEFAULT_PWD
            res  = await create_user(page, acct, pwd)
            print("[create]", json.dumps(res, ensure_ascii=False, indent=2))
            if not res.get("ok"): sys.exit(1)

        elif cmd == "recharge":
            if len(sys.argv) < 4:
                print("recharge requires: <account> <amount> [remark?]"); sys.exit(1)
            acct = sys.argv[2]; amt = float(sys.argv[3]); rem = sys.argv[4] if len(sys.argv)>=5 else ""
            res = await recharge_user(page, acct, amt, rem)
            print("[recharge]", json.dumps(res, ensure_ascii=False, indent=2))
            if not res.get("ok"): sys.exit(1)

        elif cmd == "redeem":
            if len(sys.argv) < 4:
                print("redeem requires: <account> <amount> [remark?]"); sys.exit(1)
            acct = sys.argv[2]; amt = float(sys.argv[3]); rem = sys.argv[4] if len(sys.argv)>=5 else ""
            res = await redeem_user(page, acct, amt, rem)
            print("[redeem]", json.dumps(res, ensure_ascii=False, indent=2))
            if not res.get("ok"): sys.exit(1)

        elif cmd == "test":
            acct = _sanitize_username(UP_PREFIX + _rand(UP_LEN) + UP_SUFFIX)
            print("[create]",   json.dumps(await create_user(page, acct, UP_DEFAULT_PWD), ensure_ascii=False, indent=2))
            print("[recharge]", json.dumps(await recharge_user(page, acct, 5, "bot test"), ensure_ascii=False, indent=2))
            print("[redeem]",   json.dumps(await redeem_user(page, acct, 3, "bot test"), ensure_ascii=False, indent=2))
        else:
            print(f"Unknown command: {cmd}")
    finally:
        await _close(bundle)

if __name__ == "__main__":
    asyncio.run(main())