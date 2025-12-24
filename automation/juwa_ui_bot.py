"""
Juwa UI Bot (Playwright) – login, create user (auto-shuffle on name-in-use), recharge, redeem.

CLI:
  python -m automation.juwa_ui_bot login
  python -m automation.juwa_ui_bot create <account?> [password?]
  python -m automation.juwa_ui_bot recharge <account> <amount> [remark?]
  python -m automation.juwa_ui_bot redeem  <account> <amount> [remark?]
  python -m automation.juwa_ui_bot test
"""

from __future__ import annotations
import os, sys, asyncio, string, random, json, time, base64
from typing import Optional

# --- auto-load .env ---
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("[juwa_ui_bot] .env loaded")
except Exception:
    pass

import httpx
from playwright.async_api import async_playwright, Page, Locator

# --- ENV ---
BASE = os.getenv("JUWA_BASE_URL", "https://ht.juwa777.com").rstrip("/")
ADMIN_USER = os.getenv("JUWA_USERNAME", "")
ADMIN_PASS = os.getenv("JUWA_PASSWORD", "")
ADMIN_CODE = os.getenv("JUWA_AGENT_CODE", "")

HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")
CAPTCHA_POLL_SEC = float(os.getenv("CAPTCHA_POLL_SEC", "2"))
CAPTCHA_TIMEOUT_SEC = float(os.getenv("CAPTCHA_TIMEOUT_SEC", "90"))

USERNAME_PREFIX = os.getenv("USERNAME_PREFIX", "bot")
USERNAME_SUFFIX = os.getenv("JUWA_USERNAME_SUFFIX", "_ju")
USERNAME_LEN = int(os.getenv("USERNAME_LEN", "8"))
DEFAULT_PLAYER_PASSWORD = os.getenv("DEFAULT_PLAYER_PASSWORD", "Ab123456")

LOGIN_URL = f"{BASE}/login"
HOME_URL  = f"{BASE}/HomeDetail"
UM_URL    = f"{BASE}/userManagement"


def _rand(n: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


# --- Browser ---
async def _launch():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx = await browser.new_context()
    page = await ctx.new_page()
    page.set_default_timeout(60_000)
    return pw, browser, ctx, page


async def _close(bundle):
    if not bundle:
        return
    pw, b, ctx, page = bundle
    for fn in (ctx.close, b.close, pw.stop):
        try:
            await fn()
        except Exception:
            pass


# --- Captcha via 2captcha ---
async def _solve_captcha(page: Page) -> str:
    if not CAPTCHA_API_KEY:
        raise RuntimeError("CAPTCHA_API_KEY missing")

    await page.wait_for_selector("form img", timeout=30_000)
    img = page.locator("img[src*='captcha' i]").first
    if await img.count() == 0:
        img = page.locator("form img").first

    png = await img.screenshot(type="png")
    b64 = base64.b64encode(png).decode("ascii")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "http://2captcha.com/in.php",
            data={"key": CAPTCHA_API_KEY, "method": "base64", "body": b64, "json": 1},
        )
        j = r.json()
        if j.get("status") != 1:
            raise RuntimeError(f"2captcha submit failed: {j}")
        rid = j["request"]

        start = time.time()
        while True:
            await asyncio.sleep(CAPTCHA_POLL_SEC)
            rr = await client.get(
                "http://2captcha.com/res.php",
                params={"key": CAPTCHA_API_KEY, "action": "get", "id": rid, "json": 1},
            )
            jj = rr.json()
            if jj.get("status") == 1:
                return jj["request"]
            if time.time() - start > CAPTCHA_TIMEOUT_SEC:
                raise RuntimeError("2captcha timeout")


# --- VegasZ / announcement popup dismiss ---
async def _dismiss_vegas_popup(page: Page) -> None:
    """
    Close the VegasZ / Juwa 2.0 rollout popup if present.
    We look for a dialog containing 'VegasZ Games & Juwa 2.0 Rollout'
    and click its OK button.
    """
    try:
        # small grace time – but don't block if nothing appears
        await asyncio.sleep(1.0)
        dialog = page.locator(
            "[role='dialog']:has-text('Juwa 2.0 Rollout'), "
            "[role='dialog']:has-text('VegasZ Games & Juwa 2.0 Rollout'), "
            ".el-dialog:has-text('Juwa 2.0 Rollout'), "
            ".el-dialog:has-text('VegasZ Games & Juwa 2.0 Rollout')"
        ).first
        if await dialog.count() == 0:
            return

        ok_btn = dialog.get_by_role("button", name="OK")
        if await ok_btn.count() == 0:
            ok_btn = dialog.locator("button:has-text('OK')")
        if await ok_btn.count():
            await ok_btn.first.click()
            try:
                await dialog.wait_for(state="detached", timeout=5000)
            except Exception:
                pass
    except Exception:
        # Never let this kill the workflow
        pass


