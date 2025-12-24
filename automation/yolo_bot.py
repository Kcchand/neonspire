#!/usr/bin/env python3
import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ------------------------------------------------------------------------------
# ENV
# ------------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

YOLO_BASE_URL = os.getenv("YOLO_BASE_URL", "https://agent.yolo777.game").rstrip("/")
YOLO_LOGIN_URL = os.getenv("YOLO_LOGIN_URL", f"{YOLO_BASE_URL}/admin/auth/login")

YOLO_USER = os.getenv("YOLO_USER") or os.getenv("YOLO_USERNAME")
YOLO_PASS = os.getenv("YOLO_PASS") or os.getenv("YOLO_PASSWORD")
YOLO_GCODE = os.getenv("YOLO_GCODE") or os.getenv("YOLO_2FA_CODE") or ""

HEADLESS = os.getenv("YOLO_HEADLESS", "false").lower() == "true"
SLOWMO = int(os.getenv("YOLO_SLOWMO_MS", "100"))
TIMEOUT = int(os.getenv("YOLO_TIMEOUT_MS", "60000"))

ACCT_PREFIX = os.getenv("YOLO_USERNAME_PREFIX", "auto_")
ACCT_SUFFIX = os.getenv("YOLO_USERNAME_SUFFIX", "_yl")
DEFAULT_PASS = os.getenv("YOLO_DEFAULT_PASSWORD", "Abc12345")
DEFAULT_CREDIT = int(os.getenv("YOLO_DEFAULT_FIRST_CREDIT", "0"))


# ------------------------------------------------------------------------------
# UTILS
# ------------------------------------------------------------------------------

async def wait_network_idle(page):
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
    except PWTimeout:
        pass


def build_new_username():
    import random, string
    mid = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))
    return f"{ACCT_PREFIX}{mid}{ACCT_SUFFIX}"


# ------------------------------------------------------------------------------
# LOGIN
# ------------------------------------------------------------------------------

async def do_login(page):
    # Go to admin – this already waits for DOM to be ready
    await page.goto(f"{YOLO_BASE_URL}/admin", wait_until="domcontentloaded", timeout=TIMEOUT)

    # Safe first content check
    try:
        html = await page.content()
    except Exception as e:
        print("[yolo_bot] warning: could not read initial login page content:", e)
        html = ""

    if "login-box-msg" not in html and "login to your account" not in html.lower():
        # already logged in
        return

    print("[+] On login form -> filling...")

    # username
    for sel in [
        "input[name='username']",
        "input[placeholder*='Username' i]",
        "input[type='text']",
    ]:
        loc = page.locator(sel).first
        if await loc.count():
            await loc.fill(YOLO_USER)
            break
    else:
        raise RuntimeError("login: username field not found")

    # password
    for sel in [
        "input[name='password']",
        "input[placeholder*='Password' i]",
        "#password",
        "input[type='password']",
    ]:
        loc = page.locator(sel).first
        if await loc.count():
            await loc.fill(YOLO_PASS)
            break
    else:
        raise RuntimeError("login: password field not found")

    # submit
    btn = page.locator("button#submit, button[type='submit'], button:has-text('Login')").first
    if not await btn.count():
        raise RuntimeError("login: submit button not found")
    await btn.click()

    # 🔹 Instead of a blind sleep + fragile page.content(),
    #    wait for network to calm down and guard page.content().
    try:
        await wait_network_idle(page)
    except Exception:
        # ignore; just best-effort
        pass

    try:
        html2 = await page.content()
    except Exception as e:
        print("[yolo_bot] warning: could not read post-login content (safe to ignore):", e)
        html2 = ""

    # 2FA?
    if "name=\"gcode\"" in html2 or "placeholder=\"Code\"" in html2:
        if not YOLO_GCODE:
            raise RuntimeError("login: panel requires 2FA code but YOLO_GCODE is empty")
        print("[+] 2FA layer detected -> filling gcode")
        await page.evaluate(
            """(code) => {
                const input = document.querySelector("input[name='gcode']");
                if (input) input.value = code;
            }""",
            YOLO_GCODE,
        )
        await page.evaluate(
            """() => {
                const btn = document.querySelector(".layui-layer-btn0, .btn.btn-primary[data-v-5ff4eedb]");
                if (btn) btn.click();
            }"""
        )
        await asyncio.sleep(2.0)

    # go to dashboard
    await page.goto(f"{YOLO_BASE_URL}/admin", wait_until="load")
    print("[+] Logged into YOLO Dashboard")


