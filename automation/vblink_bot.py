"""
Vblink UI Bot (Playwright) — FINAL VERIFIED

Features
- login (auto-confirm Hint dialog)
- create user (username starts with 4 letters + 2 digits, padded to 7–16; auto-retry on duplicate only)
- verify user (Search User → Player account; scrape row details)
- recharge (Search User → Player account → Set Score +amount)
- redeem   (Search User → Player account → Set Score -amount)

CLI:
  python automation/vblink_bot.py login
  python automation/vblink_bot.py create <account?> <password?>
  python automation/vblink_bot.py create-auto --count N [--password Ab123456]
  python automation/vblink_bot.py recharge <account> <amount> [remark?]
  python automation/vblink_bot.py redeem   <account> <amount> [remark?]
  python automation/vblink_bot.py test
"""

from __future__ import annotations
import os, sys, re, json, random, string, asyncio
from typing import Optional, Dict, Any, Tuple
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Locator

# ---------------- ENV ----------------
load_dotenv()
BASE = os.getenv("VBLINK_BASE_URL", "https://gm.vblink777.club").rstrip("/")
ADMIN_USER = os.getenv("VBLINK_USERNAME", "")
ADMIN_PASS = os.getenv("VBLINK_PASSWORD", "")

HEADLESS = os.getenv("VBLINK_HEADLESS", "0").lower() in ("1","true","yes","on")
SLOW_MO  = int(os.getenv("VBLINK_SLOWMO_MS", "0") or "0")
DEFAULT_PWD    = os.getenv("VB_DEFAULT_PASSWORD", "Ab123456")
DEFAULT_REMARK = os.getenv("VB_DEFAULT_REMARK", "auto create")
CREATE_SET_SCORE = int(os.getenv("VB_CREATE_SET_SCORE", "100") or "100")  # tenant requires initial points

LOGIN_URL  = f"{BASE}/#/login"
INDEX_URL  = f"{BASE}/#/index"
UM_URL     = f"{BASE}/#/manage-user/account"
SEARCH_URL = f"{BASE}/#/manage-user/search"

# ---------------- Helpers ----------------
def _rand(n=1, alphabet=string.ascii_lowercase+string.digits):
    return "".join(random.choice(alphabet) for _ in range(n))

def gen_username_from_rule() -> str:
    """First 6 chars: 4 letters + 2 digits. Pad to >=7 (max 16)."""
    letters = _rand(4, string.ascii_lowercase)
    digits  = _rand(2, string.digits)
    name = letters + digits
    if len(name) < 7: name += _rand(1)
    return name[:16]

def sanitize_username(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", s or "")
    if len(s) < 6: s += _rand(6 - len(s))
    lead = s[:6].lower()
    if not re.match(r"^[a-z]{4}\d{2}$", lead):
        s = gen_username_from_rule() + s[6:]
    if len(s) < 7: s += _rand(7 - len(s))
    return s[:16]

async def launch_browser():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        slow_mo=SLOW_MO,
        args=["--disable-blink-features=AutomationControlled"]
    )
    ctx = await browser.new_context(viewport={"width": 1368, "height": 900}, ignore_https_errors=True)
    page = await ctx.new_page()
    page.set_default_timeout(15000)
    return pw, browser, ctx, page

async def close_browser(bundle):
    if not bundle: return
    pw,b,ctx,_ = bundle
    for fn in (ctx.close, b.close, pw.stop):
        try: await fn()
        except: pass

async def _wait_spa(page: Page, url_sub: str, timeout: int = 15000):
    if url_sub not in page.url:
        try: await page.wait_for_url(f"**{url_sub}**", timeout=timeout)
        except: pass
    try: await page.wait_for_load_state("networkidle", timeout=timeout)
    except: await page.wait_for_timeout(150)