# --- Login ---
async def juwa_login(page: Page) -> dict:
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    user = page.get_by_placeholder("Account", exact=False)
    pwd  = page.get_by_placeholder("Password", exact=False)
    if await user.count() == 0:
        user = page.locator("form input[type='text']").first
    if await pwd.count() == 0:
        pwd = page.locator("form input[type='password']").first
    await user.fill(ADMIN_USER)
    await pwd.fill(ADMIN_PASS)

    try:
        code = await _solve_captcha(page)
    except Exception as e:
        return {"ok": False, "stage": "captcha", "raw": str(e)}

    form = page.locator("form:has(img)").first
    cap = form.locator("input[placeholder*='verification' i]:not([placeholder*='google' i])")
    if await cap.count() == 0:
        cap = form.locator("input").nth(2)
    await cap.first.fill(code)

    ac = form.get_by_placeholder("Agent code", exact=False)
    if await ac.count():
        await ac.fill(ADMIN_CODE)

    btn = page.get_by_role("button", name="Sign in")
    if await btn.count() == 0:
        btn = form.locator("button:has-text('Sign')")
    await btn.first.click()

    await page.wait_for_url("**/HomeDetail", timeout=30_000)

    # NEW: close VegasZ / Juwa 2.0 popup if it shows up after login
    await _dismiss_vegas_popup(page)

    return {"ok": True, "stage": "login", "url": page.url}


# --- Navigation: User Management ---
async def goto_user_management(page: Page):
    # Make sure any announcement popup is closed before navigating
    await _dismiss_vegas_popup(page)

    if "/userManagement" not in page.url:
        await page.goto(UM_URL, wait_until="domcontentloaded")

    # Some deployments show the popup AFTER navigation as well
    await _dismiss_vegas_popup(page)

    try:
        await page.wait_for_selector(
            "button:has-text('+ create'), button:has-text('create')",
            timeout=15_000,
        )
    except Exception:
        await page.get_by_text("User Management", exact=False).first.wait_for(timeout=10_000)


# ---------- Modal helpers ----------
async def _get_visible_modal(page: Page) -> Locator:
    await page.wait_for_selector(".el-dialog:visible, .ant-modal:visible, [role='dialog']:visible", timeout=20_000)
    modal = page.locator(
        ".el-dialog:has-text('Essential information'), "
        ".ant-modal:has-text('Essential information'), "
        "[role='dialog']:has-text('Essential information')"
    ).first
    if await modal.count() == 0:
        modal = page.locator(".el-dialog:visible, .ant-modal:visible, [role='dialog']:visible").last
    return modal

async def _wait_save_result(page: Page, modal: Locator) -> bool:
    toast_sel = ".el-message--success, .ant-message-success, .ant-notification-notice-success"
    table_ready = "button:has-text('+ create'), button:has-text('create')"
    try:
        await page.wait_for_selector(toast_sel, timeout=6_000); return True
    except Exception:
        pass
    try:
        await page.wait_for_selector(table_ready, timeout=6_000); return True
    except Exception:
        pass
    try:
        await modal.wait_for(state="detached", timeout=6_000); return True
    except Exception:
        return False

def _enabled_input(scope: Locator) -> Locator:
    """Return the first enabled <input> inside the given scope."""
    loc = scope.locator("input.el-input__inner:not([disabled]), input:not([disabled])")
    return loc.first

def _amount_input_by_label(body: Locator, label_text: str) -> Locator:
    """Try to find the input next to a given label text (Recharge Amount / Redeem Amount)."""
    # text node -> parent -> find input
    label = body.get_by_text(label_text, exact=False).first
    return label.locator("..").locator("input:not([disabled])")

