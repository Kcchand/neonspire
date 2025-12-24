# autoprovision_gv.py
from __future__ import annotations
import argparse
import sys
from typing import Optional, Dict, Any

from dotenv import load_dotenv
load_dotenv(override=True)

# ---- App bootstrap (supports either app factory or module-level app) ----
def bootstrap_app():
    try:
        from app import create_app  # type: ignore
        return create_app()
    except Exception:
        from app import app  # type: ignore
        return app

# ---- DB / models (need app context when used) ----
from models import db, User, ExternalAccount, get_or_create_external_account  # type: ignore
from sqlalchemy import func

# ---- GameVault client helpers ----
from gamevault_automation import (
    create_user,
    user_list,
    GV_USERNAME_PREFIX,
    GV_USERNAME_SUFFIX,
    GV_DEFAULT_PASSWORD,   # use panel's default password
)

VENDOR = "gamevault"


# -------------------- GameVault lookups --------------------
def gv_lookup_user_id(login_name: str) -> Optional[str]:
    login_name_l = (login_name or "").strip().lower()
    if not login_name_l:
        return None
    for page in range(1, 6):  # scan up to 250 users
        try:
            body = user_list(page=page, page_size=50) or {}
        except Exception:
            return None
        data = body.get("data", {}) if isinstance(body, dict) else {}
        items = data.get("list", []) if isinstance(data, dict) else []
        for it in items:
            if (it.get("login_name") or "").strip().lower() == login_name_l:
                uid = it.get("user_id")
                return str(uid) if uid is not None else None
    return None


def _build_gv_username(local_user_id: int) -> str:
    return f"{GV_USERNAME_PREFIX}{local_user_id}{GV_USERNAME_SUFFIX}"


# ========================= PUBLIC: used by player_bp =========================
def autoprovide_gamevault_login(user_id: int, game_id: int) -> Dict[str, Any]:
    """
    Create (or ensure) a GameVault account for this local user and return credentials.
    Return shape (expected by player_bp):
      { ok: bool, username: str, password: str, user_id: str, created: bool, note?: str, error?: str }
    """
    # Fetch player (needed for mapping)
    u = db.session.get(User, user_id)
    if not u:
        return {"ok": False, "error": f"user {user_id} not found"}

    gv_username = _build_gv_username(user_id)
    created = False

    # 1) already mapped?
    ea = ExternalAccount.query.filter_by(user_id=user_id, vendor=VENDOR).first()
    if ea and (ea.vendor_user_id or ea.vendor_username):
        # double-check the user still exists on GV; if not, recreate
        gv_id = ea.vendor_user_id or gv_lookup_user_id(ea.vendor_username or gv_username)
        if not gv_id:
            # recreate on panel
            try:
                create_user(gv_username)  # password is GV_DEFAULT_PASSWORD
                created = True
            except Exception as e:
                return {"ok": False, "error": f"GV create failed: {e}"}
            gv_id = gv_lookup_user_id(gv_username)
            if not gv_id:
                return {"ok": False, "error": "GV user not found after create"}
        # update mapping if needed
        if not ea.vendor_user_id:
            ea.vendor_user_id = gv_id
        if not ea.vendor_username:
            ea.vendor_username = gv_username
        db.session.add(ea)
        db.session.commit()
        return {
            "ok": True,
            "username": gv_username,
            "password": GV_DEFAULT_PASSWORD,
            "user_id": gv_id,
            "created": created,
            "note": "Ensured existing GameVault mapping",
        }

    # 2) not mapped → create or find, then write mapping
    gv_id = gv_lookup_user_id(gv_username)
    if not gv_id:
        try:
            create_user(gv_username)
            created = True
        except Exception as e:
            return {"ok": False, "error": f"GV create failed: {e}"}
        gv_id = gv_lookup_user_id(gv_username)
        if not gv_id:
            return {"ok": False, "error": "GV user not found after create"}

    rec = get_or_create_external_account(
        user_id=user_id,
        vendor=VENDOR,
        vendor_user_id=gv_id,
        vendor_username=gv_username,
    )
    db.session.add(rec)
    db.session.commit()

    return {
        "ok": True,
        "username": gv_username,
        "password": GV_DEFAULT_PASSWORD,
        "user_id": gv_id,
        "created": created,
        "note": "Auto-provisioned via GameVault",
    }


# ========================= CLI helpers (unchanged) =========================
def ensure_mapping(u: User) -> None:
    existing = ExternalAccount.query.filter_by(user_id=u.id, vendor=VENDOR).first()
    if existing and (existing.vendor_user_id or existing.vendor_username):
        print(f"✔ user {u.id} already mapped → {existing.vendor_username or existing.vendor_user_id}")
        return

    gv_username = _build_gv_username(u.id)
    gv_id = gv_lookup_user_id(gv_username)

    if not gv_id:
        try:
            resp = create_user(gv_username)
        except Exception as e:
            print(f"❌ GV create error for user {u.id} ({gv_username}): {e}")
            return
        if isinstance(resp, dict):
            gv_id = str((resp.get("data") or {}).get("user_id") or resp.get("user_id") or "").strip() or None
        if not gv_id:
            gv_id = gv_lookup_user_id(gv_username)

    if not gv_id:
        print(f"❌ could not resolve/create GV user for local user {u.id} ({gv_username})")
        return

    rec = get_or_create_external_account(
        user_id=u.id, vendor=VENDOR, vendor_user_id=gv_id, vendor_username=gv_username
    )
    db.session.add(rec)
    db.session.commit()
    print(f"✅ linked user {u.id} → {gv_username} ({gv_id})")


def run_backfill(limit: int = 200) -> None:
    users = (
        User.query
        .filter(func.coalesce(User.role, "PLAYER") == "PLAYER")
        .order_by(User.created_at.desc())
        .limit(limit)
        .all()
    )
    print(f"Backfilling {len(users)} players…")
    for u in users:
        ensure_mapping(u)
    print("Done.")


def run_single(user_id: int) -> None:
    u = db.session.get(User, user_id)
    if not u:
        print(f"User {user_id} not found.")
        return
    ensure_mapping(u)


def main():
    parser = argparse.ArgumentParser(description="Create/link GameVault accounts for local users.")
    parser.add_argument("--backfill", action="store_true", help="Create & link GV accounts for recent players")
    parser.add_argument("--limit", type=int, default=200, help="How many recent players to scan when using --backfill (default: 200)")
    parser.add_argument("--user-id", type=int, help="Create/link GV account for a single local user id")
    args = parser.parse_args()

    if not (args.backfill or args.user_id):
        parser.print_help()
        sys.exit(2)

    app = bootstrap_app()
    with app.app_context():
        if args.user_id:
            run_single(args.user_id)
        if args.backfill:
            run_backfill(limit=args.limit)


if __name__ == "__main__":
    main()