# ------------------------------------------------------------------------------
# PLAYER LIST
# ------------------------------------------------------------------------------

async def open_player_list(page):
    """
    What we know from your runs:
    - /admin/player_list works (you saw "Player List page ready (direct url)")
    So we try direct URL first; if it fails, we try sidebar.
    """
    # try direct
    direct_url = f"{YOLO_BASE_URL}/admin/player_list"
    try:
        await page.goto(direct_url, wait_until="domcontentloaded", timeout=15000)
        html = await page.content()
        if "Player List" in html or "+ New" in html or "Create" in html:
            print("[+] Player List page ready (direct url)")
            return
    except Exception:
        pass

    # fallback: try to click sidebar "Player"
    try:
        await page.evaluate(
            """() => {
                const links = Array.from(document.querySelectorAll("a, button"));
                const t = links.find(a => /player/i.test(a.textContent));
                if (t) t.click();
            }"""
        )
        await wait_network_idle(page)
        print("[+] Player List page ready (via sidebar)")
        return
    except Exception:
        pass

    raise RuntimeError("open_player_list: could not open player list page")


# ------------------------------------------------------------------------------
# CREATE
# ------------------------------------------------------------------------------

async def ui_create_player(page, account, password, credit, nickname=None):
    """
    Your last run showed: the list page didn't expose the modal inputs,
    but /admin/player_list/create DID work.
    We'll keep 3 attempts:
      1) click '+ New' and fill modal (preferred)
      2) inspect frames and try inside iframe/modal
      3) direct-create page /admin/player_list/create  <-- this worked for you
    """
    # 1) try click +New
    clicked = False
    for sel in [
        "button:has-text('+ New')",
        "a:has-text('+ New')",
        "button.btn-primary:has-text('New')",
        ".grid-create-btn",
    ]:
        loc = page.locator(sel).first
        if await loc.count():
            await loc.click()
            clicked = True
            break

    if clicked:
        # give time for modal/pjax
        await asyncio.sleep(1.5)
        container = page.locator(".modal.show, .layui-layer-content, .el-dialog__body, form").first
        if await container.count():
            # try account
            acct_filled = False
            for sel in [
                "input[name='account']",
                "input[name='username']",
                "input[placeholder*='Account' i]",
                "input[placeholder*='User' i]",
            ]:
                el = container.locator(sel).first
                if await el.count():
                    await el.fill(account)
                    acct_filled = True
                    break
            if not acct_filled:
                print("[!] create: could not find Account field in modal")

            # password
            pwd_filled = False
            for sel in [
                "input[name='password']",
                "input[placeholder*='Password' i]",
                "input[type='password']",
            ]:
                el = container.locator(sel).first
                if await el.count():
                    await el.fill(password)
                    pwd_filled = True
                    break
            if not pwd_filled:
                print("[!] create: could not find Password field in modal")

            # credit
            if credit and credit > 0:
                for sel in [
                    "input[name*='credit']",
                    "input[name*='balance']",
                    "input[placeholder*='Credit' i]",
                ]:
                    el = container.locator(sel).first
                    if await el.count():
                        await el.fill(str(credit))
                        break

            # nickname
            if nickname:
                for sel in [
                    "input[name='nickname']",
                    "input[name='name']",
                    "input[placeholder*='Nick' i]",
                ]:
                    el = container.locator(sel).first
                    if await el.count():
                        await el.fill(nickname)
                        break

            # submit in modal
            for sel in [
                "button[type='submit']",
                "button:has-text('Submit')",
                "button:has-text('Save')",
                ".layui-layer-btn0",
            ]:
                el = container.locator(sel).first
                if await el.count():
                    await el.click()
                    await asyncio.sleep(2.0)
                    print("[+] UI create submitted (modal)")
                    return

            # if reached here, modal was weird -> fall to direct
        else:
            print("[!] create: modal did not appear, falling back to direct page")

    # 3) direct-create page (this is the one that worked in your last run)
    create_url = f"{YOLO_BASE_URL}/admin/player_list/create"
    await page.goto(create_url, wait_until="domcontentloaded")
    # now we should have a normal form
    form = page.locator("form").first
    await form.wait_for(state="visible", timeout=20000)

    # account
    filled = False
    for sel in [
        "input[name='account']",
        "input[name='username']",
        "input[placeholder*='Account' i]",
        "input[placeholder*='User' i]",
    ]:
        el = form.locator(sel).first
        if await el.count():
            await el.fill(account)
            filled = True
            break
    if not filled:
        raise RuntimeError("create: no account field in direct create page")

    # password
    filled_pwd = False
    for sel in [
        "input[name='password']",
        "input[placeholder*='Password' i]",
        "input[type='password']",
    ]:
        el = form.locator(sel).first
        if await el.count():
            await el.fill(password)
            filled_pwd = True
            break
    if not filled_pwd:
        raise RuntimeError("create: no password field in direct create page")

    # credit
    if credit and credit > 0:
        for sel in [
            "input[name*='credit']",
            "input[name*='balance']",
            "input[placeholder*='Credit' i]",
        ]:
            el = form.locator(sel).first
            if await el.count():
                await el.fill(str(credit))
                break

    # nickname
    if nickname:
        for sel in [
            "input[name='nickname']",
            "input[name='name']",
            "input[placeholder*='Nick' i]",
        ]:
            el = form.locator(sel).first
            if await el.count():
                await el.fill(nickname)
                break

    # submit
    for sel in [
        "button[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Save')",
    ]:
        el = form.locator(sel).first
        if await el.count():
            await el.click()
            break

    await asyncio.sleep(2.0)
    print(f"[+] UI create via direct page submitted")


