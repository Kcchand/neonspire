#!/usr/bin/env python3
"""
gv_login_capture.py
-------------------
Normalizes and saves a GameVault session to GAMEVAULT_SESSION_FILE.

Writes JSON in this shape (what the adapter expects):
{
  "auth": "Bearer ...",
  "cookies": {
    "session": "<PHPSESSID|session id>",
    "account": "<account cookie (optional)>",
    "password": "<password cookie (optional, base64 or raw)>"
  },
  "updated_at": 1730000000
}

CLI:
  --force                 overwrite even if file exists
  --bearer                Authorization header value ("Bearer ..." or raw token)
  --phpsessid             PHPSESSID / session id (value only or "PHPSESSID=...")
  --account-cookie        value for 'account' cookie (optional)
  --password-cookie       value for 'password' cookie (optional)

Env fallbacks (loaded via python-dotenv):
  GAMEVAULT_SESSION_FILE (default: .gv_session.json)
  GAMEVAULT_AUTH
  GAMEVAULT_SESSION
  GAMEVAULT_COOKIE_ACCOUNT
  GAMEVAULT_COOKIE_PASSWORD
  (GV_* fallbacks also supported)
"""
import os
import json
import time
import argparse
from dotenv import load_dotenv

# ------- env -------
load_dotenv(override=True)

def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v not in (None, ""):
            return v
    return default

SESSION_FILE = os.path.abspath(_env("GAMEVAULT_SESSION_FILE", "GV_SESSION_FILE", default=".gv_session.json"))

# ------- args -------
p = argparse.ArgumentParser(description="Capture/normalize GameVault session to JSON")
p.add_argument("--force", action="store_true", help="overwrite even if session file exists")
p.add_argument("--bearer", help="Authorization header value (with or without 'Bearer ' prefix)")
p.add_argument("--phpsessid", help="PHPSESSID/session id (value only, or 'PHPSESSID=...')")
p.add_argument("--account-cookie", dest="account_cookie", help="optional cookie: account")
p.add_argument("--password-cookie", dest="password_cookie", help="optional cookie: password")
args = p.parse_args()

# ------- helpers -------
def norm_bearer(x: str) -> str:
    x = (x or "").strip().strip('"').strip("'")
    if not x:
        return ""
    return x if x.lower().startswith("bearer ") else f"Bearer {x}"

def norm_session(x: str) -> str:
    x = (x or "").strip().strip('"').strip("'")
    if not x:
        return ""
    low = x.lower()
    if low.startswith("phpsessid="):
        return x.split("=", 1)[1]
    if low.startswith("sessionid="):
        return x.split("=", 1)[1]
    return x

def load_existing(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def backup_if_exists(path: str):
    if os.path.exists(path):
        try:
            bak = path + ".bak"
            with open(path, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())
        except Exception:
            pass

# ------- inputs (CLI overrides env) -------
bearer = args.bearer or _env("GAMEVAULT_AUTH", "GV_AUTH")
sess   = args.phpsessid or _env("GAMEVAULT_SESSION", "GV_SESSION")
acct   = args.account_cookie or _env("GAMEVAULT_COOKIE_ACCOUNT", "GV_COOKIE_ACCOUNT")
pwd    = args.password_cookie or _env("GAMEVAULT_COOKIE_PASSWORD", "GV_COOKIE_PASSWORD")

# If not forced and session file already exists and no explicit new creds, keep it.
if not args.force and os.path.exists(SESSION_FILE) and not (bearer or sess or acct or pwd):
    print(f"✔ Session already saved in {SESSION_FILE} — keeping it.\n(Run with --force to overwrite.)")
    raise SystemExit(0)

# If missing essentials, do a quick interactive prompt (one-time)
if not bearer or not sess:
    print("""
GameVault session capture
-------------------------
Open the portal, log in, then in DevTools → Network select a request like
'userList' or 'rechargeRedeem'. Copy the 'Authorization' header (Bearer …)
and the 'PHPSESSID' cookie value.
""")
    if not bearer:
        bearer = input("Paste Authorization header or token: ").strip()
    if not sess:
        sess = input("Paste PHPSESSID value (or full 'PHPSESSID=...'): ").strip()

# Normalize
bearer_norm = norm_bearer(bearer)
sess_norm   = norm_session(sess)
acct_norm   = (acct or "").strip()
pwd_norm    = (pwd or "").strip()

if not bearer_norm or not sess_norm:
    raise SystemExit("✖ Missing bearer or session id. Aborting.")

# Ensure dir exists
os.makedirs(os.path.dirname(SESSION_FILE) or ".", exist_ok=True)

# Backup current file
backup_if_exists(SESSION_FILE)

# Target structure expected by adapter
data = {
    "auth": bearer_norm,
    "cookies": {
        "session": sess_norm,
        "account": acct_norm,
        "password": pwd_norm,
    },
    "updated_at": int(time.time()),
}

with open(SESSION_FILE, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)

print(f"✔ Saved GameVault session → {SESSION_FILE}\n{json.dumps(data, indent=2)}")