# gamevault_automation.py  — FINAL

import os, json, time, logging, subprocess, sys, re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
load_dotenv(override=True)

log = logging.getLogger("gamevault")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# -------------------- ENV helpers --------------------
def env(*names: str, default: Optional[str] = None) -> str:
    for n in names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    return default or ""

def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except Exception:
        return default

# -------------------- Config --------------------
ENABLED             = env("GAMEVAULT_ENABLED", "GV_ENABLED", default="true").lower() == "true"
BASE_URL            = env("GAMEVAULT_BASE_URL", "GV_BASE_URL", default="").rstrip("/")
CREATE_PATH         = env("GAMEVAULT_CREATE_PATH", "GV_CREATE_PATH", default="/user/addUser")
CREDIT_PATH         = env("GAMEVAULT_CREDIT_PATH", "GV_CREDIT_PATH", default="/user/rechargeRedeem")
REDEEM_PATH         = env("GAMEVAULT_REDEEM_PATH", "GV_REDEEM_PATH", default="/user/rechargeRedeem")
USERLIST_PATH       = env("GAMEVAULT_USERLIST_PATH", "GV_USERLIST_PATH", default="/user/userList")
USERLIST_METHOD     = env("GAMEVAULT_USERLIST_METHOD", "GV_USERLIST_METHOD", default="POST").upper()
PING_PATH           = env("GAMEVAULT_PING_PATH", "GV_PING_PATH", default=USERLIST_PATH)
PING_METHOD         = env("GAMEVAULT_PING_METHOD", "GV_PING_METHOD", default=USERLIST_METHOD).upper()

AUTH_BEARER_DEFAULT = env("GAMEVAULT_AUTH", "GV_AUTH", default="")
SESSION_ID_DEFAULT  = env("GAMEVAULT_SESSION", "GV_SESSION", default="")
COOKIE_ACCOUNT      = env("GAMEVAULT_COOKIE_ACCOUNT", "GV_COOKIE_ACCOUNT", default="")
COOKIE_PASSWORD     = env("GAMEVAULT_COOKIE_PASSWORD", "GV_COOKIE_PASSWORD", default="")
AGENT_ID            = env("GAMEVAULT_AGENT_ID", "GV_AGENT_ID", default="")

TIMEOUT_MS          = env_int("GAMEVAULT_TIMEOUT_MS", 45000)
UA                  = env("GAMEVAULT_UA", "GV_UA", default="CryptoCasino/1.0 (GameVault)")
SESSION_FILE        = env("GAMEVAULT_SESSION_FILE", "GV_SESSION_FILE", default=".gv_session.json")
KEEPALIVE_SECS      = env_int("GAMEVAULT_KEEPALIVE_SECS", 300)
LOGIN_SCRIPT        = env("GAMEVAULT_LOGIN_SCRIPT", "GV_LOGIN_SCRIPT", default="gv_login_capture.py")
TX_JSON             = env("GAMEVAULT_TX_JSON", default="false").lower() == "true"  # most panels need FALSE
DEBUG_AUTH          = env("GAMEVAULT_DEBUG_AUTH", default="false").lower() == "true"

GV_USERNAME_PREFIX  = env("GV_USERNAME_PREFIX", default="")
GV_USERNAME_SUFFIX  = env("GV_USERNAME_SUFFIX", default="")
GV_DEFAULT_PASSWORD = env("GV_DEFAULT_PASSWORD", default="123456")

if not BASE_URL:
    raise RuntimeError("GAMEVAULT_BASE_URL is not set.")

