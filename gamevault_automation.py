# gamevault_automation.py
# FINAL — UI login (2Captcha) → API create / recharge / redeem
# with persistent user map to avoid userList filter issues

from __future__ import annotations

import os, re, time, json, base64, random, string
from typing import Dict, Optional, List, Tuple
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, Response
from rpa.captcha import solve_image_captcha

# =============================================================================
# ENV & Defaults
# =============================================================================
BASE_URL  = os.getenv("GAMEVAULT_WEB_URL", "https://agent.gamevault999.com/").rstrip("/")
API_BASE  = os.getenv("GAMEVAULT_BASE_URL", f"{BASE_URL}/api").rstrip("/")
LOGIN_URL = os.getenv("GV_LOGIN_URL", f"{BASE_URL}/login")

GV_USER = os.getenv("GV_USERNAME", "")
GV_PASS = os.getenv("GV_PASSWORD", "")
CAPTCHA_KEY = (os.getenv("TWO_CAPTCHA_APIKEY") or os.getenv("TWO_CAPTCHA_API_KEY") or "").strip()

TIMEOUT_MS = int(os.getenv("GV_TIMEOUT_MS", os.getenv("GAMEVAULT_TIMEOUT_MS", "60000")))
UA = os.getenv("GV_USER_AGENT", "Mozilla/5.0")

# Persistent map path (repo root)
MAP_PATH = os.path.join(os.getcwd(), "gv_users.json")

def _norm_sel(s: str) -> str:
    return ", ".join([p.strip() for p in (s or "").split(",") if p.strip()])

# IMPORTANT: flexible captcha selectors; no trailing commas
SEL_CAPTCHA_IMG = _norm_sel(os.getenv(
    "GV_CAPTCHA_IMG_SEL",
    "form .el-input-group__append img, form .el-input-group__append canvas, "
    "form img[src*='captcha'], form img[alt*='captcha'], "
    "form img[src*='verify'], form img[alt*='verify'], form canvas"
))
SEL_CAPTCHA_INPUT_FORCE = (os.getenv("GV_CAPTCHA_INPUT_FORCE", "") or "").strip() or None

UN_PREFIX = os.getenv("GV_USERNAME_PREFIX", "user_")
UN_SUFFIX = os.getenv("GV_USERNAME_SUFFIX", "_gv")
DEF_PASS  = os.getenv("GV_DEFAULT_PASSWORD", "Abc12345")

# =============================================================================
# Token cache and headers
# =============================================================================
_TOKEN_CACHE: Optional[str] = None

def _api_headers(token: str, json_mode: bool = True) -> dict:
    h = {
        "User-Agent": UA,
        "Referer": BASE_URL + "/",
        "Origin": BASE_URL,
        # many tenants require BOTH:
        "Authorization": f"Bearer {token}",
        "token": token,
    }
    h["Content-Type"] = "application/json" if json_mode else "application/x-www-form-urlencoded"
    return h