# --- Create user (auto-shuffle if taken) ---
async def create_user(page: Page, account: Optional[str], password: Optional[str]) -> dict:
    await goto_user_management(page)

    base_name = account or f"{USERNAME_PREFIX}{_rand(USERNAME_LEN)}{USERNAME_SUFFIX}"
    password = password or DEFAULT_PLAYER_PASSWORD

    new_btn = page.get_by_role("button", name="+ create")
    if await new_btn.count() == 0:
        new_btn = page.locator("button:has-text('+ create'), button:has-text('Create'), button:has-text('create')")

    last_error = ""
    for attempt in range(1, 6):
        candidate = base_name if attempt == 1 else (
            (base_name[:-len(USERNAME_SUFFIX)] + _rand(random.randint(4, 6)) + USERNAME_SUFFIX)
            if base_name.endswith(USERNAME_SUFFIX) else (base_name + _rand(random.randint(4, 6)))
        )

        await new_btn.first.click()
        modal = await _get_visible_modal(page)

        body = modal.locator(".el-dialog__body, .ant-modal-body, :scope")
        inputs = body.locator("input.el-input__inner, input")
        await inputs.nth(3).wait_for()

        acc  = inputs.nth(0)
        nick = inputs.nth(1)
        lpwd = inputs.nth(2)
        cpwd = inputs.nth(3)

        for loc, val in ((acc, candidate),
                         (nick, candidate.replace(USERNAME_SUFFIX, "")),
                         (lpwd, password),
                         (cpwd, password)):
            await loc.fill("")
            await loc.type(val)

        save = modal.get_by_role("button", name="Save")
        if await save.count() == 0:
            save = modal.locator("button:has-text('Save')")
        await save.first.click()

        ok = await _wait_save_result(page, modal)
        if ok:
            # look for an error toast like "account already used"
            err_text = ""
            for sel in (
                ".el-message--error, .ant-message-error, .ant-notification-notice-error",
                ".el-form-item__error, .ant-form-item-explain-error",
            ):
                loc = page.locator(sel)
                if await loc.count():
                    try:
                        t = (await loc.first.inner_text()).strip().lower()
                        if t:
                            err_text = t
                            break
                    except Exception:
                        pass

            if err_text and any(w in err_text for w in ("already", "exists", "used", "have used", "duplicate")):
                try:
                    close_x = modal.locator("button.el-dialog__headerbtn, .ant-modal-close")
                    if await close_x.count():
                        await close_x.first.click()
                        await modal.wait_for(state="detached", timeout=5_000)
                except Exception:
                    pass
                last_error = err_text
                continue

            return {"ok": True, "created": True, "account": candidate, "attempts": attempt}

        try:
            close_x = modal.locator("button.el-dialog__headerbtn, .ant-modal-close")
            if await close_x.count():
                await close_x.first.click()
                await modal.wait_for(state="detached", timeout=5_000)
        except Exception:
            pass
        last_error = "unknown UI state after Save"

    return {"ok": False, "created": False, "account": base_name, "error": last_error or "name already used"}


# --- Open editor menu for a given account row ---
async def _open_editor_for(page: Page, account: str):
    await goto_user_management(page)
    row = page.locator("tr").filter(has_text=account).first
    await row.wait_for()

    ed = row.get_by_text("editor", exact=False)
    if await ed.count() == 0:
        ed = row.locator("[aria-haspopup='menu'], button:has(svg), button:has-text('...'), a:has-text('editor')")
    await ed.first.click()

    # Wait for dropdown menu
    menu = page.locator(".el-dropdown-menu:visible, .ant-dropdown-menu:visible, [role='menu']:visible").first
    await menu.wait_for()
    return menu


# --- Recharge ---
async def recharge_user(page: Page, account: str, amount: float, remark: str = "") -> dict:
    # Always stay in userManagement
    await goto_user_management(page)

    # Open editor for this account
    row = page.locator("tr").filter(has_text=account).first
    await row.wait_for()
    editor_btn = row.get_by_text("editor", exact=False)
    if await editor_btn.count() == 0:
        editor_btn = row.locator("button:has-text('editor'), a:has-text('editor')")
    await editor_btn.first.click()

    # Wait for dropdown
    menu = page.locator(".el-dropdown-menu:visible").first
    await menu.wait_for()

    # Click recharge inside dropdown
    recharge_item = menu.get_by_text("recharge", exact=False)
    await recharge_item.wait_for()
    await recharge_item.click()

    # Now wait for recharge modal
    modal = page.locator(".el-dialog:visible, [role='dialog']:visible").last
    await modal.wait_for()
    body = modal.locator(".el-dialog__body")

    # Fill Recharge Amount
    amt_input = body.locator("input:not([disabled])").last
    await amt_input.wait_for()
    await amt_input.fill(str(amount))

    # Fill remark if available
    if remark and await body.locator("textarea").count():
        await body.locator("textarea").fill(remark)

    # Click Confirm
    confirm_btn = modal.get_by_role("button", name="Confirm")
    if await confirm_btn.count() == 0:
        confirm_btn = modal.locator("button:has-text('Confirm')")
    await confirm_btn.first.click()

    # Wait until modal closes
    try:
        await modal.wait_for(state="detached", timeout=15_000)
    except Exception:
        pass

    return {"ok": True, "recharged": True, "account": account, "amount": amount}


