# automation/providers/juwa.py
from __future__ import annotations
from typing import Any, Optional

# Use the UI-bot sync wrappers
from automation.juwa_api import (
    create_sync as juwa_create_sync,
    recharge_sync as juwa_recharge_sync,
    redeem_sync as juwa_redeem_sync,
)


def credit(account: str, amount: int, note: str = "") -> Any:
    """
    Provider-facing 'credit' (recharge) for Juwa.
    Called via provider_credit("juwa", ...).
    """
    return juwa_recharge_sync(account, float(amount), note)


def redeem(account: str, amount: int, note: str = "") -> Any:
    """
    Provider-facing 'redeem' for Juwa.
    Called via provider_redeem("juwa", ...).
    """
    return juwa_redeem_sync(account, float(amount), note)


def auto_create() -> Any:
    """
    Optional auto-create hook for Juwa.
    Normalizes the result shape a bit for the caller.
    """
    res = juwa_create_sync()

    # If juwa_api already returns a dict with ok/account/password,
    # just pass it through.
    if not isinstance(res, dict):
        return res

    if not res.get("ok", True) and not res.get("created", False):
        # Bubble up the error, don't fake success
        return res

    account = res.get("account") or res.get("username")
    password = res.get("password") or res.get("pass") or account
    return {
        "ok": True,
        "account": account,
        "password": password,
        "note": res.get("note", "Auto-provisioned via Juwa (UI bot)"),
    }