# =============================================================================
# Local user map (username → {user_id, game_id})
# =============================================================================
def _map_load() -> dict:
    try:
        with open(MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _map_save(m: dict) -> None:
    try:
        with open(MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def _map_get(username_or_gid: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (user_id, account_username, game_id) from local map if present."""
    m = _map_load()
    key = (username_or_gid or "").strip()
    # direct key
    row = m.get(key)
    if isinstance(row, dict) and row.get("user_id"):
        return str(row.get("user_id")), key, (row.get("game_id") if row.get("game_id") is not None else None)
    # case-insensitive username
    for k, v in m.items():
        if k.strip().lower() == key.lower() and isinstance(v, dict) and v.get("user_id"):
            return str(v.get("user_id")), k, (v.get("game_id") if v.get("game_id") is not None else None)
    # reverse lookup by game_id
    for k, v in m.items():
        if isinstance(v, dict) and str(v.get("game_id") or "").strip() == key:
            return str(v.get("user_id")), k, (v.get("game_id") if v.get("game_id") is not None else None)
    return None, None, None

def _map_set(username: str, user_id: Optional[str], game_id: Optional[str]) -> None:
    if not username:
        return
    m = _map_load()
    row = m.get(username) or {}
    if user_id: row["user_id"] = str(user_id)
    if game_id is not None: row["game_id"] = str(game_id)
    m[username] = row
    _map_save(m)

# =============================================================================
# Small utils
# =============================================================================
def _sleep(page, ms: int):
    try: page.wait_for_timeout(ms)
    except Exception: time.sleep(ms/1000.0)

def _rand_username(seed: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "", (seed or "user")).lower()[:10] or "u"
    tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=3))
    u = f"{UN_PREFIX}{base}{tag}"
    if not u.endswith(UN_SUFFIX):
        u += UN_SUFFIX
    return u

def _clean(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").replace("\u00A0", " ")).strip().lower()

# =============================================================================
# Captcha helpers
# =============================================================================
def _focus_code_input(page) -> None:
    if SEL_CAPTCHA_INPUT_FORCE:
        try:
            loc = page.locator(SEL_CAPTCHA_INPUT_FORCE).first
            if loc and loc.is_visible(timeout=1200):
                loc.focus()
                return
        except Exception:
            pass
    for sel in [
        "form input[name*='verify']",
        "form input[name*='code']",
        "form input[name*='captcha']",
        "form input[placeholder*='verification']",
        "form input[placeholder*='Verification']",
        "form input[placeholder*='code']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc and loc.is_visible(timeout=800):
                name = (loc.get_attribute("name") or "").lower()
                ph   = (loc.get_attribute("placeholder") or "").lower()
                if any(k in (name+ph) for k in ("user","username","account","login","password")):
                    continue
                loc.focus()
                return
        except Exception:
            continue

def _find_captcha_visual(page):
    selector = SEL_CAPTCHA_IMG
    deadline = time.time() + 8
    last = None
    while time.time() < deadline:
        try:
            loc = page.locator(selector).first
            if loc and loc.is_visible(timeout=700):
                return loc
            last = loc
        except Exception:
            pass
        _sleep(page, 150)
    return last

def _nearest_captcha_input(page, visual):
    if SEL_CAPTCHA_INPUT_FORCE:
        try:
            forced = page.locator(SEL_CAPTCHA_INPUT_FORCE).first
            if forced and forced.is_visible(timeout=700):
                forced.focus()
                return forced
        except Exception:
            pass
    # Element-UI input-group (common case)
    try:
        group = visual.locator("xpath=ancestor::*[contains(@class,'el-input-group')][1]")
        if group and group.count() > 0:
            inp = group.locator("input[type='text'], input:not([type])").first
            if inp and inp.is_visible(timeout=700):
                inp.focus()
                return inp
    except Exception:
        pass
    # Immediate preceding input
    try:
        near = visual.locator("xpath=preceding::input[1]")
        if near and near.is_visible(timeout=700):
            name = (near.get_attribute("name") or "").lower()
            ph   = (near.get_attribute("placeholder") or "").lower()
            if not any(k in (name+ph) for k in ("user","username","account","login","password")):
                near.focus()
                return near
    except Exception:
        pass
    # Fallback scan in form
    try:
        form = visual.locator("xpath=ancestor::form[1]")
    except Exception:
        form = page
    inputs = form.locator("input[type='text'], input:not([type])")
    try:
        cnt = min(15, inputs.count())
    except Exception:
        cnt = 0
    for i in range(cnt):
        inp = inputs.nth(i)
        try:
            if not inp.is_visible(timeout=350):
                continue
            name = (inp.get_attribute("name") or "").lower()
            ph   = (inp.get_attribute("placeholder") or "").lower()
            if any(k in (name+ph) for k in ("user","username","account","login","password")):
                continue
            inp.focus()
            return inp
        except Exception:
            continue
    return None

def _get_captcha_bytes(page, ctx, vis):
    # Try canvas
    try:
        tag = (vis.evaluate("el => el.tagName") or "").lower()
    except Exception:
        tag = ""
    if tag == "canvas":
        try:
            data_url = vis.evaluate("el => el.toDataURL('image/png')")
            if isinstance(data_url, str) and data_url.startswith("data:image"):
                return base64.b64decode(data_url.split(",",1)[1])
        except Exception:
            pass
    # Try src / background url
    src = ""
    try: src = vis.get_attribute("src") or ""
    except Exception: pass
    if not src:
        try:
            style = vis.get_attribute("style") or ""
            m = re.search(r"url\((['\"]?)(.+?)\1\)", style, re.I)
            if m: src = m.group(2)
        except Exception:
            pass
    if src.startswith("data:image/"):
        return base64.b64decode(src.split(",",1)[1])
    if src:
        try:
            abs_url = src if re.match(r"^https?://", src, re.I) else urljoin(page.url, src)
            rsp = ctx.request.get(abs_url, timeout=TIMEOUT_MS/1000.0)
            if rsp.ok:
                return rsp.body()
        except Exception:
            pass
    # Fallback screenshot
    try:
        return vis.screenshot()
    except Exception:
        return None

# =============================================================================
# Token extraction (response headers + storage) and login
# =============================================================================
def _extract_token_from_storages(page) -> str:
    dump_js = """
    () => {
      const out = {local:{}, session:{}};
      try { for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i); out.local[k]=localStorage.getItem(k);} } catch(e){}
      try { for (let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i); out.session[k]=sessionStorage.getItem(k);} } catch(e){}
      return out;
    }
    """
    store = page.evaluate(dump_js) or {"local":{}, "session":{}}
    candidates: List[str] = []

    def collect(v: str):
        if not v: return
        candidates.append(v)
        try:
            obj = json.loads(v)
            if isinstance(obj, dict):
                for key2 in ("token","Token","access_token","authorization","Authorization"):
                    if obj.get(key2):
                        candidates.append(str(obj.get(key2)))
        except Exception:
            pass

    for scope in ("local","session"):
        for _, v in (store.get(scope) or {}).items():
            collect(v)

    # Prefer JWT-like strings or long token-ish values
    for c in candidates:
        if "." in c and len(c) > 20:
            return c.strip()
    for c in candidates:
        if len(c) >= 20 and re.search(r"[A-Za-z0-9\-_]{16,}", c):
            return c.strip()

    # Direct keys
    for key in ("token","Token","authorization","Authorization","tokenInfo"):
        if (store.get("local") or {}).get(key):   return (store["local"][key] or "").strip()
        if (store.get("session") or {}).get(key): return (store["session"][key] or "").strip()
    return ""

def _login_get_token(force_refresh: bool = False) -> str:
    global _TOKEN_CACHE
    if _TOKEN_CACHE and not force_refresh:
        return _TOKEN_CACHE

    if not GV_USER or not GV_PASS:
        raise RuntimeError("GV_USERNAME / GV_PASSWORD not set")
    if not CAPTCHA_KEY:
        raise RuntimeError("TWO_CAPTCHA_APIKEY / TWO_CAPTCHA_API_KEY not set")

    headless = (os.getenv("GV_HEADLESS","false").lower() == "true")
    slowmo   = int(os.getenv("GV_SLOWMO_MS","0") or "0")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=slowmo, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width":1366,"height":900}, user_agent=UA)
        page = ctx.new_page()

        # Capture tokens from /agentLogin responses if present
        tokens_from_net: List[str] = []
        def on_response(r: Response):
            try:
                if "/agent/agentLogin" in r.url:
                    for hk in ("token","authorization","Authorization"):
                        v = r.headers.get(hk)
                        if v: tokens_from_net.append(v.strip().replace("Bearer ", "").replace("bearer ", ""))
                    # Sometimes token is in JSON body
                    try:
                        data = r.json()
                        if isinstance(data, dict):
                            for k in ("token","Token","access_token","authorization","Authorization"):
                                 v = (data.get("data") or {}).get(k) or data.get(k)
                                 if v: tokens_from_net.append(str(v))
                    except Exception:
                        pass
            except Exception:
                pass
        page.on("response", on_response)

        page.goto(LOGIN_URL, wait_until="load", timeout=TIMEOUT_MS)

        try:
            form = page.locator("form").first
        except Exception:
            form = page

        # Fill username & password
        try:
            form.locator("input[type='text'], input:not([type])").first.fill(GV_USER)
        except Exception:
            page.locator("input").nth(0).fill(GV_USER)
        form.locator("input[type='password']").first.fill(GV_PASS)

        # Make sure captcha is visible, then fetch its image bytes
        _focus_code_input(page)
        visual = _find_captcha_visual(page)
        if not visual:
            raise RuntimeError("Captcha visual not found on login page")

        png = _get_captcha_bytes(page, ctx, visual)
        if not png:
            raise RuntimeError("Could not fetch captcha image bytes")

        code = solve_image_captcha(CAPTCHA_KEY, png)

        # Fill the code into the input nearest to the captcha
        target = _nearest_captcha_input(page, visual)
        if target:
            try: target.fill("")
            except Exception: pass
            target.type(code, delay=25)
        else:
            # Last resort: click image and type (focus follows)
            try: visual.click(timeout=600)
            except Exception: pass
            page.keyboard.type(code, delay=25)

        # Submit (button or Enter)
        submitted = False
        for sel in ("form button[type='submit']", "button[type='submit']"):
            try:
                form.locator(sel).first.click(timeout=1500)
                submitted = True
                break
            except Exception:
                pass
        if not submitted:
            for pat in (r"^\s*login\s*$", r"^\s*log\s*in\s*$", r"^\s*sign\s*in\s*$", r"^\s*submit\s*$"):
                try:
                    page.get_by_role("button", name=re.compile(pat, re.I)).click(timeout=1500)
                    submitted = True
                    break
                except Exception:
                    pass
        if not submitted:
            try:
                form.locator("input[type='password']").first.press("Enter")
                submitted = True
            except Exception:
                pass
        if not submitted:
            raise RuntimeError("Could not find login submit button")

        # Wait for token to appear from network or storages
        for _ in range(80):
            if tokens_from_net:
                _TOKEN_CACHE = tokens_from_net[-1].replace("Bearer ","").strip()
                break
            tok = _extract_token_from_storages(page)
            if tok:
                _TOKEN_CACHE = tok.replace("Bearer ","").strip()
                break
            # also wait until leaving /login (some tenants redirect fast)
            if "/login" not in (page.url or "").lower():
                tok2 = _extract_token_from_storages(page)
                if tok2:
                    _TOKEN_CACHE = tok2.replace("Bearer ","").strip()
                    break
            _sleep(page, 250)

        ctx.close()
        browser.close()

    if not _TOKEN_CACHE:
        raise RuntimeError("Login failed: token not found")
    return _TOKEN_CACHE

# =============================================================================
# API helpers
# =============================================================================
def _post_api(path: str, token: str, payload: dict) -> dict:
    url = f"{API_BASE}{path if path.startswith('/') else '/'+path}"

    # Try JSON first
    r = requests.post(url, headers=_api_headers(token, json_mode=True), json=payload, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"code": r.status_code, "msg": r.text}

    # If protocol/param errors, retry as form-urlencoded (seen on some tenants)
    msg = (str(data.get("msg") or "")).lower()
    if data.get("code") not in (200, "200") and ("protocol" in msg or "param" in msg):
        r2 = requests.post(url, headers=_api_headers(token, json_mode=False), data=payload, timeout=30)
        try:
            data2 = r2.json()
        except Exception:
            data2 = {"code": r2.status_code, "msg": r2.text}
        return data2

    return data

def _rows_from(data: dict) -> list:
    """Normalize various {data/list} shapes into a list of rows."""
    d = data or {}
    if isinstance(d.get("data"), dict):
        dd = d["data"]
        if isinstance(dd.get("list"), list): return dd["list"]
        if isinstance(dd, list):             return dd
    if isinstance(d.get("data"), list):      return d["data"]
    if isinstance(d.get("list"), list):      return d["list"]
    for v in d.values():
        if isinstance(v, list):              return v
        if isinstance(v, dict) and isinstance(v.get("list"), list):
            return v["list"]
    return []

def _ensure_user_api(token: str, key: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (user_id, account_username, game_id) by searching multiple payload variants."""
    key_clean = _clean(key)
    if not key_clean:
        return None, None, None

    # Primary tries (username / game_id filters + common variants)
    queries = [
        {"account": key, "page": 1, "limit": 20, "locale": "en", "timezone": "cst"},
        {"account": key_clean, "page": 1, "limit": 20, "locale": "en", "timezone": "cst"},
        {"searchType": "account", "searchContent": key, "page": 1, "limit": 20, "locale": "en", "timezone": "cst"},
        {"searchType": 1, "searchContent": key, "page": 1, "limit": 20, "locale": "en", "timezone": "cst"},
        {"accountName": key, "pageIndex": 1, "pageSize": 20, "locale": "en", "timezone": "cst"},
        {"game_id": key, "page": 1, "limit": 20, "locale": "en", "timezone": "cst"},
        {"gameId": key, "pageIndex": 1, "pageSize": 20, "locale": "en", "timezone": "cst"},
    ]
    for q in queries:
        data = _post_api("/user/userList", token, q)
        for row in _rows_from(data):
            acc   = _clean(row.get("account") or row.get("user_name") or "")
            gid   = _clean(str(row.get("game_id") or row.get("gameid") or row.get("gameId") or ""))
            uid   = str(row.get("user_id") or row.get("id") or "").strip()
            if acc == key_clean or (gid and gid == key_clean):
                return uid, (row.get("account") or row.get("user_name") or ""), (row.get("game_id") or row.get("gameid") or row.get("gameId") or "")

    # Wide scan (paged)
    for page_key in ("page", "pageIndex"):
        for size_key in ("limit", "pageSize", "pagesize"):
            payload = {"locale": "en", "timezone": "cst"}
            payload[page_key] = 1
            payload[size_key] = 100
            for page in range(1, 101):
                payload[page_key] = page
                data = _post_api("/user/userList", token, payload)
                rows = _rows_from(data)
                if not rows:
                    break
                for row in rows:
                    acc = _clean(row.get("account") or row.get("user_name") or "")
                    gid = _clean(str(row.get("game_id") or row.get("gameid") or row.get("gameId") or ""))
                    uid = str(row.get("user_id") or row.get("id") or "").strip()
                    if acc == key_clean or (gid and gid == key_clean):
                        return uid, (row.get("account") or row.get("user_name") or ""), (row.get("game_id") or row.get("gameid") or row.get("gameId") or "")
                if len(rows) < int(payload[size_key]):
                    break
    return None, None, None

