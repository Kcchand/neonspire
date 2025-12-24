# automation/providers/vblink.py
from __future__ import annotations
import asyncio
from typing import Any, Dict, Tuple

# Import the async Playwright bot helpers
from automation.vblink_bot import (
    launch_browser,
    close_browser,
    login as vb_login,
    recharge as vb_recharge,  # async (page, account, amount, remark)
    redeem  as vb_redeem,     # async (page, account, amount, remark)  -> applies negative internally
)

# ---------------- internal helpers ----------------

def _extract(username=None, amount=None, note="", *args, **kwargs) -> Tuple[str, float, str]:
    """
    Accepts any of:
      credit(self, username, amount, note)
      credit(username, amount, note)
      credit(username, amount)
      credit(self, username, amount)
    And same for redeem().
    """
    # positional normalization
    pos = [username, amount, note, *args]
    # drop implicit self if present
    if len(pos) >= 4:
        # shape: [self, username, amount, note, ...]
        _, u, a, *rest = pos
        n = rest[0] if rest else kwargs.get("note", "")
    elif len(pos) == 3:
        u, a, n = pos[:3]
    elif len(pos) == 2:
        u, a = pos[:2]
        n = kwargs.get("note", "")
    else:
        # last-ditch: pull from kwargs
        u = kwargs.get("username")
        a = kwargs.get("amount")
        n = kwargs.get("note", "")

    return str(u), float(a), str(n or "")

async def _run_vblink(op: str, username: str, amount: float, note: str) -> Dict[str, Any]:
    bundle = None
    try:
        bundle = await launch_browser()
        _, _, _, page = bundle

        lr = await vb_login(page)
        if not (lr and lr.get("ok")):
            return {"ok": False, "error": f"login failed: {lr}"}

        if op == "credit":
            res = await vb_recharge(page, username, float(amount), note or "recharge")
        else:
            # vb_redeem itself sets negative points; amount should be positive here
            res = await vb_redeem(page, username, float(amount), note or "redeem")

        return res if isinstance(res, dict) else {"ok": False, "error": str(res)}
    except Exception as e:
        return {"ok": False, "error": f"{op} exception: {e}"}
    finally:
        try:
            await close_browser(bundle)
        except Exception:
            pass

# ---------------- public sync API (used by app) ----------------

def credit(username=None, amount=None, note:str="", *args, **kwargs):
    u, a, n = _extract(username, amount, note, *args, **kwargs)
    # Run the async bot and return a plain dict (no coroutine leakage)
    return asyncio.run(_run_vblink("credit", u, a, n))

def redeem(username=None, amount=None, note:str="", *args, **kwargs):
    u, a, n = _extract(username, amount, note, *args, **kwargs)
    # Amount remains positive; the bot applies the negative internally
    return asyncio.run(_run_vblink("redeem", u, a, n))