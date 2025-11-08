# automation/ultrapanda_ui_bot.py
"""
UltraPanda UI Bot (Playwright) — FINAL

Features
- Pre/Post login popup killer (announcement / hint / GA notice): confirm / OK / X or hard-remove masks.
- Login with multiple guarded retries (fills .env creds, handles intercepted clicks).
- Create Player (auto-sanitize to 7–16 alphanumeric; retry on duplicate).
- Recharge / Redeem via Search User → Player account → Set Score (+ / - amount).
- Success detection via toast OR Current Point change OR panel auto-close.

ENV (.env at project root or automation/)
  ULTRAPANDA_BASE_URL=https://ht.ultrapanda.club
  ULTRAPANDA_USERNAME=rob1820
  ULTRAPANDA_PASSWORD=rob1820
  HEADLESS=false
  SLOW_MO_MS=120

Optional
  ULTRAPANDA_USERNAME_PREFIX=up
  ULTRAPANDA_USERNAME_SUFFIX=
  ULTRAPANDA_USERNAME_LEN=10
  ULTRAPANDA_DEFAULT_PLAYER_PASSWORD=Abc12345

CLI
  python -m automation.ultrapanda_ui_bot login
  python -m automation.ultrapanda_ui_bot create [account?] [password?]
  python -m automation.ultrapanda_ui_bot recharge <account> <amount> [remark?]
  python -m automation.ultrapanda_ui_bot redeem  <account> <amount> [remark?]
  python -m automation.ultrapanda_ui_bot test
"""

from __future__ import annotations
import os, sys, re, json, asyncio, random, string
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
    for p in (here.parents[1] / ".env", here.parent / ".env"):
        if p.exists():
            load_dotenv(p, override=True)
            print(f"[ultrapanda_ui_bot] .env loaded from {p}")
            return
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found, override=True)
        print(f"[ultrapanda_ui_bot] .env loaded from {found}")
    else:
        print("[ultrapanda_ui_bot] .env not found; relying on OS env")
_load_env()

# ---------------- Config ----------------
BASE = (os.getenv("ULTRAPANDA_BASE_URL", "https://ht.ultrapanda.club") or "").rstrip("/")
ADMIN_USER = os.getenv("ULTRAPANDA_USERNAME", "")
ADMIN_PASS = os.getenv("ULTRAPANDA_PASSWORD", "")

HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1","true","yes","on")
SLOW_MO_MS = int(os.getenv("SLOW_MO_MS", "0") or "0")

UP_PREFIX = os.getenv("ULTRAPANDA_USERNAME_PREFIX", "up")
UP_SUFFIX = os.getenv("ULTRAPANDA_USERNAME_SUFFIX", "")
UP_LEN    = int(os.getenv("ULTRAPANDA_USERNAME_LEN", "10") or "10")
UP_DEFAULT_PWD = os.getenv("ULTRAPANDA_DEFAULT_PLAYER_PASSWORD", "Abc12345")

# Speed mode (kept from earlier answer)
SPEED_MODE = os.getenv("ULTRAPANDA_SPEED_MODE", "1").lower() in ("1","true","yes","on")

LOGIN_URL  = f"{BASE}/#/login?redirect=%2F"
HOME_URL   = f"{BASE}/#/index"
UM_URL     = f"{BASE}/#/manage-user/account"
SEARCH_URL = f"{BASE}/#/manage-user/search"

# ---------------- Playwright ----------------
from playwright.async_api import async_playwright, Page, Locator

def _rand(n:int)->str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))

def _sanitize_username(name:str, min_len:int=7, max_len:int=16)->str:
    clean = re.sub(r"[^a-zA-Z0-9]", "", name or "")
    if len(clean) < min_len: clean += _rand(min_len - len(clean))
    if len(clean) > max_len: clean = clean[:max_len]
    return clean

async def _launch() -> Tuple:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        slow_mo=SLOW_MO_MS,
        args=["--disable-blink-features=AutomationControlled"]
    )
    ctx = await browser.new_context(
        viewport={"width": 1368, "height": 900},
        ignore_https_errors=True,
        java_script_enabled=True,
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        locale="en-US",
        timezone_id="America/New_York",
    )

    if SPEED_MODE:
        async def _route(route, request):
            rtype = request.resource_type
            url = request.url
            if rtype in ("image", "media", "font") or "googletagmanager" in url or "analytics" in url:
                return await route.abort()
            return await route.continue_()
        await ctx.route("**/*", _route)

    page = await ctx.new_page()
    page.set_default_timeout(8_000 if SPEED_MODE else 15_000)
    page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
    return pw, browser, ctx, page

