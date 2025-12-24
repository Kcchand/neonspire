# automation/__init__.py
from __future__ import annotations
from typing import Any, Optional, Tuple
from sqlalchemy import func

from models import db, Game, GameAccount  # only lightweight reads
from .providers import all_providers, by_key, detect_by_name

# --------- Public Facade (import this from blueprints) ---------

def get_vendor_for_game(game: Optional[Game]) -> Optional[str]:
    """
    Determine vendor key for a Game using:
      1) explicit Game.vendor (string)
      2) kv override game:{id}:vendor (handled in caller if needed)
      3) fuzzy match against providers by Game.name
    """
    if not game:
        return None
    v = (getattr(game, "vendor", None) or "").strip().lower()
    if v:
        # normalize to a registered key if possible
        if by_key(v):
            return v
        # allow common aliases (e.g. 'gamevault')
        found = detect_by_name(v)
        if found:
            return found
    # try name-based
    key = detect_by_name((getattr(game, "name", None) or ""))
    return key

def get_provider(vendor_key: Optional[str]):
    if not vendor_key:
        return None
    return by_key(vendor_key)

def find_login_username(user_id: int, game_id: Optional[int]=None, vendor_preference: Optional[str]=None) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Unified helper: try exact game_id match first, otherwise best vendor match,
    otherwise first available account for any registered provider.
    Returns (username, vendor_key, game_id)
    """
    # 1) exact match
    if user_id and game_id:
        acc = GameAccount.query.filter_by(user_id=user_id, game_id=game_id).first()
        if acc:
            uname = _first_attr(acc, "account_username","username","login","user")
            if uname:
                g = db.session.get(Game, game_id)
                vk = get_vendor_for_game(g)
                return (uname, vk, game_id)

    # 2) collect all accounts for the user and pick by vendor preference
    accounts = GameAccount.query.filter_by(user_id=user_id).all()
    options: list[Tuple[str,str,int]] = []  # (vendor_key, username, game_id)
    for acc in accounts:
        gid = getattr(acc, "game_id", None)
        g = db.session.get(Game, gid) if gid else None
        vk = get_vendor_for_game(g)
        if not vk:
            continue
        uname = _first_attr(acc, "account_username","username","login","user")
        if uname:
            options.append((vk, uname, gid))

    if not options:
        return (None, None, None)

    if vendor_preference:
        for vk, u, gid in options:
            if vk == vendor_preference:
                return (u, vk, gid)

    # prefer a stable order across popular vendors
    order = [p.key for p in all_providers()]
    for pref in order:
        for vk, u, gid in options:
            if vk == pref:
                return (u, vk, gid)

    # fallback
    vk, u, gid = options[0]
    return (u, vk, gid)


# ------------- tiny internal util (mirrors your blueprint helper) -------------
def _first_attr(obj, *names, default=""):
    if not obj:
        return default
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v not in (None, ""):
                return v
    return default


# ------------- thin wrappers used by blueprints -------------

def vendor_credit(vendor_key: str, login: str, amount: int, note: str) -> Any:
    p = get_provider(vendor_key)
    if not p:
        return {"ok": False, "error": f"Unsupported vendor {vendor_key}"}
    try:
        return p.credit(login, int(amount), note)
    except Exception as e:
        return {"ok": False, "error": str(e)}

def vendor_redeem(vendor_key: str, login: str, amount: int, note: str) -> Any:
    p = get_provider(vendor_key)
    if not p:
        return {"ok": False, "error": f"Unsupported vendor {vendor_key}"}
    try:
        return p.redeem(login, int(amount), note)
    except Exception as e:
        return {"ok": False, "error": str(e)}

def result_ok(vendor_key: str, res: Any) -> bool:
    p = get_provider(vendor_key)
    if not p:
        return False
    return p.result_ok(res)

def error_text(vendor_key: str, res: Any) -> str:
    p = get_provider(vendor_key)
    if not p:
        return str(res)
    return p.error_text(res)

def auto_create_if_supported(vendor_key: str):
    p = get_provider(vendor_key)
    if not p or not hasattr(p, "auto_create"):
        return None
    try:
        return p.auto_create()
    except Exception as e:
        return {"ok": False, "error": str(e)}