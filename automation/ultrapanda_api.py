# automation/ultrapanda_api.py
from __future__ import annotations
import os, asyncio, threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Callable

import nest_asyncio
nest_asyncio.apply()

# pull in the working playwright routines
from automation.ultrapanda_ui_bot import (
    _launch, up_login, create_user as _create_user,
    recharge_user as _recharge_user, redeem_user as _redeem_user,
)

ULTRAPANDA_ENABLED = os.getenv("ULTRAPANDA_ENABLED", "1").lower() in ("1", "true", "yes", "on")

# --- tiny result helper -------------------------------------------------------
def _result(ok: bool, **kw) -> Dict:
    out = {"ok": bool(ok)}
    out.update(kw)
    return out

# --- async runner safe for Flask + terminal ----------------------------------
def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Flask / Jupyter already has an event loop
            return asyncio.get_event_loop().run_until_complete(coro)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)

async def _with_session(coro_fn):
    pw, browser, ctx, page = await _launch()
    try:
        lr = await up_login(page)
        if not lr.get("ok"):
            return _result(False, stage="login", error=lr.get("error") or "login failed")
        return await coro_fn(page)
    finally:
        for fn in (ctx.close, browser.close, pw.stop):
            try:
                await fn()
            except Exception:
                pass

# ------------------------- public sync API (used by UI) -----------------------
def up_supported() -> bool:
    return ULTRAPANDA_ENABLED

def up_create(account: Optional[str] = None, password: Optional[str] = None) -> Dict:
    if not ULTRAPANDA_ENABLED:
        return _result(False, error="UltraPanda disabled")

    async def _job(page):
        res = await _create_user(page, account, password)
        return _result(res.get("ok", False), **res)

    return run_async(_with_session(_job))

def up_recharge(account: str, amount: float, remark: str = "") -> Dict:
    if not ULTRAPANDA_ENABLED:
        return _result(False, error="UltraPanda disabled")

    async def _job(page):
        res = await _recharge_user(page, account, amount, remark or "recharge")
        return _result(res.get("ok", False), **res)

    return run_async(_with_session(_job))

def up_redeem(account: str, amount: float, remark: str = "") -> Dict:
    if not ULTRAPANDA_ENABLED:
        return _result(False, error="UltraPanda disabled")

    async def _job(page):
        res = await _redeem_user(page, account, amount, remark or "redeem")
        return _result(res.get("ok", False), **res)

    return run_async(_with_session(_job))

# ------------------------- autoprovision (background) -------------------------
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()

def _ensure_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=int(os.getenv("UP_AUTOPROV_WORKERS", "1")))

def start_ultrapanda_autoprovision(
    *,
    user_id: int,
    on_notify: Callable[[int, str], None],
    on_success: Callable[[int, Dict], None],
    on_error: Callable[[int, str], None],
    desired_username: Optional[str] = None,
    desired_password: Optional[str] = None,
) -> None:
    """
    Fire-and-forget job:
      1) send 'processing' notification
      2) run bot, create UP player
      3) on success -> persist GameAccount + notify ready
      4) on error -> notify error
    """
    if not ULTRAPANDA_ENABLED:
        on_error(user_id, "UltraPanda disabled")
        return

    _ensure_pool()

    def _runner():
        try:
            on_notify(user_id, "Your access request for UltraPanda is being processed.")
        except Exception:
            pass

        try:
            res = up_create(desired_username, desired_password)
            if not res.get("ok"):
                msg = res.get("error") or "UltraPanda create failed"
                on_error(user_id, msg)
                return
            on_success(user_id, res)
        except Exception as e:
            on_error(user_id, f"UltraPanda error: {e}")

    _pool.submit(_runner)