async def _close(bundle: Tuple):
    if not bundle: return
    pw, b, ctx, page = bundle
    for fn in (ctx.close, b.close, pw.stop):
        try: await fn()
        except Exception: pass

# ---------------- Minor utils ----------------
async def _is_open(page: Page) -> bool:
    try:
        _ = page.url
        return not page.is_closed()
    except Exception:
        return False

async def _wait_spa(page: Page, url_sub: str, timeout: int = 12_000):
    try:
        if url_sub not in page.url:
            await page.wait_for_url(f"**{url_sub}**", timeout=timeout)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout//2)
        except Exception:
            pass
        await page.wait_for_timeout(80 if SPEED_MODE else 120)
    except Exception:
        if await _is_open(page):
            await page.wait_for_timeout(80)

async def _safe_click_text(page: Page, text: str, timeout: int = 4000) -> bool:
    for sel in (
        f"role=link[name='{text}']",
        f"role=button[name='{text}']",
        f"text={text}",
        f"a:has-text('{text}')",
        f"button:has-text('{text}')",
        f"li:has-text('{text}')",
        f"span:has-text('{text}')",
    ):
        loc = page.locator(sel).first
        if await loc.count():
            try:
                await loc.click(timeout=timeout)
                return True
            except Exception:
                pass
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

# ---------------- Popup killers ----------------
async def _dismiss_any_notice(page: Page, rounds: int = 3):
    if not await _is_open(page): return
    for _ in range(rounds):
        for sel in (
            "button:has-text('confirm')",
            "button:has-text('Confirm')",
            "button:has-text('OK')",
            "button:has-text('Ok')",
            ".el-message-box__btns .el-button--primary",
            ".ant-modal .ant-btn-primary",
        ):
            btn = page.locator(sel).first
            if await btn.count():
                try: await btn.click()
                except Exception: pass
        for sel in (
            ".notice-box .el-dialog__headerbtn",
            ".el-dialog__headerbtn",
            ".ant-modal .ant-modal-close",
            ".ant-notification-close-icon",
        ):
            x = page.locator(sel).first
            if await x.count():
                try: await x.click()
                except Exception: pass
        try: await page.keyboard.press("Escape")
        except Exception: pass
        if await _is_open(page):
            await page.wait_for_timeout(80)

    try:
        await page.evaluate("""
(() => {
  const kill = sel => document.querySelectorAll(sel).forEach(n=>n.remove());
  kill('.notice-box,.el-message-box__wrapper,.el-dialog__wrapper,.ant-modal-root,.ant-modal-wrap,.ant-modal-mask,.v-modal');
  Array.from(document.querySelectorAll('*')).forEach(n=>{
    const cs = getComputedStyle(n);
    const z = parseInt(cs.zIndex||'0',10);
    if (z>10000) n.style.zIndex='0';
    if (cs.pointerEvents==='none') n.style.pointerEvents='auto';
  });
})();
""")
    except Exception:
        pass

# ---------------- Errors / points ----------------
async def _read_any_error_text(page: Page) -> str:
    for sel in (
        ".el-message--error, .ant-message-error",
        ".el-form-item__error, .ant-form-item-explain-error",
        ".el-message-box__message, .ant-modal .ant-modal-body",
    ):
        loc = page.locator(sel).first
        if await loc.count():
            try:
                t = (await loc.text_content() or "").strip()
                if t: return t
            except Exception:
                pass
    return ""

async def _read_current_point(page: Page) -> Optional[int]:
    try:
        lab = page.locator("text=Current Point").first
        if not await lab.count(): return None
        container = lab.locator("xpath=..")
        txt = (await container.inner_text() or "").strip()
        if not re.search(r"\b-?\d+\b", txt):
            txt = (await page.locator("section,div").filter(has_text="Current Point").first.inner_text() or "").strip()
        m = re.search(r"Current\s*Point\s*([\-]?\d+)", txt, flags=re.I) or re.search(r"\b(\-?\d+)\b", txt)
        return int(m.group(1)) if m else None
    except Exception:
        return None