def _ensure_user(token: str, username_or_gid: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """1) Try local map, 2) fall back to API lookup, and persist on success."""
    uid, acc, gid = _map_get(username_or_gid)
    if uid:
        return uid, acc, gid
    uid, acc, gid = _ensure_user_api(token, username_or_gid)
    if uid:
        _map_set(acc, uid, gid)
    return uid, acc, gid

# =============================================================================
# Public API
# =============================================================================
def gv_create_account(display_name: str, email: str = "") -> Dict:
    """
    Create a player via API using a token obtained through UI login + 2Captcha.
    Returns { ok, username, password, user_id? } or { ok: False, error }.
    Auto-retries once with a randomized username if "login name have used".
    """
    try:
        token = _login_get_token()

        # normalize desired username (respect provided if valid, else random)
        base = re.sub(r"[^A-Za-z0-9_]+", "", display_name or "User")
        username = base if re.match(r"^[A-Za-z0-9_]{3,}$", base or "") else _rand_username(display_name)
        if not username.endswith(UN_SUFFIX):
            username = f"{UN_PREFIX}{username}{UN_SUFFIX}"

        def _attempt_create(uname: str) -> dict:
            payload = {
                "account": uname,
                "nickname": (display_name.split("_",1)[0] or display_name or "User")[:13],
                "rechargeamount": "",
                "login_pwd": DEF_PASS,
                "check_pwd": DEF_PASS,
                "captcha": None,
                "locale": "en",
                "timezone": "cst",
                "t": "",
            }
            return _post_api("/user/addUser", token, payload)

        data = _attempt_create(username)

        # Handle "name used" by retrying once with randomized username
        msg_text = (data.get("msg") or "").lower()
        if data.get("code") == 200 and ((data.get("data") or {}).get("retcode") in (0, "0")):
            user_id = str((data["data"].get("user_id") or "")).strip()
            if user_id:
                _map_set(username, user_id, None)
            return {"ok": True, "username": username, "password": DEF_PASS, "user_id": user_id or None}
        elif "login name have used" in msg_text or "already exists" in msg_text:
            username2 = _rand_username(display_name)
            data2 = _attempt_create(username2)
            if data2.get("code") == 200:
                dd = data2.get("data") or {}
                user_id = str(dd.get("user_id") or "").strip()
                if user_id:
                    _map_set(username2, user_id, None)
                return {"ok": True, "username": username2, "password": DEF_PASS, "user_id": user_id or None}

        # Other success shapes
        if data.get("code") == 200:
            dd = data.get("data") or {}
            if isinstance(dd, dict):
                user_id = str(dd.get("user_id") or dd.get("id") or "").strip()
                if user_id:
                    _map_set(username, user_id, None)
                    return {"ok": True, "username": username, "password": DEF_PASS, "user_id": user_id}
                if dd.get("retcode") in (0, "0"):
                    _map_set(username, None, None)
                    return {"ok": True, "username": username, "password": DEF_PASS, "user_id": None}

        return {"ok": False, "error": data.get("msg") or (data.get("data") or {}).get("error_en") or "Create failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def gv_credit(username_or_gameid: str, amount: float, note: str = "") -> Dict:
    """
    Credit (recharge) a player's balance using the same UI-login token flow.
    Accepts username or game_id. Uses local map first, then API lookup.
    """
    try:
        token = _login_get_token()
        uid, acc, gid = _ensure_user(token, username_or_gameid)
        if not uid:
            return {"ok": False, "error": f"user not found: {username_or_gameid}"}

        payload = {
            "user_id": str(uid),
            "type": 1,  # recharge
            "account": acc,
            "balance": 0,
            "amount": str(int(float(amount))),
            "remark": note or "",
            "locale": "en",
            "timezone": "cst",
        }

        data = _post_api("/user/rechargeRedeem", token, payload)
        if data.get("code") == 200:
            bal = (data.get("data") or {}).get("Balance")
            _map_set(acc, uid, gid)  # keep map fresh
            return {"ok": True, "balance": bal, "account": acc, "game_id": gid}
        return {"ok": False, "error": data.get("msg") or "recharge failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def gv_redeem(username_or_gameid: str, amount: float, note: str = "") -> Dict:
    """
    Redeem (withdraw) a player's balance using the same UI-login token flow.
    Accepts username or game_id. Uses local map first, then API lookup.
    """
    try:
        token = _login_get_token()
        uid, acc, gid = _ensure_user(token, username_or_gameid)
        if not uid:
            return {"ok": False, "error": f"user not found: {username_or_gameid}"}

        payload = {
            "user_id": str(uid),
            "type": 2,  # redeem
            "account": acc,
            "balance": 0,
            "amount": str(int(float(amount))),
            "remark": note or "",
            "locale": "en",
            "timezone": "cst",
        }

        data = _post_api("/user/rechargeRedeem", token, payload)
        if data.get("code") == 200:
            bal = (data.get("data") or {}).get("Balance")
            _map_set(acc, uid, gid)
            return {"ok": True, "balance": bal, "account": acc, "game_id": gid}
        return {"ok": False, "error": data.get("msg") or "redeem failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# Handy manual mapping helper: call this once if you already know ids.
def gv_map_user(username: str, user_id: str, game_id: Optional[str] = None) -> Dict:
    try:
        _map_set(username, user_id, game_id)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# =============================================================================
# Optional terminal test
# =============================================================================
if __name__ == "__main__":
    print("=== CREATE ===")
    res = gv_create_account("TestUser123", "test@example.com")
    print(json.dumps(res, indent=2))
    if res.get("ok"):
        u = res["username"]
        print("\n=== CREDIT (1) ===")
        print(json.dumps(gv_credit(u, 1, "autotest credit"), indent=2))
        print("\n=== REDEEM (1) ===")
        print(json.dumps(gv_redeem(u, 1, "autotest redeem"), indent=2))