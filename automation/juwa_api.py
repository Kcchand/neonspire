# automation/juwa_api.py
from __future__ import annotations
import asyncio
from typing import Optional, Dict, Any

# import the Playwright bot internals you already have
from .juwa_ui_bot import (
    _launch, _close,
    juwa_login, create_user, recharge_user, redeem_user,
    DEFAULT_PLAYER_PASSWORD, USERNAME_PREFIX, USERNAME_SUFFIX, USERNAME_LEN, _rand
)

async def _run(action: str, **kwargs) -> Dict[str, Any]:
    """Open a fresh browser, login, do the action, close."""
    bundle = await _launch()
    _, _, _, page = bundle
    try:
        lr = await juwa_login(page)
        if not lr.get("ok"):
            return {"ok": False, "stage": "login", "error": lr}

        if action == "create":
            return await create_user(
                page,
                kwargs.get("account"),
                kwargs.get("password") or DEFAULT_PLAYER_PASSWORD,
            )
        elif action == "recharge":
            return await recharge_user(
                page,
                kwargs["account"],
                float(kwargs["amount"]),
                kwargs.get("remark", ""),
            )
        elif action == "redeem":
            return await redeem_user(
                page,
                kwargs["account"],
                float(kwargs["amount"]),
                kwargs.get("remark", ""),
            )
        else:
            return {"ok": False, "error": f"unknown action: {action}"}
    finally:
        await _close(bundle)

# ---- Sync-friendly wrappers for Flask routes ----
def create_sync(account: Optional[str] = None, password: Optional[str] = None) -> Dict[str, Any]:
    return asyncio.run(_run("create", account=account, password=password))

def recharge_sync(account: str, amount: float, remark: str = "") -> Dict[str, Any]:
    return asyncio.run(_run("recharge", account=account, amount=amount, remark=remark))

def redeem_sync(account: str, amount: float, remark: str = "") -> Dict[str, Any]:
    return asyncio.run(_run("redeem", account=account, amount=amount, remark=remark))