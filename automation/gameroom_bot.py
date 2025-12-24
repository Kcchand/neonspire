#!/usr/bin/env python3
import os
import sys
import time
import json
import base64
import random
import string
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ------------------------------------------------------------------------------
# ENV
# ------------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

GR_BASE_URL = os.getenv("GAMEROOM_BASE_URL", "https://agentserver1.gameroom777.com").rstrip("/")
GR_LOGIN_URL = os.getenv("GAMEROOM_LOGIN_URL", f"{GR_BASE_URL}/admin/login")
GR_USER = os.getenv("GAMEROOM_USER", "")
GR_PASS = os.getenv("GAMEROOM_PASS", "")
GR_2CAPTCHA_KEY = (
    os.getenv("GR_2CAPTCHA_KEY")
    or os.getenv("TWOCAPTCHA_API_KEY")
    or os.getenv("TWO_CAPTCHA_APIKEY")
    or ""
)

HEADLESS = os.getenv("GAMEROOM_HEADLESS", "false").lower() == "true"
SLOWMO = int(os.getenv("GAMEROOM_SLOWMO_MS", "120"))

ACCT_PREFIX = os.getenv("GAMEROOM_USERNAME_PREFIX", "gr_")
ACCT_SUFFIX = os.getenv("GAMEROOM_USERNAME_SUFFIX", "_gm")
FIXED_LEN = os.getenv("GAMEROOM_USERNAME_FIXED_LEN")

DEFAULT_PASS = os.getenv("GAMEROOM_DEFAULT_PASSWORD", "Abc123456")
DEFAULT_CREDIT = int(os.getenv("GAMEROOM_DEFAULT_FIRST_CREDIT", "0"))

# ------------------------------------------------------------------------------
# 2CAPTCHA
# ------------------------------------------------------------------------------
def solve_2captcha(api_key: str, img_bytes: bytes) -> str | None:
    if not api_key:
        return None
    import urllib.request, urllib.parse

    data = {
        "key": api_key,
        "method": "base64",
        "json": 1,
        "body": base64.b64encode(img_bytes).decode("ascii"),
    }
    data_enc = urllib.parse.urlencode(data).encode("ascii")

    try:
        with urllib.request.urlopen("http://2captcha.com/in.php", data=data_enc, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print("[gameroom][2captcha] upload error:", e)
        return None

    try:
        j = json.loads(raw)
        if j.get("status") != 1:
            print("[gameroom][2captcha] bad upload:", j)
            return None
        cap_id = j["request"]
    except Exception:
        print("[gameroom][2captcha] unexpected upload resp:", raw)
        return None

    for _ in range(15):
        time.sleep(3)
        try:
            with urllib.request.urlopen(
                f"http://2captcha.com/res.php?key={api_key}&action=get&id={cap_id}&json=1",
                timeout=25,
            ) as resp:
                raw2 = resp.read().decode("utf-8")
        except Exception as e:
            print("[gameroom][2captcha] poll error:", e)
            return None

        try:
            j2 = json.loads(raw2)
        except Exception:
            continue

        if j2.get("status") == 1:
            return j2["request"]
        if j2.get("request") == "CAPCHA_NOT_READY":
            continue
        print("[gameroom][2captcha] solver error:", j2)
        return None

    return None

# ------------------------------------------------------------------------------
# UTILS
# ------------------------------------------------------------------------------
def _rand_str(n: int) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))

def build_new_username() -> str:
    if FIXED_LEN:
        try:
            ln = int(FIXED_LEN)
            if ln > 0:
                return _rand_str(ln)
        except ValueError:
            pass
    mid = _rand_str(5)
    return f"{ACCT_PREFIX}{mid}{ACCT_SUFFIX}"

async def wait_idle(page_or_frame, timeout=12000):
    try:
        await page_or_frame.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        pass

def to_int_string(val) -> str:
    try:
        f = float(val)
    except Exception:
        return "0"
    return str(int(f))