# --- Redeem ---
async def redeem_user(page: Page, account: str, amount: float, remark: str = "") -> dict:
    # Always stay in userManagement
    await goto_user_management(page)

    # Find account row
    row = page.locator("tr").filter(has_text=account).first
    await row.wait_for()

    # Open editor dropdown
    editor_btn = row.get_by_text("editor", exact=False)
    if await editor_btn.count() == 0:
        editor_btn = row.locator("button:has-text('editor'), a:has-text('editor')")
    await editor_btn.first.click()

    # Wait for dropdown menu
    menu = page.locator(".el-dropdown-menu:visible").first
    await menu.wait_for()

    # Click redeem
    redeem_item = menu.get_by_text("redeem", exact=False)
    await redeem_item.wait_for()
    await redeem_item.click()

    # Redeem modal
    modal = page.locator(".el-dialog:visible, [role='dialog']:visible").last
    await modal.wait_for()
    body = modal.locator(".el-dialog__body")

    # Fill amount
    amt_input = body.locator("input:not([disabled])").last
    await amt_input.wait_for()
    await amt_input.fill(str(amount))

    # Fill remark if exists
    if remark and await body.locator("textarea").count():
        await body.locator("textarea").fill(remark)

    # Confirm
    confirm_btn = modal.get_by_role("button", name="Confirm")
    if await confirm_btn.count() == 0:
        confirm_btn = modal.locator("button:has-text('Confirm')")
    await confirm_btn.first.click()

    # Wait for modal to close
    try:
        await modal.wait_for(state="detached", timeout=15_000)
    except Exception:
        pass

    return {"ok": True, "redeemed": True, "account": account, "amount": amount}


# --- CLI ---
async def main():
    if not (ADMIN_USER and ADMIN_PASS):
        print("Missing JUWA_USERNAME / JUWA_PASSWORD in .env")
        sys.exit(1)
    if not CAPTCHA_API_KEY:
        print("Missing CAPTCHA_API_KEY in .env – required for hands-free login")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m automation.juwa_ui_bot login")
        print("  python -m automation.juwa_ui_bot create <account?> [password?]")
        print("  python -m automation.juwa_ui_bot recharge <account> <amount> [remark?]")
        print("  python -m automation.juwa_ui_bot redeem  <account> <amount> [remark?]")
        print("  python -m automation.juwa_ui_bot test")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    bundle = await _launch()
    _, _, _, page = bundle
    try:
        lr = await juwa_login(page)
        print("[login]", json.dumps(lr, ensure_ascii=False, indent=2))
        if not lr.get("ok"):
            sys.exit(1)

        if cmd == "login":
            return

        if cmd == "create":
            acct = sys.argv[2] if len(sys.argv) >= 3 else f"{USERNAME_PREFIX}{_rand(USERNAME_LEN)}{USERNAME_SUFFIX}"
            pwd  = sys.argv[3] if len(sys.argv) >= 4 else DEFAULT_PLAYER_PASSWORD
            print("[create]", json.dumps(await create_user(page, acct, pwd), indent=2))

        elif cmd == "recharge":
            if len(sys.argv) < 4:
                print("recharge requires: <account> <amount> [remark?]")
                sys.exit(1)
            acct = sys.argv[2]
            amt = float(sys.argv[3])
            rem = sys.argv[4] if len(sys.argv) >= 5 else ""
            print("[recharge]", json.dumps(await recharge_user(page, acct, amt, rem), indent=2))

        elif cmd == "redeem":
            if len(sys.argv) < 4:
                print("redeem requires: <account> <amount> [remark?]")
                sys.exit(1)
            acct = sys.argv[2]
            amt = float(sys.argv[3])
            rem = sys.argv[4] if len(sys.argv) >= 5 else ""
            print("[redeem]", json.dumps(await redeem_user(page, acct, amt, rem), indent=2))

        elif cmd == "test":
            acct = f"{USERNAME_PREFIX}{_rand(USERNAME_LEN)}{USERNAME_SUFFIX}"
            print("[create]",   json.dumps(await create_user(page, acct, DEFAULT_PLAYER_PASSWORD), indent=2))
            print("[recharge]", json.dumps(await recharge_user(page, acct, 1, "bot test"), indent=2))
            print("[redeem]",   json.dumps(await redeem_user(page, acct, 1, "bot test"), indent=2))
        else:
            print(f"Unknown command: {cmd}")
    finally:
        await _close(bundle)


if __name__ == "__main__":
    asyncio.run(main())