async def _panel_gone(page: Page) -> bool:
    try:
        return not await page.locator("text=Set Score").first.is_visible()
    except Exception:
        return False

async def _wait_success_or_point_change(page: Page, prev_point: Optional[int], timeout_ms: int = 12_000) -> tuple[bool, Optional[int]]:
    step = 0.25; waited = 0.0; deadline = timeout_ms/1000.0
    while waited < deadline and await _is_open(page):
        if await page.locator(".el-message--success, .ant-message-success").count():
            return True, await _read_current_point(page)
        now = await _read_current_point(page)
        if prev_point is not None and now is not None and now != prev_point:
            return True, now
        if await _panel_gone(page):
            return True, now
        await asyncio.sleep(step); waited += step
    return False, await _read_current_point(page)

# ---------------- Login ----------------
async def _find_login_inputs(page: Page, timeout: int = 20_000):
    end = page._impl_obj._loop.time() + timeout/1000
    user = pw = None
    while page._impl_obj._loop.time() < end and await _is_open(page):
        await _dismiss_any_notice(page, rounds=1)
        for sel in ("input[placeholder='User Name']", "input[placeholder*='User' i]", "input[placeholder*='Account' i]", "input[type='text']"):
            loc = page.locator(sel).first
            if await loc.count(): user = loc; break
        for sel in ("input[placeholder='Password']", "input[placeholder*='password' i]", "input[type='password']"):
            loc = page.locator(sel).first
            if await loc.count(): pw = loc; break
        if user and pw: break
        await page.wait_for_timeout(120)
    return user, pw

async def up_login(page: Page) -> dict:
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await _dismiss_any_notice(page)

        user_input, pass_input = await _find_login_inputs(page, timeout=25_000)
        if not (user_input and pass_input):
            await _wait_spa(page, "#/index", 10_000)
            await _dismiss_any_notice(page)
            if "#/index" in page.url:
                return {"ok": True, "stage": "login", "url": page.url}
            return {"ok": False, "stage": "prelogin", "error": "username/password input not found"}

        await user_input.fill(ADMIN_USER)
        await pass_input.fill(ADMIN_PASS)

        login_btn = page.locator("button:has-text('Login')").first
        for _ in range(4):
            try:
                await login_btn.click()
                break
            except Exception:
                await _dismiss_any_notice(page, rounds=2)

        await _wait_spa(page, "#/index", 30_000)
        await _dismiss_any_notice(page)

        if "#/index" in page.url:
            return {"ok": True, "stage": "login", "url": page.url}
        return {"ok": False, "stage": "login", "error": "did not reach index", "url": page.url}
    except Exception as e:
        return {"ok": False, "stage": "login", "error": str(e)}

# ---------------- Navigation ----------------
async def _open_user_management(page: Page):
    if "#/index" not in page.url:
        await page.goto(HOME_URL, wait_until="domcontentloaded")
    await _wait_spa(page, "#/index", 20_000)
    await _dismiss_any_notice(page)
    await _safe_click_text(page, "User management")
    if await _is_open(page):
        await page.wait_for_timeout(120)

async def _goto_players_list(page: Page):
    # try menu path first (more stable when side-effects are needed)
    await _open_user_management(page)
    clicked = False
    for sel in ("a:has-text('User Management')","button:has-text('User Management')","li:has-text('User Management')","span:has-text('User Management')"):
        loc = page.locator(sel).first
        if await loc.count():
            try: await loc.click(); clicked=True; break
            except Exception: pass
    if not clicked:
        await page.goto(UM_URL, wait_until="domcontentloaded")
    await _wait_spa(page, "#/manage-user/account", 20_000)
    await _dismiss_any_notice(page)

async def _goto_search_user(page: Page):
    await _open_user_management(page)
    ok = False
    for sel in ("a:has-text('Search User')","button:has-text('Search User')","li:has-text('Search User')","span:has-text('Search User')"):
        loc = page.locator(sel).first
        if await loc.count():
            try: await loc.click(); ok=True; break
            except Exception: pass
    if not ok:
        await page.goto(SEARCH_URL, wait_until="domcontentloaded")
    await _wait_spa(page, "#/manage-user/search", 20_000)
    await _dismiss_any_notice(page)