# ------------------------------------------------------------------------------
# POPUP HELPERS
# ------------------------------------------------------------------------------
async def close_success_popup(page, timeout_ms: int = 1500):
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        layers = page.locator(".layui-layer")
        cnt = await layers.count()
        for i in range(cnt):
            box = layers.nth(i)
            try:
                txt = await box.inner_text()
            except Exception:
                txt = ""
            if "GAMEROOM" in (txt or "").upper():
                for sel in [
                    ".layui-layer-btn button:has-text('Close')",
                    ".layui-layer-btn .layui-layer-btn1",
                    "button:has-text('关闭')",
                ]:
                    btn = box.locator(sel)
                    if await btn.count():
                        try:
                            await btn.first.click()
                            await asyncio.sleep(0.05)
                            await page.evaluate(
                                "document.querySelectorAll('div.layui-layer-shade').forEach(e => e.remove());"
                            )
                            return
                        except Exception:
                            pass
                try:
                    await page.evaluate("(el)=>el.remove()", box)
                except Exception:
                    pass
                try:
                    await page.evaluate(
                        "document.querySelectorAll('div.layui-layer-shade').forEach(e => e.remove());"
                    )
                except Exception:
                    pass
                return
        try:
            await page.evaluate(
                "document.querySelectorAll('div.layui-layer-shade').forEach(e => e.remove());"
            )
        except Exception:
            pass
        await asyncio.sleep(0.05)

async def close_all_layers(page, timeout_ms: int = 1500):
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        did = False
        for sel in [
            "button:has-text('Close')",
            "button:has-text('关闭')",
            ".layui-layer-btn1",
            ".layui-layer-setwin .layui-layer-close1",
            ".layui-layer-setwin .layui-layer-close",
        ]:
            loc = page.locator(sel)
            if await loc.count():
                try:
                    await loc.first.click()
                    did = True
                except Exception:
                    pass
        try:
            await page.evaluate(
                "document.querySelectorAll('div.layui-layer-shade').forEach(e => e.remove());"
            )
        except Exception:
            pass
        if not did:
            return
        await asyncio.sleep(0.05)

# ------------------------------------------------------------------------------
# LOGIN
# ------------------------------------------------------------------------------
async def do_login(page):
    print("[gameroom] 2captcha key:", "SET" if GR_2CAPTCHA_KEY else "EMPTY")
    await page.goto(GR_LOGIN_URL, wait_until="domcontentloaded")

    await page.fill("input[name='username'], input[placeholder*='user' i]", GR_USER)
    await page.fill("input[name='password'], input[placeholder*='pass' i]", GR_PASS)

    canvas = page.locator("#captchaImg canvas")
    try:
        await canvas.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        try:
            await page.evaluate("if (window.verifyCode) window.verifyCode.refresh();")
        except Exception:
            pass
        await canvas.wait_for(state="visible", timeout=8000)

    img_bytes = await canvas.screenshot()
    print("[gameroom] captcha canvas captured:", len(img_bytes), "bytes")

    solved = None
    if GR_2CAPTCHA_KEY:
        solved = solve_2captcha(GR_2CAPTCHA_KEY, img_bytes)
        print("[gameroom] 2captcha replied:", solved)

    if not solved:
        raise RuntimeError("2captcha did not solve captcha")

    import re
    solved_clean = re.sub(r"\D", "", solved)[:4]
    if not solved_clean:
        raise RuntimeError("2captcha returned non-digit captcha")

    await page.fill("input[name='captcha'], input[lay-verify*='captcha']", solved_clean)

    for sel in (
        "button[lay-filter='login']",
        ".layui-btn[lay-submit]",
        "button:has-text('Login')",
    ):
        if await page.locator(sel).count():
            await page.locator(sel).click()
            break

    try:
        await page.wait_for_url(f"{GR_BASE_URL}/admin", timeout=15000)
        print("[gameroom] Logged in ✅ (/admin)")
        return
    except Exception:
        pass

    if page.url.startswith(f"{GR_BASE_URL}/admin"):
        print("[gameroom] Logged in ✅ (current url was already /admin)")
        return

    raise RuntimeError(f"login failed, current url = {page.url}")

