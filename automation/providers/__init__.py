"""
Unified game automation facade.

Exports:
- detect_vendor(game)
- provider_credit(vendor, account, amount, note="")
- provider_redeem(vendor, account, amount, note="")
- provider_auto_create(vendor)  # optional
- result_ok(res) / result_error_text(res)
- all_providers, by_key, detect_by_name
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Any, Union, List
import asyncio

# Import concrete providers (each may gracefully degrade if their deps are missing)
from . import juwa
from . import gamevault as gv
from . import milkyway
from . import vblink

# ---------------------------------------------------------------------------
# YOLO (Playwright bot)
# ---------------------------------------------------------------------------
try:
    from automation.yolo_bot import (
        recharge_sync as yolo_credit_sync,
        redeem_sync as yolo_redeem_sync,
        auto_create_sync as yolo_auto_create_sync,
    )
    _YOLO_AVAILABLE = True
except Exception:
    _YOLO_AVAILABLE = False

    def yolo_credit_sync(*args, **kwargs):
        return {"ok": False, "error": "YOLO bot not available (missing deps)"}

    def yolo_redeem_sync(*args, **kwargs):
        return {"ok": False, "error": "YOLO bot not available (missing deps)"}

    def yolo_auto_create_sync():
        return {"ok": False, "error": "YOLO auto-create not available"}


# ---------------------------------------------------------------------------
# UltraPanda (Playwright bot)
# ---------------------------------------------------------------------------
try:
    from automation.ultrapanda_ui_bot import (
        recharge_sync as up_credit_sync,
        redeem_sync as up_redeem_sync,
    )
    _ULTRAPANDA_AVAILABLE = True
except Exception:
    _ULTRAPANDA_AVAILABLE = False

    def up_credit_sync(*args, **kwargs):
        return {"ok": False, "error": "Ultrapanda bot not available (missing dependencies)"}

    def up_redeem_sync(*args, **kwargs):
        return {"ok": False, "error": "Ultrapanda bot not available (missing dependencies)"}


# ---------------------------------------------------------------------------
# FireKirin (Playwright bot)
# ---------------------------------------------------------------------------
try:
    from automation.firekirin_ui_bot import (
        recharge_sync as fk_credit_sync,
        redeem_sync as fk_redeem_sync,
    )
    _FIREKIRIN_AVAILABLE = True
except Exception:
    _FIREKIRIN_AVAILABLE = False

    def fk_credit_sync(*args, **kwargs):
        return {"ok": False, "error": "FireKirin bot not available (missing dependencies)"}

    def fk_redeem_sync(*args, **kwargs):
        return {"ok": False, "error": "FireKirin bot not available (missing dependencies)"}


# ---------------------------------------------------------------------------
# Orion Stars (Playwright bot) - NEW ADDITION
# ---------------------------------------------------------------------------
try:
    from automation.orionstars_ui_bot import (
        recharge_sync as os_credit_sync,
        redeem_sync as os_redeem_sync,
        auto_create_sync as os_auto_create_sync,
    )
    _ORIONSTARS_AVAILABLE = True
except Exception:
    _ORIONSTARS_AVAILABLE = False

    def os_credit_sync(*args, **kwargs):
        return {"ok": False, "error": "Orion Stars bot not available (missing dependencies)"}

    def os_redeem_sync(*args, **kwargs):
        return {"ok": False, "error": "Orion Stars bot not available (missing dependencies)"}

    def os_auto_create_sync():
        return {"ok": False, "error": "Orion Stars auto-create not available"}


# ---------------------------------------------------------------------------
# Gameroom (async bot wrapper)
# ---------------------------------------------------------------------------
try:
    from automation import gameroom_bot as grm
    _GAMEROOM_IMPORTED = True
except Exception:
    grm = None
    _GAMEROOM_IMPORTED = False


# ---------- Success / error normalization ------------------------------------
def result_ok(vendor: Optional[str], res: Any) -> bool:
    """
    Normalize success across different wrappers.
    """
    if isinstance(res, dict):
        if res.get("ok") is True:
            return True
        if str(res.get("status", "")).lower() in {"ok", "success", "200"}:
            return True
        if res.get("success") is True:
            return True
    if res is True:
        return True
    # Milkyway UI wrappers may return None on success
    if res is None and (vendor or "").lower() == "milkyway":
        return True
    if isinstance(res, str) and any(
        k in res.lower() for k in ("ok", "success", "done", "loaded", "credited", "recharged", "redeemed")
    ):
        return True
    return False


def result_error_text(res: Any) -> str:
    if isinstance(res, dict):
        return str(res.get("error") or res)
    return str(res)


# ---------- Provider registry -------------------------------------------------
@dataclass
class Provider:
    key: str
    credit: Callable[[str, int, str], Any]     # (account, amount, note) -> result
    redeem: Callable[[str, int, str], Any]     # (account, amount, note) -> result
    auto_create: Optional[Callable[[], Any]] = None  # optional


by_key: dict[str, Provider] = {
    "juwa": Provider(
        key="juwa",
        credit=juwa.credit,
        redeem=juwa.redeem,
        auto_create=None,
    ),
    "gv": Provider(
        key="gv",
        credit=gv.credit,
        redeem=gv.redeem,
        auto_create=None,
    ),
    "gamevault": Provider(  # alias to gv
        key="gv",
        credit=gv.credit,
        redeem=gv.redeem,
        auto_create=None,
    ),
    "milkyway": Provider(
        key="milkyway",
        credit=milkyway.credit,          # <-- Milkyway UI wired here
        redeem=milkyway.redeem,          # <-- Milkyway UI wired here
        auto_create=milkyway.auto_create, # <-- Milkyway UI auto-create wired here
    ),
    "vblink": Provider(
        key="vblink",
        credit=vblink.credit,
        redeem=vblink.redeem,
        auto_create=None,
    ),
}

# Register UltraPanda
by_key["ultrapanda"] = Provider(
    key="ultrapanda",
    credit=lambda account, amount, note="": up_credit_sync(account, amount, note),
    redeem=lambda account, amount, note="": up_redeem_sync(account, amount, note),
    auto_create=None,
)

# Register YOLO
by_key["yolo"] = Provider(
    key="yolo",
    credit=lambda account, amount, note="": yolo_credit_sync(account, amount, note),
    redeem=lambda account, amount, note="": yolo_redeem_sync(account, amount, note),
    auto_create=yolo_auto_create_sync if _YOLO_AVAILABLE else None,
)

# Register FireKirin
by_key["firekirin"] = Provider(
    key="firekirin",
    credit=lambda account, amount, note="": fk_credit_sync(account, amount, note),
    redeem=lambda account, amount, note="": fk_redeem_sync(account, amount, note),
    auto_create=None,
)

# Register Orion Stars - NEW ADDITION
by_key["orionstars"] = Provider(
    key="orionstars",
    credit=lambda account, amount, note="": os_credit_sync(account, amount, note),
    redeem=lambda account, amount, note="": os_redeem_sync(account, amount, note),
    auto_create=os_auto_create_sync if _ORIONSTARS_AVAILABLE else None,
)

# Register Orion Stars aliases
by_key["orion"] = Provider(
    key="orionstars",
    credit=lambda account, amount, note="": os_credit_sync(account, amount, note),
    redeem=lambda account, amount, note="": os_redeem_sync(account, amount, note),
    auto_create=os_auto_create_sync if _ORIONSTARS_AVAILABLE else None,
)

by_key["os"] = Provider(
    key="orionstars",
    credit=lambda account, amount, note="": os_credit_sync(account, amount, note),
    redeem=lambda account, amount, note="": os_redeem_sync(account, amount, note),
    auto_create=os_auto_create_sync if _ORIONSTARS_AVAILABLE else None,
)


# ---------- Gameroom registration --------------------------------------------
if _GAMEROOM_IMPORTED:

    async def _gm_recharge_async(account: str, amount: int, note: str = "") -> dict:
        async with grm.async_playwright() as p:
            browser = await p.chromium.launch(headless=grm.HEADLESS, slow_mo=grm.SLOWMO)
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            try:
                await grm.do_login(page)
                await grm.ui_recharge(page, account, amount)
                return {"ok": True}
            finally:
                await browser.close()

    async def _gm_redeem_async(account: str, amount: int, note: str = "") -> dict:
        async with grm.async_playwright() as p:
            browser = await p.chromium.launch(headless=grm.HEADLESS, slow_mo=grm.SLOWMO)
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            try:
                await grm.do_login(page)
                await grm.ui_redeem(page, account, amount)
                return {"ok": True}
            finally:
                await browser.close()

    def _gm_recharge_sync(account: str, amount: int, note: str = "") -> dict:
        return asyncio.run(_gm_recharge_async(account, amount, note))

    def _gm_redeem_sync(account: str, amount: int, note: str = "") -> dict:
        return asyncio.run(_gm_redeem_async(account, amount, note))

    async def _gm_create_async() -> dict:
        async with grm.async_playwright() as p:
            browser = await p.chromium.launch(headless=grm.HEADLESS, slow_mo=grm.SLOWMO)
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            try:
                await grm.do_login(page)
                acct = grm.build_new_username()
                pwd = grm.DEFAULT_PASS
                info = await grm.ui_create_user(page, acct, pwd, grm.DEFAULT_CREDIT, nickname=acct)
                return {
                    "ok": True,
                    "username": info["account"],
                    "password": info["password"],
                    "note": "Gameroom auto-provisioned",
                }
            finally:
                await browser.close()

    def _gm_create_sync() -> dict:
        return asyncio.run(_gm_create_async())

    by_key["gameroom"] = Provider(
        key="gameroom",
        credit=_gm_recharge_sync,
        redeem=_gm_redeem_sync,
        auto_create=_gm_create_sync,
    )


# Keep list for legacy imports
all_providers = list({p.key for p in by_key.values()})


# ---------- Detection helpers -------------------------------------------------
def _normalized_vendor_string(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().lower()
    if s in {"gv", "gamevault", "game vault"}:
        return "gv"
    if s in {"milkyway", "milky"}:
        return "milkyway"
    if s in {"juwa"}:
        return "juwa"
    if s in {"vblink", "vb", "vb-link"}:
        return "vblink"
    if s in {"ultrapanda", "ultra panda", "up"}:
        return "ultrapanda"
    if s in {"yolo", "yolo777", "yolo 777", "yolo-777"}:
        return "yolo"
    if s in {"firekirin", "fire kirin", "fk"}:
        return "firekirin"
    if s in {"orionstars", "orion", "os", "orion stars", "orion-stars"}:
        return "orionstars"
    if s in {"gameroom", "game room", "gr"}:
        return "gameroom"
    return None


def detect_by_name(name: Optional[str]) -> Optional[str]:
    return _normalized_vendor_string(name or "")


def detect_vendor(game) -> Optional[str]:
    """
    Detect vendor for a Game model instance by checking:
    - game.vendor field (if present)
    - game.name field
    """
    if not game:
        return None

    v = getattr(game, "vendor", None)
    v = _normalized_vendor_string(v) if v else None
    if v:
        return v

    n = (getattr(game, "name", None) or "").strip().lower()
    if "gamevault" in n or n in {"gv", "game vault", "gvault"}:
        return "gv"
    if "milkyway" in n or "milky" in n:
        return "milkyway"
    if "juwa" in n:
        return "juwa"
    if "vblink" in n or "vb link" in n or "vb-link" in n or n in {"vb", "vblink"}:
        return "vblink"
    if "ultrapanda" in n or "ultra panda" in n or n in {"up"}:
        return "ultrapanda"
    if "yolo" in n or "yolo777" in n or "yolo 777" in n:
        return "yolo"
    if "firekirin" in n or "fire kirin" in n or n in {"fk"}:
        return "firekirin"
    if "orionstars" in n or "orion stars" in n or "orion-stars" in n or n in {"orion", "os"}:
        return "orionstars"
    if "gameroom" in n or "game room" in n or n == "gr":
        return "gameroom"
    return None


# ---------- Facade calls ------------------------------------------------------
def provider_credit(vendor: str, account: str, amount: int, note: str = "") -> Any:
    v = (vendor or "").lower()
    p = by_key.get(v) or by_key.get(_normalized_vendor_string(v) or "")
    if not p:
        return {"ok": False, "error": f"Unsupported vendor '{vendor}'"}
    return p.credit(account, int(amount), note)


def provider_redeem(vendor: str, account: str, amount: int, note: str = "") -> Any:
    v = (vendor or "").lower()
    p = by_key.get(v) or by_key.get(_normalized_vendor_string(v) or "")
    if not p:
        return {"ok": False, "error": f"Unsupported vendor '{vendor}'"}
    return p.redeem(account, int(amount), note)


def provider_auto_create(vendor: str) -> Any:
    v = (vendor or "").lower()
    p = by_key.get(v) or by_key.get(_normalized_vendor_string(v) or "")
    if not p or not p.auto_create:
        return {"ok": False, "error": f"Auto-create not supported for vendor '{vendor}'"}
    return p.auto_create()