async def _force_confirm_modal(page: Page):
    """Auto-dismiss 'Hint' or other confirm/OK modals that can block clicks."""
    try:
        for sel in (
            "button:has-text('confirm')",
            "button:has-text('Confirm')",
            "button:has-text('OK')",
            "button:has-text('Ok')",
            "button:has-text('确定')",
            ".el-message-box__btns button:has-text('OK')",
        ):
            b = page.locator(sel).first
            if await b.count():
                try: await b.click(force=True, timeout=600)
                except: pass
        rb = page.get_by_role("button", name=re.compile(r"^(confirm|ok|确定)$", re.I)).first
        if await rb.count():
            try: await rb.click(timeout=600)
            except: pass
        await page.evaluate("""
(() => {
  const labels = ['confirm','Confirm','OK','Ok','确定'];
  for (const el of document.querySelectorAll('button,.el-button,.ant-btn')) {
    const t=(el.innerText||'').trim(); if(labels.includes(t)) { try{el.click()}catch(e){} }
  }
})();
""")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(120)
    except: pass

async def _goto(page: Page, url: str, sub: str):
    try:
        print(f"[nav] Opening {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"[warn] First goto({url}) failed: {e}. Retrying once...")
        await page.goto(url, wait_until="load", timeout=60000)
    await _wait_spa(page, sub, 30000)
    await _force_confirm_modal(page)

# ------- Toast / error helpers -------
async def _any_error_text(page: Page) -> str:
    for sel in (
        ".el-message--error, .ant-message-error, .el-notification--error, .el-message__content",
        ".el-form-item__error, .ant-form-item-explain-error",
        ".el-message-box__message, .ant-modal .ant-modal-body",
    ):
        loc = page.locator(sel).first
        if await loc.count():
            try:
                return (await loc.text_content() or "").strip()
            except:
                pass
    return ""

def _non_blocking_banner(msg: str) -> bool:
    low = msg.lower()
    return (
        ("error: 52" in low and "no restric" in low) or
        ("error: 22" in low and "transfer" in low)
    )

# ---------------- Login ----------------
async def login(page: Page):
    await _goto(page, LOGIN_URL, "#/login")
    if "#/login" in page.url:
        u = None
        for sel in (
            "input[placeholder*='User Name' i]",
            "input[placeholder*='Account' i]",
            "input[placeholder*='account' i]",
            "input[type='text']"
        ):
            loc = page.locator(sel).first
            if await loc.count(): u = loc; break
        p = page.locator("input[type='password']").first
        if not (u and await p.count()):
            return {"ok": False, "error":"login fields not found", "url": page.url}
        await u.fill(ADMIN_USER)
        await p.fill(ADMIN_PASS)
        for sel in ("button:has-text('Login')","button[type='submit']","button:has-text('OK')"):
            b = page.locator(sel).first
            if await b.count():
                await b.click(); break
    await _wait_spa(page, "#/index", 30000)
    await _force_confirm_modal(page)
    return {"ok": "#/index" in page.url, "url": page.url}

# ---------------- Navigation ----------------
async def goto_user_management(page: Page):
    await _goto(page, UM_URL, "#/manage-user/account")

async def goto_search_user(page: Page):
    await _goto(page, SEARCH_URL, "#/manage-user/search")

# ---------------- UI finders ----------------
async def _find_add_player_button(page: Page):
    for _ in range(24):
        for sel in ("a:has-text('Add Player')","button:has-text('Add Player')","text=Add Player"):
            btn = page.locator(sel).first
            if await btn.count():
                try:
                    await btn.scroll_into_view_if_needed()
                    if await btn.is_visible(): return btn
                except: pass
        await page.mouse.wheel(0, 950)
        await page.wait_for_timeout(120)
    await page.evaluate("""
(() => {
  for (const el of document.querySelectorAll('a,button,span')) {
    if ((el.innerText||'').trim() === 'Add Player') { el.scrollIntoView(); break; }
  }
})();
""")
    return page.locator("a:has-text('Add Player'), button:has-text('Add Player')").first

def _field_after(page: Page, label_text: str):
    return page.locator(f"text={label_text}").locator("..").locator("input,textarea").first

# ---------- Low-level input setter ----------
async def _set_input_via_handle(page: Page, handle, value: str):
    await page.evaluate(
        """([node, val]) => {
            const desc = Object.getOwnPropertyDescriptor(node.__proto__, 'value')
                      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            desc.set.call(node, String(val));
            node.dispatchEvent(new Event('input', {bubbles:true}));
            node.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        [handle, str(value)]
    )

async def _set_score_value(page: Page, value: int):
    value = max(1, int(value))
    lab_input = page.locator("text=Set Score").locator("..").locator("input").first
    if await lab_input.count():
        h = await lab_input.element_handle()
        await _set_input_via_handle(page, h, str(value))
    else:
        candidates = await page.locator("form input[type='number'], form input").element_handles()
        for h in candidates[:8]:
            try:
                iv = await page.evaluate("(n)=>n.value", h)
                t  = (await page.evaluate("(n)=>n.getAttribute('type')||''", h) or "").lower()
                if t in ("number","") and (iv.strip() in ("","0")):
                    await _set_input_via_handle(page, h, str(value)); break
            except: pass
        else:
            gen = page.locator("input[placeholder*='ie 100' i], input[type='number']").first
            h = await gen.element_handle()
            if not h: raise RuntimeError("Set Score input not found")
            await _set_input_via_handle(page, h, str(value))
    try: await page.keyboard.press("Tab")
    except: pass
    await page.wait_for_timeout(120)

# ---------- Add Player helpers ----------
async def _find_add_form(page: Page):
    card = page.locator(".el-card").filter(has_text="Add Player").first
    if await card.count(): return card
    return page.locator("form:has(button:has-text('OK'))").first

async def _click_ok_in_add_form(page: Page):
    form = await _find_add_form(page)
    if not await form.count(): raise RuntimeError("Add Player form not found")
    ok = form.locator("button:has-text('OK')").last
    try:
        await ok.scroll_into_view_if_needed()
        await ok.click(timeout=2000)
    except:
        await _force_confirm_modal(page)
        try:
            await ok.click(timeout=2000, force=True)
        except:
            h = await ok.element_handle()
            if not h: raise
            await page.evaluate("(el)=>el.click()", h)

# ---------------- Search & verify ----------------
async def search_user_and_details(page: Page, account: str) -> Optional[Dict[str, Any]]:
    await goto_search_user(page)
    # ensure "Player account"
    try:
        await page.get_by_role("radio", name=re.compile(r"Player account", re.I)).check()
    except:
        lab = page.locator("label:has-text('Player account'), span:has-text('Player account')").first
        if await lab.count(): await lab.click()

    # search box + OK
    q = None
    for sel in ("input[placeholder*='search' i]","input[placeholder*='account' i]","input[placeholder*='user' i]"):
        loc = page.locator(sel).first
        if await loc.count(): q = loc; break
    if not q: return None
    await q.fill(""); await q.type(account)
    await page.get_by_role("button", name=re.compile(r"^OK$", re.I)).first.click()
    await page.wait_for_timeout(350)

    row = page.locator("table tr").filter(has_text=account).first
    if not await row.count():
        return None

    tds = row.locator("td")
    data: Dict[str, Any] = {"username": account}
    try: data["level"] = (await tds.nth(2).inner_text() or "").strip()
    except: pass
    try: data["upline"] = (await tds.nth(3).inner_text() or "").strip()
    except: pass
    try: data["score"] = (await tds.nth(4).inner_text() or "").strip()
    except: pass
    try: data["invite_code"] = (await tds.nth(5).inner_text() or "").strip()
    except: pass
    try: data["name"] = (await tds.nth(6).inner_text() or "").strip()
    except: pass
    return data

# ---------------- Create user (stable) ----------------
async def create_user(page: Page, account: Optional[str]=None, password: Optional[str]=None, init_score: Optional[int]=None) -> dict:
    """
    Stable create flow:
      - Go to User Management → Add Player (no Search loops)
      - Fill username/password; set Set Score via JS setter
      - Submit OK (scoped to the Add Player form)
      - Wait for success toast; verify once in Search User
      - Retry new username only on duplicate/exists
    """
    password = password or DEFAULT_PWD
    need_score = int(init_score if init_score is not None else CREATE_SET_SCORE)
    base = sanitize_username(account) if account else gen_username_from_rule()

    last_err = ""
    for attempt in range(1, 6):
        candidate = base if attempt == 1 else sanitize_username(base + _rand(1))

        # Go to "User Management" page and click "Add Player"
        await goto_user_management(page)
        add_btn = await _find_add_player_button(page)
        if not await add_btn.count():
            raise RuntimeError("Add Player not found")
        await add_btn.click()
        await page.wait_for_timeout(150)
        await _force_confirm_modal(page)

        # Locate fields
        try:
            u = _field_after(page, "User Name"); await u.wait_for(timeout=8000)
        except:
            u = page.locator(".el-card input[type='text'], form input[type='text']").first; await u.wait_for(timeout=8000)
        try:
            p1 = _field_after(page, "Password")
        except:
            p1 = page.locator("input[type='password']").first
        r = page.locator(".el-card textarea, textarea").first

        # Fill fields
        await u.fill(""); await u.type(candidate)
        await p1.fill(""); await p1.type(password)
        pw_all = page.locator("input[type='password']")
        if await pw_all.count() >= 2:
            await pw_all.nth(1).fill(""); await pw_all.nth(1).type(password)

        # Set initial points (required)
        await _set_score_value(page, need_score)

        if await r.count():
            try: await r.fill(""); await r.type(DEFAULT_REMARK)
            except: pass

        # Submit (form-scoped OK)
        await _click_ok_in_add_form(page)

        # Wait for either success or actionable error
        success = False
        for _ in range(60):
            if await page.locator(".el-message--success, .ant-message-success, .el-message:has(.el-message__icon--success)").count():
                success = True
                break
            msg = await _any_error_text(page)
            if msg:
                if _non_blocking_banner(msg):
                    pass  # ignore banner; keep waiting
                else:
                    low = msg.lower()
                    if any(k in low for k in ("exist","already","used","duplicate")):
                        last_err = msg
                        success = False
                        break  # try a new username
                    if ("enter points" in low) or ("enter poitns" in low):
                        await _set_score_value(page, need_score)
                        await _click_ok_in_add_form(page)
                        continue
                    last_err = msg
                    break
            await asyncio.sleep(0.2)

        if success:
            details = await search_user_and_details(page, candidate)
            return {"ok": True, "created": candidate, "attempts": attempt, "detail": details or {}}

        # Retry only on duplicate; otherwise fail with last error
        if last_err and not any(k in last_err.lower() for k in ("exist","already","used","duplicate")):
            raise RuntimeError(last_err or "Create user failed")

    # Final fallback: verify the base name once more
    details = await search_user_and_details(page, base)
    if details:
        return {"ok": True, "created": base, "attempts": "verified-after-loop", "detail": details}
    raise RuntimeError("Create user: no success signal" + (f" ({last_err})" if last_err else ""))

# ---------------- Table finding & scrolling ----------------
async def find_player_row(page: Page, account: str) -> Tuple[Optional[Locator], Optional[Locator]]:
    # After submitting search, scroll page to bottom to ensure the bottom card renders.
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(250)

    # Wait until any table rows exist OR "No Data" appears.
    try:
        await page.wait_for_function(
            """(acct) => {
                const rows = Array.from(document.querySelectorAll('table tbody tr'));
                return rows.some(r => (r.innerText||'').toLowerCase().includes(acct.toLowerCase())) ||
                       document.body.innerText.includes('No Data');
            }""",
            arg=account,
            timeout=12000
        )
    except:
        pass

    # Only consider real body tables (ignore header-only tables).
    tables = page.locator("table:has(tbody)")
    tcount = await tables.count()
    for ix in range(tcount):
        tbl = tables.nth(ix)
        row = tbl.locator("tbody tr").filter(has_text=re.compile(re.escape(account), re.I)).first
        if await row.count():
            return tbl, row
    return None, None

async def _scroll_to_far_right(page: Page, element: Locator):
    h = await element.element_handle()
    if not h: return
    await page.evaluate("""
(node) => {
  function scrollableAncestor(el){
    while (el && el !== document.body){
      const style = getComputedStyle(el);
      const overflowX = style.overflowX;
      if ((overflowX === 'auto' || overflowX === 'scroll') && el.scrollWidth > el.clientWidth) return el;
      el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }
  const sc = scrollableAncestor(node);
  sc.scrollLeft = sc.scrollWidth;
}
""", h)
    await page.wait_for_timeout(150)

# ---------------- Search → open Set Score (player) ----------------
async def goto_search_and_open_set_score(page: Page, account: str):
    await goto_search_user(page)

    # Ensure "Player account" radio
    try:
        await page.get_by_role("radio", name=re.compile(r"Player account", re.I)).check()
    except:
        lab = page.locator("label:has-text('Player account'), span:has-text('Player account')").first
        if await lab.count(): await lab.click()

    # Search field + OK (the OK in the search card)
    q = None
    for sel in (
        "input[placeholder*='account to be search' i]",
        "input[placeholder*='account' i]",
        "input[placeholder*='search' i]",
        "input[placeholder*='Player account' i]",
    ):
        cand = page.locator(sel).first
        if await cand.count():
            q = cand; break
    if not q: raise RuntimeError("Search input not found")

    await q.fill("")
    await q.type(account)
    await page.get_by_role("button", name=re.compile(r"^OK$", re.I)).first.click()
    await page.wait_for_timeout(350)

    table, row = await find_player_row(page, account)
    if not row:
        await page.get_by_role("button", name=re.compile(r"^OK$", re.I)).first.click()
        await page.wait_for_timeout(500)
        table, row = await find_player_row(page, account)
        if not row:
            raise RuntimeError(f"Player row for {account} not found")

    # Scroll horizontally to reveal Action column
    await _scroll_to_far_right(page, table)

    # Click Set Score within this row
    btn = row.locator("button:has-text('Set Score'), a:has-text('Set Score')").first
    await btn.scroll_into_view_if_needed()
    await btn.click()

    # Wait for Set Score block that names our account
    set_block = page.locator(".el-card, section").filter(
        has_text=re.compile(rf"Set\s*Score.*Player\s*account\s*{re.escape(account)}", re.I)
    ).first
    await set_block.wait_for(timeout=15000)
    await set_block.locator("input[placeholder*='ie 100' i], input[type='number']").first.wait_for(timeout=10000)
    return set_block

# ---------------- helpers for Set Score form ----------------
async def _read_current_point_in(card) -> Optional[int]:
    try:
        row = card.locator(":scope").filter(has_text=re.compile(r"Current\s*Point", re.I)).first
        if not await row.count(): return None
        txt = (await row.inner_text() or "").strip()
        m = re.search(r"Current\s*Point\s*([-\d.,]+)", txt, re.I)
        if not m:
            chip = card.locator("text=Current Point").locator("xpath=following::*[1]")
            if await chip.count():
                raw = (await chip.inner_text() or "").strip()
                m = re.search(r"([-\d.,]+)", raw)
        return int(float(m.group(1).replace(",", ""))) if m else None
    except:
        return None

async def _set_amount_js_and_blur(page: Page, input_loc: Locator, value: str):
    """Set the amount EXACTLY like the app does (value + input/change), then blur."""
    h = await input_loc.element_handle()
    if not h:
        raise RuntimeError("Amount input not found")
    await page.evaluate(
        """([el, val]) => {
            const desc = Object.getOwnPropertyDescriptor(el.__proto__, 'value')
                      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            desc.set.call(el, String(val));
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        [h, value]
    )
    try:
        await input_loc.press("Tab")
    except:
        try:
            await input_loc.evaluate("el => el.blur()")
        except:
            await page.mouse.click(5, 5)

async def _submit_set_score_in_block(page: Page, card, account: str, amount: float, remark: str = "") -> dict:
    prev = await _read_current_point_in(card)

    # Fill amount via JS + blur (prevents “Please enter poitns , ie 100”)
    amt = card.locator("input[placeholder*='ie 100' i], input[type='number']").first
    await amt.wait_for(timeout=10000)
    await amt.scroll_into_view_if_needed()
    amt_value = str(int(amount) if float(amount).is_integer() else float(amount))
    await _set_amount_js_and_blur(page, amt, amt_value)

    # Optional remark
    if remark:
        rem = card.locator("textarea").first
        if await rem.count():
            try:
                await rem.scroll_into_view_if_needed()
                await rem.fill("")
                await rem.type(remark)
            except: pass

    # Click OK inside the form/card
    form = amt.locator("xpath=ancestor::form[1]"); 
    if not await form.count(): form = card
    ok = form.locator("button:has-text('OK'), .ant-btn-primary:has-text('OK')").last
    clicked = False
    if await ok.count():
        try:
            await ok.scroll_into_view_if_needed()
            await ok.click(timeout=1800); clicked = True
        except:
            try:
                await ok.click(timeout=1800, force=True); clicked = True
            except:
                try:
                    h = await ok.element_handle()
                    if h: await page.evaluate("(el)=>el.click()", h); clicked = True
                except: pass
    if not clicked:
        # lowest OK on page as ultimate fallback
        await page.evaluate("""
(() => {
  const btns = Array.from(document.querySelectorAll('button,.el-button,.ant-btn'))
    .filter(b => /^(OK|Ok|确定)$/.test((b.innerText||'').trim()))
    .filter(b => { const s=getComputedStyle(b), r=b.getBoundingClientRect(); return s.visibility!=='hidden' && s.display!=='none' && r.width && r.height; })
    .sort((a,b)=>a.getBoundingClientRect().bottom - b.getBoundingClientRect().bottom);
  if (btns.length) btns[btns.length-1].click();
})();
""")

    # Confirm modals
    for _ in range(4):
        dlg_ok = page.locator(
            ".el-message-box__btns button:has-text('OK'), "
            ".el-message-box__btns button:has-text('Ok'), "
            ".el-message-box__btns button:has-text('Confirm'), "
            ".el-message-box__btns button:has-text('确定'), "
            ".ant-modal .ant-btn-primary"
        ).first
        if await dlg_ok.count():
            try: await dlg_ok.click()
            except: pass
            await page.wait_for_timeout(250)
        else:
            break

    # Wait for success signals
    def success_payload(note: str, now: Optional[int]):
        return {"ok": True, "account": account, "amount": float(amount), "current_point": now, "note": note}

    for _ in range(60):
        if await page.locator(".el-message--success, .ant-message-success, .el-message:has(.el-message__icon--success)").count():
            return success_payload("toast", await _read_current_point_in(card))
        if not await card.is_visible():
            return success_payload("card closed", await _read_current_point_in(card))
        now = await _read_current_point_in(card)
        if prev is not None and now is not None and now != prev:
            return success_payload("point changed", now)
        # success wording in modal
        if await page.locator(".el-message-box__message, .ant-modal-body").filter(
            has_text=re.compile(r"(success|completed|done|已|成功)", re.I)
        ).count():
            return success_payload("modal says success", await _read_current_point_in(card))
        # inline form error
        ferr = card.locator(".el-form-item__error, .ant-form-item-explain-error").first
        if await ferr.count():
            msg = (await ferr.text_content() or "").strip()
            raise RuntimeError(msg or "Set Score failed")
        # rate limit soft wait
        low = (await _any_error_text(page)).lower()
        if "frequency of requests is too high" in low or "error: 167" in low:
            await page.wait_for_timeout(1200)
        await asyncio.sleep(0.2)

    # bubble any visible error
    gerr = page.locator(".el-message--error, .ant-message-error").first
    if await gerr.count():
        msg = (await gerr.text_content() or "").strip()
        raise RuntimeError(msg or "Set Score failed")

    raise RuntimeError("No success confirmation from Set Score")

# ---------------- Public APIs ----------------
async def recharge(page: Page, account: str, amount: float, remark: str = "") -> dict:
    card = await goto_search_and_open_set_score(page, account)
    return await _submit_set_score_in_block(page, card, account, abs(float(amount)), remark or "recharge")

async def redeem(page: Page, account: str, amount: float, remark: str = "") -> dict:
    card = await goto_search_and_open_set_score(page, account)
    return await _submit_set_score_in_block(page, card, account, -abs(float(amount)), remark or "redeem")

# ---------------- CLI ----------------
async def main():
    if not (ADMIN_USER and ADMIN_PASS and BASE):
        print("Missing VBLINK_BASE_URL / VBLINK_USERNAME / VBLINK_PASSWORD in .env")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python automation/vblink_bot.py login")
        print("  python automation/vblink_bot.py create <account?> <password?>")
        print("  python automation/vblink_bot.py create-auto --count N [--password Ab123456]")
        print("  python automation/vblink_bot.py recharge <account> <amount> [remark?]")
        print("  python automation/vblink_bot.py redeem   <account> <amount> [remark?]")
        print("  python automation/vblink_bot.py test")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    bundle = await launch_browser()
    _, _, _, page = bundle

    try:
        lr = await login(page)
        print("[login]", json.dumps(lr, indent=2))
        if not lr["ok"]: sys.exit(1)

        if cmd == "login":
            return

        if cmd == "create":
            acct = sanitize_username(sys.argv[2]) if len(sys.argv) >= 3 else gen_username_from_rule()
            pwd  = sys.argv[3] if len(sys.argv) >= 4 else DEFAULT_PWD
            res  = await create_user(page, acct, pwd)
            details = await search_user_and_details(page, res.get("created", acct))
            if details: res["detail"] = details
            print("[create]", json.dumps(res, indent=2))
            if not res.get("ok"): sys.exit(1)

        elif cmd == "create-auto":
            if "--count" not in sys.argv:
                print("create-auto requires --count N"); sys.exit(1)
            count = int(sys.argv[sys.argv.index("--count")+1])
            pwd = DEFAULT_PWD
            if "--password" in sys.argv:
                pwd = sys.argv[sys.argv.index("--password")+1]
            for i in range(count):
                acct = gen_username_from_rule()
                res = await create_user(page, acct, pwd)
                details = await search_user_and_details(page, res.get("created", acct))
                if details: res["detail"] = details
                print(f"[create-auto {i+1}/{count}]", json.dumps(res, indent=2))

        elif cmd == "recharge":
            if len(sys.argv) < 4:
                print("recharge requires: <account> <amount> [remark?]"); sys.exit(1)
            acct = sys.argv[2]; amt = float(sys.argv[3]); rem = sys.argv[4] if len(sys.argv)>=5 else ""
            out = await recharge(page, acct, amt, rem)
            print("[recharge]", json.dumps(out, indent=2))

        elif cmd == "redeem":
            if len(sys.argv) < 4:
                print("redeem requires: <account> <amount> [remark?]"); sys.exit(1)
            acct = sys.argv[2]; amt = float(sys.argv[3]); rem = sys.argv[4] if len(sys.argv)>=5 else ""
            out = await redeem(page, acct, amt, rem)
            print("[redeem]", json.dumps(out, indent=2))

        elif cmd == "test":
            acct = gen_username_from_rule()
            created = await create_user(page, acct, DEFAULT_PWD)
            details = await search_user_and_details(page, created.get("created", acct))
            if details: created["detail"] = details
            print("[create]", json.dumps(created, indent=2))
            print("[recharge]", json.dumps(await recharge(page, created["created"], 5, "bot test"), indent=2))
            print("[redeem]",   json.dumps(await redeem(page, created["created"], 3, "bot test"), indent=2))

        else:
            print(f"Unknown command: {cmd}"); sys.exit(1)

    finally:
        await close_browser(bundle)

if __name__ == "__main__":
    asyncio.run(main())