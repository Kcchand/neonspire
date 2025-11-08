# automation/providers/milkyway.py
from typing import Any

key = "milkyway"
detect_names = ("milkyway", "milky")

try:
    from automation.milkyway_ui_bot import (
        mw_recharge as _recharge,   # (account, amount, note="")
        mw_redeem as _redeem,       # (account, amount, note="")
        mw_create_player_auto as _auto_create,
    )
except Exception:  # pragma: no cover
    def _recharge(*a, **k):
        return {"ok": False, "error": "milkyway_ui_bot.mw_recharge not available"}
    def _redeem(*a, **k):
        return {"ok": False, "error": "milkyway_ui_bot.mw_redeem not available"}
    def _auto_create(*a, **k):
        return {"ok": False, "error": "milkyway_ui_bot.mw_create_player_auto not available"}

def credit(account: str, amount: int, note: str) -> Any:
    return _recharge(account, int(amount), note)

def redeem(account: str, amount: int, note: str) -> Any:
    return _redeem(account, int(amount), note)

def auto_create() -> Any:
    """
    Expected shape on success:
      {"ok": True, "username": "...", "password": "...", "note": "Auto-provisioned via Milkyway"}
    """
    res = _auto_create()
    # Normalize some common outputs from UI bot if needed
    if isinstance(res, dict) and (res.get("ok") or res.get("created")):
        username = (res.get("account") or res.get("username"))
        password = (res.get("password") or username)
        if username:
            return {"ok": True, "username": username, "password": password, "note": res.get("note") or "Auto-provisioned"}
    return res