# ------------------------------------------------------------------------------
# SEARCH + EDIT (for recharge/redeem)
# ------------------------------------------------------------------------------

async def ui_open_edit_for_account(page, account: str):
    """
    1. must be on player_list
    2. search for account
    3. open row edit popup/page
    """
    # 1) try search box
    searched = False
    for sel in [
        "input[placeholder*='Search' i]",
        "input[placeholder*='search' i]",
        "input[placeholder*='account' i]",
        ".grid-search .form-control",
        "form .form-control",
    ]:
        inp = page.locator(sel).first
        if await inp.count():
            await inp.fill(account)
            await inp.press("Enter")
            await wait_network_idle(page)
            searched = True
            break

    if not searched:
        # sometimes dcat has toolbar with filters
        await page.evaluate(
            """(acct) => {
                const inputs = Array.from(document.querySelectorAll("input, .form-control"));
                const box = inputs.find(i => /search|account|username/i.test(i.placeholder||""));
                if (box) {
                    box.value = acct;
                    const e = new KeyboardEvent('keydown', {key:'Enter'});
                    box.dispatchEvent(e);
                }
            }""",
            account,
        )
        await asyncio.sleep(1.5)

    # 2) now click "Edit" / row action
    # YOLO/Dcat usually renders actions in '.grid__actions__'
    for sel in [
        f"a:has-text('Edit')",
        f"button:has-text('Edit')",
        ".grid__actions__ a:has-text('Edit')",
        ".dropdown-menu a:has-text('Edit')",
    ]:
        el = page.locator(sel).first
        if await el.count():
            await el.click()
            await wait_network_idle(page)
            return

    # some panels open detail by clicking the row itself
    rows = page.locator("table tbody tr").all()
    if rows:
        for r in rows:
            txt = (await r.inner_text()).strip()
            if account in txt:
                # click the row
                await r.click()
                await wait_network_idle(page)
                return

    raise RuntimeError(f"ui_open_edit_for_account: could not open edit for {account}")


async def ui_recharge(page, account: str, amount: float):
    # must be on player list
    await ui_open_edit_for_account(page, account)

    # now we are inside edit/detail — try to find "Recharge" tab/button
    opened = False
    for sel in [
        "a:has-text('Recharge')",
        "button:has-text('Recharge')",
        "li:has-text('Recharge')",
        "a[href*='recharge']",
    ]:
        el = page.locator(sel).first
        if await el.count():
            await el.click()
            opened = True
            break

    if not opened:
        # maybe it's a separate page /admin/player_list/<id>/recharge – but we don't know id
        raise RuntimeError("ui_recharge: could not find Recharge button/tab")

    await asyncio.sleep(1.0)

    # fill amount
    filled = False
    for sel in [
        "input[name*='amount']",
        "input[placeholder*='Amount' i]",
        "input[type='number']",
    ]:
        el = page.locator(sel).first
        if await el.count():
            await el.fill(str(amount))
            filled = True
            break

    if not filled:
        raise RuntimeError("ui_recharge: could not find amount field")

    # submit
    submitted = False
    for sel in [
        "button[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('OK')",
        ".layui-layer-btn0",
    ]:
        el = page.locator(sel).first
        if await el.count():
            await el.click()
            submitted = True
            break

    if not submitted:
        raise RuntimeError("ui_recharge: could not find submit button")

    await asyncio.sleep(1.0)
    print(f"[+] Recharged {account} by {amount}")


