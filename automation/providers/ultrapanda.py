# automation/providers/ultrapanda.py
from __future__ import annotations
import asyncio
from typing import Any, Dict, Tuple, Optional

# import your async Ultrapanda bot primitives
from automation.ultrapanda_ui_bot import (
    _launch as up_launch,
    _close  as up_close,
    up_login,
    create_user as up_create_user,
    recharge_user as up_recharge_user,   # async (page, account, amount, remark)
    redeem_user  as up_redeem_user,      # async (page, account, amount, remark) -> passes negative inside
)

# ---------- small helpers ----------
def _norm_args(username=None, amount=None, note: str = "", *args, **kwargs) -> Tuple[str, float, str]:
    """
    Accepts either (username, amount, note) or (self, username, amount, note).
    Keeps amount POSITIVE for both credit & redeem; the bot applies +/- internally.
    """
    pos = [username, amount, note, *args]
    if len(pos) >= 4:            # (self, username, amount, note)
        _, u, a, *rest = pos
        n = rest[0] if rest else kwargs.get("note", "")
    elif len(pos) == 3:
        u, a, n = pos[:3]
    elif len(pos) == 2:
        u, a = pos[:2]; n = kwargs.get("note", "")
    else:
        u = kwargs.get("username"); a = kwargs.get("amount"); n = kwargs.get("note", "")
    return str(u), float(a), str(n or "")

async def _run(op: str, username: str, amount: float, note: str) -> Dict[str, Any]:
    bundle = None
    try:
        bundle = await up_launch()
        _, _, _, page = bundle

        lr = await up_login(page)
        if not lr.get("ok"):
            return {"ok": False, "error": f"login failed: {lr}"}

        if op == "create":
            # password optional; bot handles defaults
            res = await up_create_user(page, username or None, None)
        elif op == "credit":
            res = await up_recharge_user(page, username, abs(float(amount)), note or "recharge")
        elif op == "redeem":
            res = await up_redeem_user(page, username, abs(float(amount)), note or "redeem")
        else:
            return {"ok": False, "error": f"unknown op {op}"}

        return res if isinstance(res, dict) else {"ok": False, "error": str(res)}
    except Exception as e:
        return {"ok": False, "error": f"{op} exception: {e}"}
    finally:
        try:
            await up_close(bundle)
        except Exception:
            pass

# ---------- public SYNC API (used by Flask) ----------
def create(username: Optional[str] = None, password: Optional[str] = None):
    # password is ignored; Ultrapanda bot already handles default pwd
    return asyncio.run(_run("create", username or "", 0.0, ""))

def credit(username=None, amount=None, note: str = "", *args, **kwargs):
    u, a, n = _norm_args(username, amount, note, *args, **kwargs)
    return asyncio.run(_run("credit", u, a, n))

def redeem(username=None, amount=None, note: str = "", *args, **kwargs):
    u, a, n = _norm_args(username, amount, note, *args, **kwargs)
    return asyncio.run(_run("redeem", u, a, n))

# Optional: simple detector for game names in your UI (“Ultrapanda”, “UP”, etc.)
def detect_by_name(name: str) -> bool:
    return str(name or "").strip().lower() in {"ultrapanda", "ultra panda", "up"}