# ---------------- Create user ----------------
async def _open_add_player_form(page: Page):
    # Robust path: menu → players list (ensures app state), then fall back to direct URL
    try:
        await _goto_players_list(page)
    except Exception:
        await page.goto(UM_URL, wait_until="domcontentloaded")
        await _wait_spa(page, "#/manage-user/account", 12_000)
    await _dismiss_any_notice(page)

    # Try multiple ways to press "Add Player"
    selectors = [
        "a:has-text('Add Player')",
        "button:has-text('Add Player')",
        ".el-button:has-text('Add Player')",
        "button:has(svg.el-icon-plus), .el-button:has(.el-icon-plus)",
    ]
    add_clicked = False
    for sel in selectors:
        el = page.locator(sel).first
        if await el.count():
            try:
                await el.scroll_into_view_if_needed()
                await el.click()
                add_clicked = True
                break
            except Exception:
                pass
    if not add_clicked:
        # scroll search
        for _ in range(6):
            await page.mouse.wheel(0, 900)
            await page.wait_for_timeout(60)
            el = page.locator("a:has-text('Add Player'), button:has-text('Add Player')").first
            if await el.count():
                try:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    add_clicked = True
                    break
                except Exception:
                    pass
    if not add_clicked:
        # last resort JS click
        try:
            await page.evaluate("""
(() => {
  const want = /^(Add\\s*Player)$/i;
  const nodes = Array.from(document.querySelectorAll('button,a,span,div'));
  const btn = nodes.find(n => want.test((n.innerText||'').trim()));
  if (btn) btn.click();
})();
""")
            add_clicked = True
        except Exception:
            pass

    # Wait for VISIBLE inputs only (hidden form exists in DOM)
    form_ready = page.locator(
        "form:visible input[type='password']:visible, "
        ".el-dialog:visible input[type='password']:visible, "
        "input[placeholder*='account name' i]:visible, "
        "input[placeholder*='user' i]:visible"
    ).first
    await form_ready.wait_for(timeout=15_000)

async def create_user(page: Page, account: Optional[str], password: Optional[str]) -> dict:
    password = password or UP_DEFAULT_PWD
    base = _sanitize_username(account or (UP_PREFIX + _rand(max(7, UP_LEN)) + UP_SUFFIX))

    last_err = ""
    for attempt in range(1, 6):
        candidate = base if attempt == 1 else _sanitize_username(base + _rand(random.randint(3,5)))
        await _open_add_player_form(page)

        uname = None
        for sel in ("input[placeholder*='account name' i]","input[placeholder*='user' i]","form input[type='text']","input[type='text']"):
            loc = page.locator(f"{sel}:visible:not([disabled])").first
            if await loc.count(): uname = loc; break
        if not uname:
            return {"ok": False, "created": False, "error": "Cannot find username field"}

        pw_inputs = page.locator("input[type='password']:visible")
        if await pw_inputs.count() == 0:
            return {"ok": False, "created": False, "error": "Password fields not found"}

        # Fast JS set (no slow typing)
        async def _set_input_js(loc, val: str):
            h = await loc.element_handle()
            if h:
                await page.evaluate("""([el, v]) => {
                    const d = Object.getOwnPropertyDescriptor(el.__proto__, 'value')
                          || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                    d.set.call(el, String(v));
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }""", [h, val])

        await _set_input_js(uname, candidate)
        await _set_input_js(pw_inputs.nth(0), password)
        if await pw_inputs.count() > 1:
            await _set_input_js(pw_inputs.nth(1), password)

        # Submit
        submitted = False
        for sel in ("button:has-text('OK')","button:has-text('Ok')","button:has-text('Confirm')","button:has-text('Save')"):
            b = page.locator(f"{sel}:visible").first
            if await b.count():
                try:
                    await b.click()
                    submitted = True
                    break
                except Exception:
                    pass
        if not submitted:
            try:
                await page.evaluate("""
(() => {
  const btns = Array.from(document.querySelectorAll('button,.el-button,.ant-btn'))
    .filter(b => /^(OK|Ok|Confirm|Save|确定)$/.test((b.innerText||'').trim()));
  if (btns.length) btns[btns.length-1].click();
})();
""")
                submitted = True
            except Exception:
                pass
        if not submitted:
            return {"ok": False, "created": False, "error": "Submit button not found"}

        for _ in range(30):
            if await page.locator(".el-message--success, .ant-message-success").count():
                return {"ok": True, "created": True, "account": candidate, "attempts": attempt}
            err = await _read_any_error_text(page)
            if err:
                last_err = err; break
            await asyncio.sleep(0.2)

        if last_err:
            low = last_err.lower()
            if any(k in low for k in ("already","exist","used","duplicate")) or "alphanumeric" in low:
                continue
            break

    return {"ok": False, "created": False, "account": base, "error": last_err or "unable to create user"}