# ------------------------------------------------------------------------------
# OPEN USER MANAGEMENT
# ------------------------------------------------------------------------------
async def open_user_management(page):
    await page.evaluate(
        """
        () => {
            const nodes = Array.from(document.querySelectorAll('li, a, span, cite'));
            const g = nodes.find(n => n.textContent && n.textContent.trim().toLowerCase() === 'game user');
            if (g) g.click();
        }
        """
    )
    await asyncio.sleep(0.25)
    await page.evaluate(
        """
        () => {
            const nodes = Array.from(document.querySelectorAll('li, a, span, cite'));
            const u = nodes.find(n => n.textContent && n.textContent.trim().toLowerCase() === 'user management');
            if (u) u.click();
        }
        """
    )
    await asyncio.sleep(0.6)
    await wait_idle(page)
    return page

# ------------------------------------------------------------------------------
# get list frame
# ------------------------------------------------------------------------------
async def get_user_list_frame(page):
    for f in page.frames:
        if f is page.main_frame:
            continue
        url = f.url or ""
        if "/admin/player/index" in url or "player/index" in url or "user" in url.lower():
            try:
                if await f.locator("input[placeholder*='Username' i], input[placeholder*='user name' i]").count():
                    return f
            except Exception:
                pass
    return page

# ------------------------------------------------------------------------------
# get popup iframe (recharge / withdraw)
# ------------------------------------------------------------------------------
async def get_popup_iframe(page, kind: str, timeout_sec: float = 3.0):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for f in page.frames:
            if f is page.main_frame:
                continue
            url = f.url or ""
            if kind == "recharge" and "/admin/player/recharge" in url:
                return f
            if kind == "withdraw" and "/admin/player/withdraw" in url:
                return f
        await asyncio.sleep(0.1)
    return None

# ------------------------------------------------------------------------------
# CREATE USER (same)
# ------------------------------------------------------------------------------
async def ui_create_user(page, account, password, credit, nickname=None):
    await open_user_management(page)
    list_frame = await get_user_list_frame(page)

    clicked = False
    for sel in [
        "button.layui-btn:has-text('Add user')",
        "button.layui-btn:has-text('Add User')",
        "a.layui-btn:has-text('Add user')",
        "a.layui-btn:has-text('Add User')",
    ]:
        if await list_frame.locator(sel).count():
            await list_frame.locator(sel).first.click()
            clicked = True
            break
    if not clicked:
        raise RuntimeError("Add user button not found")

    form_iframe = None
    deadline = time.time() + 9
    while time.time() < deadline:
        for f in page.frames:
            if f is page.main_frame:
                continue
            if "/admin/player/insert" in (f.url or ""):
                form_iframe = f
                break
        if form_iframe:
            break
        await asyncio.sleep(0.2)

    if not form_iframe:
        raise RuntimeError("Add User popup did not appear (no /admin/player/insert iframe)")

    for sel in ["input[name='username']", "input[placeholder*='username' i]"]:
        if await form_iframe.locator(sel).count():
            await form_iframe.locator(sel).fill(account)
            break

    final_nickname = nickname or account
    for sel in ["input[name='nickname']", "input[name='nickName']", "input[placeholder*='nickname' i]"]:
        if await form_iframe.locator(sel).count():
            await form_iframe.locator(sel).fill(final_nickname)
            break

    for sel in [
        "input[placeholder*='Recharge Balance' i]",
        "input[name*='recharge' i]",
        "input[name='money']",
    ]:
        if await form_iframe.locator(sel).count():
            await form_iframe.locator(sel).fill(str(credit if credit else 0))
            break

    pw_inputs = form_iframe.locator("input[type='password']")
    if await pw_inputs.count() >= 1:
        await pw_inputs.nth(0).fill(password)
    if await pw_inputs.count() >= 2:
        await pw_inputs.nth(1).fill(password)

    submitted = False
    for sel in [
        "button.layui-btn:has-text('Submit')",
        ".layui-btn.layui-btn-normal:has-text('Submit')",
        "button[type='submit']",
    ]:
        if await form_iframe.locator(sel).count():
            await form_iframe.locator(sel).first.click()
            submitted = True
            break
    if not submitted:
        await form_iframe.press("input", "Enter")

    await close_success_popup(page, 1500)
    await close_all_layers(page, 800)

    print(f"[gameroom] user created -> {account} / {password}")
    return {"account": account, "password": password, "credit": credit if credit else 0}

