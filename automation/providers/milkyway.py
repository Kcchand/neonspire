# automation/providers/milkyway.py

from __future__ import annotations

from typing import Any, Dict, Optional, Union

key = "milkyway"
detect_names = ("milkyway", "milky")


# We import the wrapper funcs from automation/milkyway_ui_bot.py
# Your CLI already confirms these work:
#   python -m automation.milkyway_ui_bot recharge <user> <amount>
#   python -m automation.milkyway_ui_bot redeem   <user> <amount>
#   python -m automation.milkyway_ui_bot create-auto
try:
    from automation.milkyway_ui_bot import (
        mw_recharge as _recharge,          # (account_or_id, amount, note="")
        mw_redeem as _redeem,              # (account_or_id, amount, note="")
        mw_create_player_auto as _auto_create,  # () -> dict
    )
except Exception as e:  # pragma: no cover
    _IMPORT_ERROR = str(e)

    def _recharge(*a, **k):
        return {"ok": False, "error": f"milkyway_ui_bot.mw_recharge not available: {_IMPORT_ERROR}"}

    def _redeem(*a, **k):
        return {"ok": False, "error": f"milkyway_ui_bot.mw_redeem not available: {_IMPORT_ERROR}"}

    def _auto_create(*a, **k):
        return {"ok": False, "error": f"milkyway_ui_bot.mw_create_player_auto not available: {_IMPORT_ERROR}"}


def _to_amount_int(amount: Union[int, float, str]) -> int:
    """
    Milkyway UI expects integer amounts in your flow.
    Accepts int/float/"2"/"2.0" and converts safely.
    """
    if isinstance(amount, int):
        return amount
    if isinstance(amount, float):
        return int(round(amount))
    s = str(amount).strip()
    if not s:
        raise ValueError("amount is empty")
    return int(round(float(s)))


def _normalize_ok(res: Any) -> Dict[str, Any]:
    """
    Normalize provider results for the rest of your system.
    Always returns a dict.
    """
    if isinstance(res, dict):
        # If UI bot already returns ok/created, keep it
        if res.get("ok") is True:
            return res
        if res.get("created") is True:
            # convert created -> ok
            out = dict(res)
            out["ok"] = True
            return out
        return res
    return {"ok": True, "result": res}


def credit(account: str, amount: Union[int, float, str], note: str = "") -> Dict[str, Any]:
    """
    Website/worker calls this for Recharge.
    """
    amt = _to_amount_int(amount)
    res = _recharge(account, amt, note or "")
    out = _normalize_ok(res)
    # add consistent fields
    if out.get("ok") is True:
        out.setdefault("action", "recharge")
        out.setdefault("username", account)
        out.setdefault("amount", amt)
    return out


def redeem(account: str, amount: Union[int, float, str], note: str = "") -> Dict[str, Any]:
    """
    Website/worker calls this for Redeem.
    """
    amt = _to_amount_int(amount)
    res = _redeem(account, amt, note or "")
    out = _normalize_ok(res)
    if out.get("ok") is True:
        out.setdefault("action", "redeem")
        out.setdefault("username", account)
        out.setdefault("amount", amt)
    return out


def auto_create() -> Dict[str, Any]:
    """
    Website/worker calls this to auto create a player.

    Normalized success shape:
      {
        "ok": True,
        "username": "...",
        "password": "...",
        "note": "Auto-provisioned via Milkyway"
      }
    """
    res = _auto_create()
    if not isinstance(res, dict):
        # if UI bot returned something odd, still wrap it
        return {"ok": True, "result": res, "note": "Auto-provisioned via Milkyway"}

    # your mw_create_player_auto() returns something like:
    # {"created": True, "account": "...", "nickname": "..."}  OR includes password
    if res.get("ok") is True or res.get("created") is True:
        username = res.get("account") or res.get("username")
        if not username:
            return {"ok": False, "error": f"auto_create returned success but no username: {res}"}

        password = res.get("password") or username  # your system often uses username as password
        return {
            "ok": True,
            "username": username,
            "password": password,
            "note": res.get("note") or "Auto-provisioned via Milkyway",
            "raw": res,  # keep raw for debugging
        }

    # failure passthrough
    return res