# -------------------- Session file helpers --------------------
def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _normalize_session_dict(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = raw or {}
    auth = (raw.get("auth") or raw.get("bearer") or AUTH_BEARER_DEFAULT or "").strip()
    cookies_raw = raw.get("cookies") or {}
    session_val = cookies_raw.get("session") or raw.get("phpsessid") or SESSION_ID_DEFAULT
    account_val = cookies_raw.get("account") or raw.get("account_cookie") or COOKIE_ACCOUNT
    password_val= cookies_raw.get("password") or raw.get("password_cookie") or COOKIE_PASSWORD
    return {
        "auth": auth,
        "cookies": {"session": session_val or "", "account": account_val or "", "password": password_val or ""},
        "updated_at": int(raw.get("updated_at") or time.time()),
    }

def _load_session_from_file() -> Dict[str, Any]:
    if not os.path.exists(SESSION_FILE):
        data = _normalize_session_dict({})
        try:
            os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        except Exception:
            pass
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        return data
    data = _read_json(SESSION_FILE)
    if data is None:
        return _normalize_session_dict({})
    return _normalize_session_dict(data)

def _save_session_to_file(data: Dict[str, Any]) -> None:
    try:
        out = _normalize_session_dict(data)
        out["updated_at"] = int(time.time())
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except Exception as e:
        log.warning("Could not write session file %s: %s", SESSION_FILE, e)

# -------------------- HTTP core with auto-reauth --------------------
class GVClient:
    def __init__(self):
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, */*;q=0.8",
            "Origin": BASE_URL.split("/api")[0] if "/api" in BASE_URL else BASE_URL,
        })
        self._apply_auth_from_file()

    def _apply_auth_from_file(self):
        info = _load_session_from_file()
        self.bearer = (info.get("auth") or AUTH_BEARER_DEFAULT or "").strip()

        ck = info.get("cookies") or {}
        sess_val = ck.get("session", "") or ck.get("phpsessid", "")
        if sess_val:
            self.sess.cookies.set("PHPSESSID", sess_val)
            self.sess.cookies.set("session", sess_val)
        if ck.get("account"):
            self.sess.cookies.set("account", ck.get("account"))
        if ck.get("password"):
            self.sess.cookies.set("password", ck.get("password"))

        if self.bearer:
            self.sess.headers["Authorization"] = self.bearer
            raw = self.bearer.split(" ", 1)[1] if self.bearer.lower().startswith("bearer ") else self.bearer
            self.sess.headers["token"] = raw

        origin = BASE_URL.split("/api")[0] if "/api" in BASE_URL else BASE_URL
        self.sess.headers.setdefault("Referer", origin + "/")
        self.sess.headers.setdefault("X-Requested-With", "XMLHttpRequest")
        self.sess.headers.setdefault("Accept-Language", "en-US,en;q=0.9")

        if DEBUG_AUTH:
            masked_auth = (self.bearer[:16] + "…") if self.bearer else ""
            masked_sess = (sess_val[:8] + "…") if sess_val else ""
            log.info("GV auth applied (auth=%s, PHPSESSID=%s, account=%s)", masked_auth, masked_sess, ck.get("account",""))

    def _persist_current_auth(self):
        cookies = {
            "session": self.sess.cookies.get("session", "") or self.sess.cookies.get("PHPSESSID", ""),
            "account": self.sess.cookies.get("account", ""),
            "password": self.sess.cookies.get("password", ""),
        }
        _save_session_to_file({"auth": self.sess.headers.get("Authorization", self.bearer), "cookies": cookies})

    def _full(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return urljoin(BASE_URL + "/", path.lstrip("/"))

    def _needs_reauth(self, resp: requests.Response, body: Any) -> bool:
        if resp.status_code in (401, 403):
            return True
        if isinstance(body, dict):
            code = str(body.get("code") if "code" in body else body.get("status", "")).strip()
            if code in ("401", 401, "403", 403):
                return True
            msg = str(body.get("message") or body.get("msg") or "").lower()
            if "please login" in msg or ("token" in msg and ("expire" in msg or "invalid" in msg)):
                return True
        return False

    def _login_refresh(self):
        script = os.path.join(os.path.dirname(__file__), LOGIN_SCRIPT)
        log.info("Refreshing GameVault session via: %s --force", LOGIN_SCRIPT)
        proc = subprocess.run([sys.executable, script, "--force"], capture_output=True, text=True, timeout=150)
        if proc.returncode != 0:
            raise RuntimeError(f"GameVault login script failed: rc={proc.returncode}\n{proc.stderr or proc.stdout}")
        self._apply_auth_from_file()
        log.info("GameVault session refreshed.")

    def _attach_defaults(self, kwargs: Dict[str, Any], method: str) -> Dict[str, Any]:
        headers = kwargs.setdefault("headers", {})
        if method.upper() != "GET":
            if TX_JSON:
                headers.setdefault("Content-Type", "application/json;charset=UTF-8")
            else:
                if "data" in kwargs and "json" not in kwargs:
                    headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        return kwargs

    def _inject_agent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if AGENT_ID and "agent_id" not in payload and "agentId" not in payload:
            payload["agent_id"] = AGENT_ID
            payload["agentId"]  = AGENT_ID
        return payload

    def request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        if not ENABLED:
            raise RuntimeError("GAMEVAULT_ENABLED is false")
        url = self._full(path)
        timeout = (TIMEOUT_MS or 45000) / 1000.0

        kwargs = self._attach_defaults(kwargs, method)
        r = self.sess.request(method.upper(), url, timeout=timeout, **kwargs)

        try:
            body = r.json()
        except Exception:
            body = {"status_code": r.status_code, "text": r.text}

        if self._needs_reauth(r, body):
            self._login_refresh()
            r = self.sess.request(method.upper(), url, timeout=timeout, **kwargs)
            try:
                body = r.json()
            except Exception:
                body = {"status_code": r.status_code, "text": r.text}

        if r.status_code >= 400:
            raise RuntimeError(f"GameVault HTTP {r.status_code}: {body}")
        self._persist_current_auth()
        return body

GV = GVClient()  # singleton

# -------------------- Public dataclasses & helpers --------------------
@dataclass
class CreditInput:
    """Input for recharge/redeem calls (accepts numeric user_id or username)."""
    user_id: str  # may be numeric id OR gv login name (we’ll detect)
    amount: float
    memo: str = ""
    op_type: str = "recharge"  # or "redeem"

# Legacy shims (UI may import these)
@dataclass
class LegacyCreditInput:
    request_id: str
    username: str
    amount: int
    note: str = ""
    vendor_user_id: Optional[str] = None

@dataclass
class RedeemInput:
    request_id: str
    username: str
    amount: int
    note: str = ""
    vendor_user_id: Optional[str] = None

# -------------------- User lookup --------------------
def _lookup_user_id_by_username(username: Optional[str]) -> Optional[str]:
    if not username:
        return None
    try:
        for page in range(1, 11):  # scan up to 500 users if needed
            body = user_list(page=page, page_size=50)
            items = (body or {}).get("data", {}).get("list", []) if isinstance(body, dict) else []
            for it in items:
                if (it.get("login_name") or "").strip().lower() == username.strip().lower():
                    return str(it.get("user_id"))
    except Exception:
        return None
    return None

def _ensure_user_id(vendor_user_id: Optional[str], username: Optional[str]) -> str:
    uid = (vendor_user_id or "").strip()
    if uid:
        return uid
    uid = _lookup_user_id_by_username(username)
    if not uid:
        raise RuntimeError("protocol param error: missing user_id (mapping/lookup failed)")
    return uid

# -------------------- API helpers --------------------
def _post_gv(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = GV._inject_agent(payload)
    if TX_JSON:
        return GV.request("POST", path, json=payload)
    else:
        return GV.request("POST", path, data=payload)

def _ok(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    code = body.get("code", body.get("status", None))
    msg  = (body.get("msg") or body.get("message") or "").lower()
    return (code in (0, 200, "0", "200")) or ("success" in msg)

def _is_numeric(s: str) -> bool:
    return bool(re.fullmatch(r"\d+", (s or "").strip()))

# -------------------- Create / Credit / Redeem --------------------
def create_user(username: str, password: Optional[str] = None) -> Dict[str, Any]:
    # idempotent prefix/suffix
    uname = (username or "").strip()
    if GV_USERNAME_PREFIX and not uname.startswith(GV_USERNAME_PREFIX):
        uname = GV_USERNAME_PREFIX + uname
    if GV_USERNAME_SUFFIX and not uname.endswith(GV_USERNAME_SUFFIX):
        uname = uname + GV_USERNAME_SUFFIX
    payload = {"username": uname, "password": password or GV_DEFAULT_PASSWORD}
    return _post_gv(CREATE_PATH, payload)

def credit_recharge(inp: CreditInput) -> Dict[str, Any]:
    uid_or_name = str(inp.user_id).strip()
    amt_i  = int(float(inp.amount))
    remark = inp.memo or ""
    is_id  = _is_numeric(uid_or_name)

    # Try most common field shapes first
    variants = []

    if is_id:
        variants += [
            {"userId": uid_or_name, "type": 1, "money": amt_i, "remark": remark},
            {"uid": uid_or_name, "type": 1, "amount": amt_i, "remark": remark},
            {"user_id": uid_or_name, "type": 1, "amount": amt_i, "memo": remark},
            {"user_id": uid_or_name, "type": "recharge", "amount": amt_i, "memo": remark},
        ]
    else:
        # username-based fallbacks (some panels accept these)
        variants += [
            {"loginName": uid_or_name, "type": 1, "amount": amt_i, "remark": remark},
            {"username":  uid_or_name, "type": 1, "money": amt_i,  "remark": remark},
            {"login_name":uid_or_name, "type": 1, "amount": amt_i, "memo":   remark},
        ]

    last = None
    for p in variants:
        try:
            body = _post_gv(CREDIT_PATH, p)
            last = body
            if _ok(body):
                return body
        except Exception as e:
            last = {"error": str(e)}
            continue
    # make error explicit to help debugging
    return last or {"msg": "protocol param error", "status": 400}

def credit_redeem(user_id: str, amount: float, memo: str = "") -> Dict[str, Any]:
    uid_or_name = str(user_id).strip()
    amt_i  = int(float(amount))
    remark = memo or ""
    is_id  = _is_numeric(uid_or_name)

    variants = []
    if is_id:
        variants += [
            {"userId": uid_or_name, "type": 2, "money": amt_i, "remark": remark},
            {"uid": uid_or_name, "type": 2, "amount": amt_i, "remark": remark},
            {"user_id": uid_or_name, "type": 2, "amount": amt_i, "memo": remark},
            {"user_id": uid_or_name, "type": "redeem", "amount": amt_i, "memo": remark},
        ]
    else:
        variants += [
            {"loginName": uid_or_name, "type": 2, "amount": amt_i, "remark": remark},
            {"username":  uid_or_name, "type": 2, "money": amt_i,  "remark": remark},
            {"login_name":uid_or_name, "type": 2, "amount": amt_i, "memo":   remark},
        ]

    last = None
    for p in variants:
        try:
            body = _post_gv(REDEEM_PATH, p)
            last = body
            if _ok(body):
                return body
        except Exception as e:
            last = {"error": str(e)}
            continue
    return last or {"msg": "protocol param error", "status": 400}

# ---------- Back-compat shims ----------
def credit_deposit(inp: LegacyCreditInput) -> Dict[str, Any]:
    uid = _ensure_user_id(inp.vendor_user_id, inp.username)
    return credit_recharge(CreditInput(user_id=str(uid), amount=int(inp.amount),
                                       memo=inp.note or inp.request_id, op_type="recharge"))

def redeem_withdraw(inp: RedeemInput) -> Dict[str, Any]:
    uid = _ensure_user_id(inp.vendor_user_id, inp.username)
    return credit_redeem(str(uid), int(inp.amount), memo=inp.note or inp.request_id)

class GameVaultClient:
    def lookup_user_id(self, username: str) -> Optional[str]:
        return _lookup_user_id_by_username(username)

# -------------------- Misc API --------------------
def user_list(page: int = 1, page_size: int = 1) -> Dict[str, Any]:
    payload = {"page": page, "pageSize": page_size, "pageNum": page, "page_size": page_size}
    payload = GV._inject_agent(payload)
    if USERLIST_METHOD == "GET":
        return GV.request("GET", USERLIST_PATH, params=payload)
    else:
        if TX_JSON:
            return GV.request("POST", USERLIST_PATH, json=payload)
        else:
            return GV.request("POST", USERLIST_PATH, data=payload)

def ping_ok() -> bool:
    try:
        payload = {"page": 1, "pageSize": 1, "pageNum": 1, "page_size": 1}
        payload = GV._inject_agent(payload)
        if PING_METHOD == "GET":
            body = GV.request("GET", PING_PATH, params=payload)
        else:
            if TX_JSON:
                body = GV.request("POST", PING_PATH, json=payload)
            else:
                body = GV.request("POST", PING_PATH, data=payload)
        code = str(body.get("code") if isinstance(body, dict) and "code" in body else body.get("status", ""))
        if code in ("401", 401, "403", 403):
            return False
        return True
    except Exception as e:
        log.warning("GameVault ping failed: %s", e)
        return False