# ------------------------------------------------------------------------------
# SEARCH USER
# ------------------------------------------------------------------------------
async def find_user_row(page, account: str):
    await open_user_management(page)

    async def try_in(target):
        for sel in ["input[placeholder*='Username' i]", "input[placeholder*='user name' i]"]:
            if await target.locator(sel).count():
                await target.locator(sel).fill(account)
                break
        for sel in ["button:has-text('Search')", "button[lay-filter='search']", ".layui-btn[lay-filter='search']"]:
            if await target.locator(sel).count():
                await target.locator(sel).click()
                break
        await asyncio.sleep(0.6)
        rows = target.locator("table tbody tr")
        return rows

    rows = await try_in(page)
    if await rows.count() > 0:
        await rows.nth(0).click()
        await asyncio.sleep(0.2)
        return page, rows.nth(0)

    for f in page.frames:
        if f is page.main_frame:
            continue
        try:
            rows = await try_in(f)
            if await rows.count() > 0:
                await rows.nth(0).click()
                await asyncio.sleep(0.2)
                return f, rows.nth(0)
        except Exception:
            continue

    raise RuntimeError(f"user {account} not found in gameroom")

# ------------------------------------------------------------------------------
# RECHARGE (unchanged from your last version)
# ------------------------------------------------------------------------------
async def ui_recharge(page, account: str, amount: float):
    amount_str = to_int_string(amount)

    frame, row = await find_user_row(page, account)

    try:
        await frame.evaluate("const t=document.querySelector('.layui-table-main'); if(t) t.scrollLeft=9999;")
    except Exception:
        pass

    clicked = False
    if row is not None:
        try:
            btn = row.locator("text=Recharge")
            if await btn.count():
                await btn.first.click()
                clicked = True
        except Exception:
            pass
    if not clicked:
        btn = frame.locator("text=Recharge")
        if await btn.count():
            await btn.first.click()
            clicked = True

    if not clicked:
        raise RuntimeError("could not click Recharge button")

    popup_iframe = await get_popup_iframe(page, "recharge", 3.0)
    if popup_iframe is None:
        raise RuntimeError("recharge iframe did not appear")

    if await popup_iframe.locator("#recharge_balance").count():
        field = popup_iframe.locator("#recharge_balance")
        await field.fill("")
        await field.fill(amount_str)
        try:
            await popup_iframe.evaluate(
                """() => {
                    const inp = document.getElementById('recharge_balance');
                    if (inp) {
                        inp.dispatchEvent(new Event('input', {bubbles:true}));
                        inp.dispatchEvent(new Event('change', {bubbles:true}));
                        inp.dispatchEvent(new Event('blur', {bubbles:true}));
                    }
                }"""
            )
        except Exception:
            pass
    else:
        for sel in [
            "input[placeholder*='Recharge Balance' i]",
            "input[name='rechargeMoney']",
            "input[name='amount']",
        ]:
            if await popup_iframe.locator(sel).count():
                await popup_iframe.locator(sel).fill(amount_str)
                break

    if await popup_iframe.locator("input[placeholder*='Remarks' i], input[name='remarks']").count():
        await popup_iframe.locator("input[placeholder*='Remarks' i], input[name='remarks']").first.fill("loaded")

    submitted = False
    for sel in [
        "button:has-text('Submit')",
        "button.layui-btn",
        ".layui-layer-btn0",
        "button[lay-filter='submit']",
    ]:
        if await popup_iframe.locator(sel).count():
            await popup_iframe.locator(sel).first.click()
            submitted = True
            break
    if not submitted:
        try:
            await popup_iframe.evaluate(
                """() => {
                    const b = Array.from(document.querySelectorAll('button')).find(x=>x.innerText.trim()==='Submit');
                    if (b) b.click();
                }"""
            )
        except Exception:
            pass

    await asyncio.sleep(0.8)

    err_text = ""
    try:
        err_text = await popup_iframe.inner_text()
    except Exception:
        err_text = ""

    if "The balance must be an integer" in err_text:
        raise RuntimeError("Gameroom rejected recharge: The balance must be an integer")

    await close_success_popup(page, 1500)
    await close_all_layers(page, 1500)

    print(f"[gameroom] recharged {account} by {amount_str}")

