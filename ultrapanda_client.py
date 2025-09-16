# ultrapanda_client.py
import os, time, hashlib
from urllib.parse import urljoin, unquote, quote_plus
import requests

# ---------- Base & Paths ----------
BASE = os.getenv("ULTRAPANDA_BASE_URL", "https://ht.ultrapanda.mobi").rstrip("/")
PATH_LOGIN = os.getenv("ULTRAPANDA_LOGIN_PATH", "/api/user/login")
PATH_SAVE  = os.getenv("ULTRAPANDA_SAVE_PLAYER_PATH", "/api/account/savePlayer")
PATH_SCORE = os.getenv("ULTRAPANDA_ENTER_SCORE_PATH", "/api/account/enterScore")

# ---------- Headers / UA ----------
UA = os.getenv("GAMEVAULT_UA", "Mozilla/5.0")
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": BASE,
    "Referer": BASE + "/",
}

X_FINGERPRINT = os.getenv("ULTRAPANDA_X_FINGERPRINT", "")  # optional

# ---------- Login payload (encrypted strings you captured) ----------
U_ENC = os.getenv("ULTRAPANDA_LOGIN_USERNAME_ENC", "").strip()
P_ENC = os.getenv("ULTRAPANDA_LOGIN_PASSWORD_ENC", "").strip()
LOGIN_SIGN  = os.getenv("ULTRAPANDA_LOGIN_SIGN", "").strip()      # only if replay works
LOGIN_STIME = os.getenv("ULTRAPANDA_LOGIN_STIME", "").strip()     # only if replay works

# ---------- Sign strategy (update once you find the exact JS formula) ----------
SIGN_MODE  = os.getenv("ULTRAPANDA_SIGN_MODE", "body_params_stime_salt").strip()
SIGN_SALT  = os.getenv("ULTRAPANDA_SIGN_SALT", "").strip()
SIGN_INCLUDE_XTOKEN = os.getenv("ULTRAPANDA_SIGN_INCLUDE_XTOKEN", "false").lower() == "true"

# ---------- Score type flags ----------
ADD    = os.getenv("ULTRAPANDA_SCORE_TYPE_ADD", "1")
DEDUCT = os.getenv("ULTRAPANDA_SCORE_TYPE_DEDUCT", "2")

# ---------- Options ----------
INCLUDE_BODY_TOKEN = os.getenv("ULTRAPANDA_SAVE_INCLUDE_BODY_TOKEN", "true").lower() == "true"

# NOTE: we DELIBERATELY ignore ULTRAPANDA_TOKEN_URLENC for live calls,
# to avoid ever sending a placeholder string. Always use the fresh login token.
# If you *really* need to force a token from env (not recommended), set:
#   ULTRAPANDA_USE_ENV_TOKEN=true
USE_ENV_TOKEN = os.getenv("ULTRAPANDA_USE_ENV_TOKEN", "false").lower() == "true"
ENV_TOKEN_ENC = os.getenv("ULTRAPANDA_TOKEN_URLENC", "").strip()

_session = None
_token_raw = None  # decoded token from login
_token_enc = None  # urlencoded token for headers/body

def _url(p: str) -> str:
    return urljoin(BASE + "/", p.lstrip("/"))

def _ensure_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(DEFAULT_HEADERS)
    return _session

def login():
    """
    POST /api/user/login using the encrypted username/password you captured.
    Returns token (URL-encoded) in JSON.
    """
    sess = _ensure_session()
    stime = int(LOGIN_STIME) if LOGIN_STIME.isdigit() else int(time.time())
    payload = {
        "username": U_ENC,
        "password": P_ENC,
        "stime":   stime,
        "sign":    LOGIN_SIGN,   # if the panel requires fresh sign, set LOGIN_SIGN/LOGIN_STIME properly
        "auth_code": "",
    }
    r = sess.post(_url(PATH_LOGIN), json=payload, timeout=30)
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "_status": r.status_code, "_text": r.text[:300]}
    if r.status_code == 200 and "token" in j:
        return {"ok": True, "token": j["token"], "raw": j}
    return {"ok": False, "_status": r.status_code, "json": j}

def _refresh_token_from_login():
    global _token_raw, _token_enc
    L = login()
    if not L.get("ok"):
        raise RuntimeError(f"Login failed: {L}")
    _token_enc = L["token"]                   # as returned (URL-encoded)
    _token_raw = unquote(_token_enc)          # decoded for hashing if needed
    return _token_raw, _token_enc

def get_token_raw():
    """
    Always prefer live login. Only use env token if explicitly forced.
    """
    global _token_raw, _token_enc
    if USE_ENV_TOKEN and ENV_TOKEN_ENC and not ENV_TOKEN_ENC.startswith("<"):
        _token_enc = ENV_TOKEN_ENC
        _token_raw = unquote(ENV_TOKEN_ENC)
        return _token_raw
    if _token_raw:
        return _token_raw
    _refresh_token_from_login()
    return _token_raw

