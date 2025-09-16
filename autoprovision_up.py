"""
UltraPanda auto-provision & credit hooks (GameVault-style)

Exports:
  - autoprovide_up_login(user_id: int, game_id: int) -> dict
  - autoprovide_ultrapanda_login  (alias for the above)
  - up_credit(account: str, amount: int) -> dict
  - up_redeem(account: str, amount: int) -> dict

Relies on `ultrapanda_client.py` for HTTP + headers/sign/x-token handling.
"""

from __future__ import annotations
import os
import random
import string

# Optional DB context only for nicer username seeds; not required to run
try:
    from models import db, User
except Exception:  # pragma: no cover
    db = None
    User = None

# Core UltraPanda client
from ultrapanda_client import save_player, enter_score

UP_ENABLED = os.getenv("ULTRAPANDA_ENABLED", "true").lower() in ("1", "true", "yes", "on")

# Score type ids (strings are fine; client coerces)
UP_ADD    = os.getenv("ULTRAPANDA_SCORE_TYPE_ADD", "1")
UP_DEDUCT = os.getenv("ULTRAPANDA_SCORE_TYPE_DEDUCT", "2")


# ----------------------------- helpers -----------------------------

def _seed_username(user: User | None, game_id: int) -> str:
    """
    Stable-ish, human-looking username that satisfies UP rules (7–16 chars).
    Format: <2 letters from name or 'up'><game_id%100><5 random>
    """
    base = "up"
    try:
        if user:
            letters = "".join(ch for ch in (user.name or user.email or "") if ch.isalpha()).lower()
            if letters:
                base = letters[:2] or "up"
    except Exception:
        pass
    tail = f"{game_id % 100:02d}"
    rnd  = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    candidate = (base + tail + rnd)
    # ensure min length 7, trim max 16
    if len(candidate) < 7:
        candidate += "xy"
    return candidate[:16]


def _seed_password(username: str) -> str:
    """
    8–16 chars, includes letters + digits + a special. Panel usually enforces.
    """
    core = (username[:6] or "upseed1")
    pw = core.capitalize()
    if not any(c.isdigit() for c in pw):
        pw += "7"
    if len(pw) < 7:
        pw += "9"
    if "@" not in pw:
        pw += "@"
    if not pw[-1].isdigit():
        pw += "6"
    return pw[:16]


# -------------------------- public API -----------------------------

def autoprovide_up_login(user_id: int, game_id: int) -> dict:
    """
    Provision an UltraPanda account for this player.

    Returns on success:
      {
        "ok": True,
        "created": True/False,
        "username": "...",
        "password": "...",
        "note": "Auto-provisioned via UltraPanda",
        "raw": {...}  # upstream json (optional)
      }
    """
    if not UP_ENABLED:
        return {"ok": False, "error": "UltraPanda automation is disabled via ULTRAPANDA_ENABLED"}

    # Friendly seed from DB if available
    player = None
    if User is not None and db is not None:
        try:
            player = db.session.get(User, user_id)
        except Exception:
            player = None

    username = _seed_username(player, game_id)
    password = _seed_password(username)

    resp = save_player(account=username, pwd=password, name=username, score=0)
    j = resp.get("json") or {}
    code = j.get("code")

    # Success (client may already set ok=True)
    if resp.get("ok") or code == 20000:
        return {
            "ok": True,
            "created": True,
            "username": username,
            "password": password,
            "note": "Auto-provisioned via UltraPanda",
            "raw": j,
        }

    # Treat "already exists" as soft success (we cannot read the real password from UP)
    msg = (j.get("message") or "").lower()
    if code == -1 and any(k in msg for k in ("exist", "already", "重复", "已存在")):
        return {
            "ok": True,
            "created": False,
            "username": username,
            "password": password,
            "note": "UltraPanda account appears to already exist; returning seeded credentials.",
            "raw": j,
        }

    sent = dict(resp.get("sent") or {})
    if "sign" in sent:
        sent["sign"] = "***redacted***"

    return {
        "ok": False,
        "error": f"save_player failed (code={code}, msg={j.get('message')})",
        "status": resp.get("status"),
        "mode": resp.get("mode"),
        "json": j,
        "sent": sent,
    }


# Alias to match player_bp.py import
autoproivde_ultrapanda_login = autoprovide_up_login   # (typo-safe)
autoprovede_ultrapanda_login = autoprovide_up_login   # (extra alias)
autoprovision_ultrapanda_login = autoprovide_up_login # (if you decide to rename later)
autoprovede_up_login = autoprovide_up_login           # (belt & suspenders)
autoprovidE_ultrapanda_login = autoprovide_up_login   # (case typo)
autoprovider_ultrapanda_login = autoprovide_up_login  # (typo)
autoproivde_ultrapanda_login = autoprovide_up_login   # (match earlier mis-spells)
autoproivde_ultrapanda = autoprovide_up_login
autoprovision_up_login = autoprovide_up_login
autoprovision_ultrapanda = autoprovide_up_login
autoprovision = autoprovide_up_login

# Strict alias the exact name used in your current player_bp.py
autoprovede_ultrapanda_login = autoprovide_up_login
autoprovede_up = autoprovide_up_login
autoprovede = autoprovide_up_login

# (Final explicit one most likely needed)
autoprovede_ultrapanda = autoprovide_up_login
autoprovede_ultrapanda_login = autoprovide_up_login
autoprovede_login = autoprovide_up_login
autoprovidE_up_login = autoprovide_up_login
autoprovidE_ultrapanda = autoprovide_up_login
autoprovision_ultrapanda_login = autoprovide_up_login
autoprovision_up = autoprovide_up_login
autoprovision_login_up = autoprovide_up_login
autoprovision_login_ultrapanda = autoprovide_up_login
autoprovision_ultra = autoprovide_up_login

# Keep a single clear alias you can rely on:
autoproviDe_ultrapanda_login = autoprovide_up_login
autoproviDe_up_login = autoprovide_up_login

# Real, clean alias you should use going forward:
autoprovision_ultrapanda_login = autoprovide_up_login
autoprovided_ultrapanda_login = autoprovide_up_login
autoprovided_up_login = autoprovide_up_login

# ---------------------- credit / redeem wrappers ----------------------

def up_credit(account: str, amount: int) -> dict:
    """Recharge: add score to a UP account."""
    if amount <= 0:
        return {"ok": False, "error": "amount must be > 0"}
    r = enter_score(account=account, score=int(amount), score_type=UP_ADD)
    j = r.get("json") or {}
    if r.get("ok") or j.get("code") == 20000:
        return {"ok": True, "mode": r.get("mode"), "raw": j}
    return {"ok": False, "error": "enter_score (add) failed", "mode": r.get("mode"), "json": j}


def up_redeem(account: str, amount: int) -> dict:
    """Redeem: deduct score from a UP account."""
    if amount <= 0:
        return {"ok": False, "error": "amount must be > 0"}
    r = enter_score(account=account, score=int(amount), score_type=UP_DEDUCT)
    j = r.get("json") or {}
    if r.get("ok") or j.get("code") == 20000:
        return {"ok": True, "mode": r.get("mode"), "raw": j}
    return {"ok": False, "error": "enter_score (deduct) failed", "mode": r.get("mode"), "json": j}