# ------------------------------------------------------------------------------
# REDEEM (ONLY THIS CHANGED)
# ------------------------------------------------------------------------------
async def ui_redeem(page, account: str, amount: float):
    """
    Withdraw popup is also an iframe: /admin/player/withdraw
    In your screenshot the input said: 'Please enter Withdraw Balance'
    and the site complained: 'Required item cannot be blank'
    → that means we weren't typing INSIDE the iframe.
    """
    amount_str = to_int_string(amount)

    # 1) find the row like before
    frame, row = await find_user_row(page, account)

    # 2) scroll to actions
    try:
        await frame.evaluate("const t=document.querySelector('.layui-table-main'); if(t) t.scrollLeft=9999;")
    except Exception:
        pass

    # 3) click Withdraw / Redeem on that row
    clicked = False
    if row is not None:
        for text in ["Withdraw", "Redeem"]:
            btn = row.locator(f"text={text}")
            if await btn.count():
                await btn.first.click()
                clicked = True
                break
    if not clicked:
        for text in ["Withdraw", "Redeem"]:
            btn = frame.locator(f"text={text}")
            if await btn.count():
                await btn.first.click()
                clicked = True
                break

    if not clicked:
        raise RuntimeError("could not click Withdraw/Redeem button")

    # 4) get the REAL withdraw iframe
    popup_iframe = await get_popup_iframe(page, "withdraw", 3.0)
    if not popup_iframe:
        # last fallback — but your UI DOES use iframe, so this should not hit
        target = page
    else:
        target = popup_iframe

    # 5) fill withdraw amount (try the most specific one first)
    filled = False
    for sel in [
        "#withdraw_balance",                              # likely id
        "input[name='withdraw_money']",                   # common name
        "input[placeholder*='Withdraw Balance' i]",       # what your screenshot shows
        "input[name='amount']",
        "input[type='number']",
    ]:
        if await target.locator(sel).count():
            await target.locator(sel).fill(amount_str)
            filled = True
            # trigger layui validation
            try:
                await target.evaluate(
                    """(sel)=>{
                        const inp = document.querySelector(sel);
                        if (inp){
                            inp.dispatchEvent(new Event('input',{bubbles:true}));
                            inp.dispatchEvent(new Event('change',{bubbles:true}));
                            inp.dispatchEvent(new Event('blur',{bubbles:true}));
                        }
                    }""",
                    sel,
                )
            except Exception:
                pass
            break

    if not filled:
        raise RuntimeError("could not find Withdraw Balance input inside popup")

    # 6) remarks
    for sel in [
        "input[placeholder*='Remarks' i]",
        "input[name='remarks']",
    ]:
        if await target.locator(sel).count():
            await target.locator(sel).fill("redeem")
            break

    # 7) click Submit INSIDE iframe
    submitted = False
    for sel in [
        "button:has-text('Submit')",
        ".layui-btn:has-text('Submit')",
        ".layui-layer-btn0",
        "button[lay-filter='submit']",
    ]:
        if await target.locator(sel).count():
            await target.locator(sel).first.click()
            submitted = True
            break

    if not submitted:
        # JS fallback
        try:
            await target.evaluate(
                """() => {
                    const b = Array.from(document.querySelectorAll('button')).find(x=>x.innerText.trim()==='Submit');
                    if (b) b.click();
                }"""
            )
        except Exception:
            pass

    # 8) small wait for server-side validation ("Required item cannot be blank")
    await asyncio.sleep(0.8)

    # check if still complaining
    try:
        inner_txt = await target.inner_text()
    except Exception:
        inner_txt = ""

    if "Required item cannot be blank" in inner_txt:
        raise RuntimeError("Gameroom rejected withdraw: Required item cannot be blank (field not filled)")

    # 9) close popups
    await close_success_popup(page, 1500)
    await close_all_layers(page, 1500)

    print(f"[gameroom] redeemed {amount_str} from {account}")

