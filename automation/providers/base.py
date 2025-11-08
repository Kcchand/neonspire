# automation/providers/base.py
from __future__ import annotations
import asyncio
from typing import Any, Protocol, runtime_checkable

def _run_maybe_async(func, *args, **kwargs):
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(*args, **kwargs))
    res = func(*args, **kwargs)
    if asyncio.iscoroutine(res):
        return asyncio.run(res)
    return res

def result_ok(vendor: str | None, res: Any) -> bool:
    # normalize success flags across different wrappers
    if isinstance(res, dict):
        if res.get("ok") is True:
            return True
        if str(res.get("status", "")).lower() in {"ok", "success", "200"}:
            return True
        if res.get("success") is True:
            return True
    if res is True:
        return True
    if res is None and vendor == "milkyway":
        # milkyway bot often returns None on success
        return True
    if isinstance(res, str) and any(k in res.lower() for k in ("ok","success","done","loaded","credited","redeemed")):
        return True
    return False

def result_error_text(res: Any) -> str:
    if isinstance(res, dict):
        return str(res.get("error") or res)
    return str(res)

@runtime_checkable
class Provider(Protocol):
    key: str  # 'juwa' | 'gv' | 'milkyway' | 'vblink' | ...

    def detect(self, name: str, explicit_vendor: str | None) -> bool:
        """Return True if this provider matches a Game (by name or explicit vendor)."""

    def credit(self, login: str, amount: int, note: str) -> Any:
        """Recharge/credit balance on vendor."""

    def redeem(self, login: str, amount: int, note: str) -> Any:
        """Redeem/withdraw from vendor."""

    def auto_create(self) -> dict | None:
        """Optional auto-provision for accounts. Return dict with username/password if supported."""
        return None