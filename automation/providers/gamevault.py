# automation/providers/gamevault.py
from typing import Any
import asyncio

key = "gv"
detect_names = ("gamevault", "game vault", "gv", "gvault")

# gamevault_automation may expose async functions in your project
try:
    from gamevault_automation import gv_credit as _gv_credit, gv_redeem as _gv_redeem
except Exception as e:  # pragma: no cover
    _IMPORT_ERROR = e
    _gv_credit = None
    _gv_redeem = None

def _run_maybe_async(func, *args, **kwargs):
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(*args, **kwargs))
    res = func(*args, **kwargs)
    if asyncio.iscoroutine(res):
        return asyncio.run(res)
    return res

def credit(account: str, amount: int, note: str) -> Any:
    if not _gv_credit:
        return {"ok": False, "error": f"GameVault not configured: {_IMPORT_ERROR if '_IMPORT_ERROR' in globals() else 'gv_credit unavailable'}"}
    return _run_maybe_async(_gv_credit, account, int(amount), note)

def redeem(account: str, amount: int, note: str) -> Any:
    if not _gv_redeem:
        return {"ok": False, "error": f"GameVault not configured: {_IMPORT_ERROR if '_IMPORT_ERROR' in globals() else 'gv_redeem unavailable'}"}
    return _run_maybe_async(_gv_redeem, account, int(amount), note)