# ---------------- Recharge / Redeem ----------------
async def _open_set_score_via_search_user(page: Page, account: str):
    await _goto_search_user(page)
    radio = page.locator("label:has-text('Player account'), span:has-text('Player account')").first
    if await radio.count():
        try: await radio.click()
        except Exception: pass

    box = None
    for sel in ("input[placeholder*='search here' i]","input[placeholder*='account' i]","input[placeholder*='user' i]"):
        loc = page.locator(sel).first
        if await loc.count(): box = loc; break
    if not box: raise RuntimeError("Search input not found")

    await box.fill(account)
    await page.locator("button:has-text('OK')").first.click()

    row = page.locator("table tr").filter(has_text=account).first
    await row.wait_for(timeout=12_000)

    set_btn = page.locator("button:has-text('Set Score'), a:has-text('Set Score')").first
    await _scroll_until_visible(page, set_btn, steps=20, dy=900)
    await set_btn.wait_for(timeout=10_000)
    await set_btn.click()

    await page.locator("input[placeholder*='ie 100' i], input[type='number']").first.wait_for(timeout=10_000)

async def _submit_set_score(page: Page, amount: float, remark: str = "") -> dict:
    prev_point = await _read_current_point(page)
    amt_val = int(amount) if float(amount).is_integer() else float(amount)
    amt_str = str(amt_val)

    amt_input = page.locator("input[placeholder*='ie 100' i], input[type='number']").first
    await amt_input.scroll_into_view_if_needed()
    await amt_input.fill("")
    await page.keyboard.type(amt_str)

    form = amt_input.locator("xpath=ancestor::form[1]")
    if not await form.count():
        form = page.locator("section:has-text('Set Score'), .el-card:has-text('Set Score'), div:has(> *:has-text('Set Score'))").first

    if remark:
        rem = form.locator("textarea, textarea[placeholder]").first
        if await rem.count():
            try: await rem.fill(remark)
            except Exception: pass

    await form.locator("button:has-text('OK'), .ant-btn-primary:has-text('OK')").first.click()

    for _ in range(2):
        dlg_ok = page.locator(
            ".el-message-box__btns button:has-text('OK'), "
            ".ant-modal .ant-btn-primary:has-text('OK'), "
            ".el-dialog__wrapper button:has-text('OK')"
        ).first
        if await dlg_ok.count():
            try: await dlg_ok.click()
            except Exception: pass
            await asyncio.sleep(0.25)
        else:
            break

    ok, now_point = await _wait_success_or_point_change(page, prev_point, timeout_ms=12_000)
    if ok:
        return {"ok": True, "amount": float(amt_val), "current_point": now_point, "prev_point": prev_point}

    err = await _read_any_error_text(page)
    if err: raise RuntimeError(err)
    try:
        if not (await amt_input.input_value()).strip():
            raise RuntimeError("Amount input cleared by validation (integer/min value required).")
    except Exception:
        pass
    raise RuntimeError("Set Score: no success confirmation")

async def recharge_user(page: Page, account: str, amount: float, remark: str = "") -> dict:
    await _open_set_score_via_search_user(page, account)
    return await _submit_set_score(page, abs(float(amount)), remark or "recharge")

async def redeem_user(page: Page, account: str, amount: float, remark: str = "") -> dict:
    await _open_set_score_via_search_user(page, account)
    return await _submit_set_score(page, -abs(float(amount)), remark or "redeem")


# ---------------- Sync wrappers ----------------
def _run(coro):
    """
    Run an async coroutine safely from any Flask/Gunicorn thread.
    Prefer asyncio.run(); if a loop is already running (rare), fall back
    to a dedicated new loop.
    """
    try:
        return asyncio.run(coro)  # works when no loop in this thread (Flask worker)
    except RuntimeError as e:
        # If we're somehow inside an existing running loop, use a fresh one.
        if "running event loop" in str(e).lower():
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(coro)
            finally:
                try:
                    loop.close()
                finally:
                    asyncio.set_event_loop(None)
        raise

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