# ------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------
async def _amain():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python automation/gameroom_bot.py create")
        print("  python automation/gameroom_bot.py recharge <account> <amount>")
        print("  python automation/gameroom_bot.py redeem <account> <amount>")
        return

    mode = sys.argv[1]
    os.environ["CURRENT_VENDOR"] = "gameroom"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=SLOWMO, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        try:
            if mode == "create":
                await do_login(page)
                acct = build_new_username()
                pwd = DEFAULT_PASS
                info = await ui_create_user(page, acct, pwd, DEFAULT_CREDIT, nickname=acct)
                print(
                    f"✅ Gameroom user created successfully | account={info['account']} | "
                    f"password={info['password']} | balance={info['credit']}"
                )

            elif mode == "recharge":
                await do_login(page)
                acct = sys.argv[2]
                amt = float(sys.argv[3])
                await ui_recharge(page, acct, amt)
                print(f"✅ Gameroom recharge done | account={acct} | amount={to_int_string(amt)}")

            elif mode == "redeem":
                await do_login(page)
                acct = sys.argv[2]
                amt = float(sys.argv[3])
                await ui_redeem(page, acct, amt)
                print(f"✅ Gameroom redeem done | account={acct} | amount={to_int_string(amt)}")

        finally:
            try:
                await browser.close()
            except Exception:
                pass
            
            
            # ------------------------------------------------------------------------------
# SYNC WRAPPERS FOR FLASK (imported by player_bp.py)
# ------------------------------------------------------------------------------

def auto_create_sync() -> dict:
    """
    Synchronous wrapper used by Flask to auto-provision a Gameroom account.
    Returns: {"ok": True/False, "account": "...", "password": "...", "note": "..."}
    """
    return asyncio.run(_create_sync_inner())


async def _create_sync_inner() -> dict:
    os.environ["CURRENT_VENDOR"] = "gameroom"   # 👈 add this
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=SLOWMO, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            await do_login(page)
            acct = build_new_username()
            pwd = DEFAULT_PASS
            info = await ui_create_user(page, acct, pwd, DEFAULT_CREDIT, nickname=acct)
            return {
                "ok": True,
                "account": info.get("account", acct),
                "password": info.get("password", pwd),
                "note": "Auto-provisioned via Gameroom bot",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def recharge_sync(account: str, amount: float, remark: str = "") -> dict:
    """
    Synchronous wrapper used by Flask to load balance to a Gameroom user.
    """
    return asyncio.run(_recharge_sync_inner(account, amount, remark))


async def _recharge_sync_inner(account: str, amount: float, remark: str = "") -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=SLOWMO, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            await do_login(page)
            await ui_recharge(page, account, amount)
            return {"ok": True, "account": account, "amount": int(amount)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def redeem_sync(account: str, amount: float, remark: str = "") -> dict:
    """
    Synchronous wrapper used by Flask to withdraw from a Gameroom user.
    """
    return asyncio.run(_redeem_sync_inner(account, amount, remark))


async def _redeem_sync_inner(account: str, amount: float, remark: str = "") -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=SLOWMO, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            await do_login(page)
            await ui_redeem(page, account, amount)
            return {"ok": True, "account": account, "amount": int(amount)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            try:
                await browser.close()
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(_amain())