def get_token_enc():
    """
    URL-encoded token (for headers/body). Always live unless forced by env.
    """
    global _token_raw, _token_enc
    if USE_ENV_TOKEN and ENV_TOKEN_ENC and not ENV_TOKEN_ENC.startswith("<"):
        _token_enc = ENV_TOKEN_ENC
        _token_raw = unquote(ENV_TOKEN_ENC)
        return _token_enc
    if _token_enc:
        return _token_enc
    _refresh_token_from_login()
    return _token_enc

# ---------- Sign helpers ----------
def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def _build_sign_for_save(body: dict, x_token_raw: str, stime_sec: int) -> str:
    """
    Update this to exactly match the site JS once you find it.
    We support a few common patterns selected by ULTRAPANDA_SIGN_MODE.
    """
    if not SIGN_SALT:
        # Without SALT the server will reject with code:-1
        return "nosalt"

    if SIGN_MODE == "body_params_stime_salt":
        # Example: md5("account=...&pwd=...&score=...&stime=..."+SALT)
        seq = f"account={body.get('account','')}&pwd={body.get('pwd','')}&score={body.get('score','0')}&stime={stime_sec}{SIGN_SALT}"
        return _md5_hex(seq)

    if SIGN_MODE == "xtoken_stime_salt":
        # Example: md5(x_token_raw + stime + SALT)
        return _md5_hex(f"{x_token_raw}{stime_sec}{SIGN_SALT}")

    if SIGN_MODE == "salt_stime_xtoken":
        # Example: md5(SALT + stime + x_token_raw)
        return _md5_hex(f"{SIGN_SALT}{stime_sec}{x_token_raw}")

    # Fallback: just salt+stime (unlikely)
    return _md5_hex(f"{SIGN_SALT}{stime_sec}")

def _common_headers_with_xtoken():
    h = dict(DEFAULT_HEADERS)
    # Per your capture, include these:
    h["x-time"] = str(int(time.time() * 1000))  # ms
    if X_FINGERPRINT:
        h["x-fingerprint"] = X_FINGERPRINT
    # token header must be URL-encoded per your capture
    h["x-token"] = get_token_enc()
    return h

def save_player(account: str, pwd: str, name: str = None, score: int = 0, phone: str = "", remark: str = ""):
    """
    Mirrors the successful browser call you captured:
      - JSON body with account/pwd/... + token + stime (seconds) + sign
      - Headers with x-token (URL-encoded), x-time (ms), x-fingerprint (optional)
    """
    sess = _ensure_session()

    # Build body
    stime_sec = int(time.time())
    token_enc = get_token_enc()  # always live (or forced by env explicitly)
    token_raw = unquote(token_enc)

    body = {
        "account": account,
        "pwd": pwd,
        "name": name or account,
        "tel_area_code": "",
        "phone": phone or "",
        "remark": remark or "",
        "score": str(score),
    }
    if INCLUDE_BODY_TOKEN:
        body["token"] = token_enc  # keep encoded in body to match capture
    body["stime"] = stime_sec
    body["sign"]  = _build_sign_for_save(body, token_raw, stime_sec)

    # Headers
    headers = _common_headers_with_xtoken()

    # Send
    r = sess.post(_url(PATH_SAVE), json=body, headers=headers, timeout=30)
    try:
        j = r.json()
    except Exception:
        j = {"_status": r.status_code, "_text": r.text[:500]}

    # redact sign in echo
    body_echo = dict(body)
    if "sign" in body_echo:
        body_echo["sign"] = "***redacted***"

    ok = isinstance(j, dict) and j.get("code") == 20000
    return {"ok": ok, "status": r.status_code, "json": j, "sent": body_echo}

def enter_score(account: str, amount: int, mode: str = "add"):
    """
    Same header pattern; body likely needs token/stime/sign too.
    Update _build_sign_for_save or add a dedicated builder if enterScore differs.
    """
    sess = _ensure_session()
    stime_sec = int(time.time())
    token_enc = get_token_enc()
    token_raw = unquote(token_enc)

    score_type = ADD if mode.lower().strip() == "add" else DEDUCT

    body = {
        "account": account,
        "score": str(int(amount)),
        "type": str(score_type),
    }
    if INCLUDE_BODY_TOKEN:
        body["token"] = token_enc
    body["stime"] = stime_sec
    body["sign"]  = _build_sign_for_save(body, token_raw, stime_sec)  # adjust if enterScore uses a different formula

    headers = _common_headers_with_xtoken()

    r = sess.post(_url(PATH_SCORE), json=body, headers=headers, timeout=30)
    try:
        j = r.json()
    except Exception:
        j = {"_status": r.status_code, "_text": r.text[:500]}

    ok = isinstance(j, dict) and j.get("code") == 20000
    return {"ok": ok, "status": r.status_code, "json": j, "sent": {**body, "sign": "***redacted***"}}