async def ui_redeem(page, account: str, amount: float):
    await ui_open_edit_for_account(page, account)

    opened = False
    for sel in [
        "a:has-text('Redeem')",
        "a:has-text('Withdraw')",
        "button:has-text('Redeem')",
        "button:has-text('Withdraw')",
    ]:
        el = page.locator(sel).first
        if await el.count():
            await el.click()
            opened = True
            break

    if not opened:
        raise RuntimeError("ui_redeem: could not find Redeem/Withdraw button/tab")

    await asyncio.sleep(1.0)

    filled = False
    for sel in [
        "input[name*='amount']",
        "input[placeholder*='Amount' i]",
        "input[type='number']",
    ]:
        el = page.locator(sel).first
        if await el.count():
            await el.fill(str(amount))
            filled = True
            break
    if not filled:
        raise RuntimeError("ui_redeem: could not find amount field")

    submitted = False
    for sel in [
        "button[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('OK')",
        ".layui-layer-btn0",
    ]:
        el = page.locator(sel).first
        if await el.count():
            await el.click()
            submitted = True
            break
    if not submitted:
        raise RuntimeError("ui_redeem: could not find submit button")

    await asyncio.sleep(1.0)
    print(f"[+] Redeemed {amount} from {account}")


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

async def main():
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python automation/yolo_bot.py create")
        print("  python automation/yolo_bot.py recharge <account> <amount>")
        print("  python automation/yolo_bot.py redeem <account> <amount>")
        return

    mode = sys.argv[1]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, slow_mo=SLOWMO)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        await do_login(page)

        if mode == "create":
            await open_player_list(page)
            acct = build_new_username()
            pwd = DEFAULT_PASS
            await ui_create_player(page, acct, pwd, DEFAULT_CREDIT, nickname=acct)
            print(
                {
                    "ok": True,
                    "account": acct,
                    "password": pwd,
                    "credit": DEFAULT_CREDIT,
                }
            )

        elif mode == "recharge":
            if len(sys.argv) < 4:
                raise SystemExit("recharge needs: account amount")
            acct = sys.argv[2]
            amt = float(sys.argv[3])
            await open_player_list(page)
            await ui_recharge(page, acct, amt)

        elif mode == "redeem":
            if len(sys.argv) < 4:
                raise SystemExit("redeem needs: account amount")
            acct = sys.argv[2]
            amt = float(sys.argv[3])
            await open_player_list(page)
            await ui_redeem(page, acct, amt)

        else:
            print("unknown mode")

        await asyncio.sleep(2)
        await browser.close()
        
        # ------------------------------------------------------------------------------
# SYNC WRAPPERS for Flask (auto_create_sync / recharge_sync / redeem_sync)
# ------------------------------------------------------------------------------

def auto_create_sync():
    """Used by Flask to auto-create YOLO accounts (same as CLI 'create')."""
    import asyncio
    async def _run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, slow_mo=SLOWMO)
            page = await browser.new_page()
            await do_login(page)
            await open_player_list(page)
            acct = build_new_username()
            pwd = DEFAULT_PASS
            await ui_create_player(page, acct, pwd, DEFAULT_CREDIT, nickname=acct)
            await browser.close()
            return {"ok": True, "account": acct, "password": pwd, "credit": DEFAULT_CREDIT}
    return asyncio.run(_run())


def recharge_sync(account: str, amount: float, note: str = ""):
    """Used by Flask to load balance for YOLO accounts."""
    import asyncio
    async def _run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, slow_mo=SLOWMO)
            page = await browser.new_page()
            await do_login(page)
            await open_player_list(page)
            await ui_recharge(page, account, amount)
            await browser.close()
            return {"ok": True, "account": account, "amount": amount}
    return asyncio.run(_run())


def redeem_sync(account: str, amount: float, note: str = ""):
    """Used by Flask to withdraw balance for YOLO accounts."""
    import asyncio
    async def _run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, slow_mo=SLOWMO)
            page = await browser.new_page()
            await do_login(page)
            await open_player_list(page)
            await ui_redeem(page, account, amount)
            await browser.close()
            return {"ok": True, "account": account, "amount": amount}
    return asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(main())