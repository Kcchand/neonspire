# autoprovision_gv.py
from __future__ import annotations

import argparse
import sys
from typing import Optional

from dotenv import load_dotenv

# Ensure .env is loaded so gamevault_automation sees the env vars
load_dotenv(override=True)

# ---- App bootstrap (supports either app factory or module-level app) ----
def bootstrap_app():
    """
    Return a Flask app instance. Works if you use:
      - app.py with create_app(), or
      - app.py exposing a module-level 'app'
    """
    try:
        # Preferred: factory pattern
        from app import create_app  # type: ignore
        return create_app()
    except Exception:
        # Fallback: global app
        from app import app  # type: ignore
        return app

# ---- Imports that require the app context (models/DB) ----
from models import db, User, ExternalAccount, get_or_create_external_account  # type: ignore
from sqlalchemy import func

# GameVault helpers
from gamevault_automation import (
    create_user,
    user_list,
    GV_USERNAME_PREFIX,
    GV_USERNAME_SUFFIX,
)

VENDOR = "gamevault"


# -------------------- GameVault lookups --------------------
def gv_lookup_user_id(login_name: str) -> Optional[str]:
    """
    Look up a GameVault numeric user_id by login_name, scanning up to 5 pages (250 rows).
    """
    login_name_l = (login_name or "").strip().lower()
    if not login_name_l:
        return None

    for page in range(1, 6):
        try:
            body = user_list(page=page, page_size=50) or {}
        except Exception:
            # network or auth error — let caller decide what to do
            return None

        data = body.get("data", {}) if isinstance(body, dict) else {}
        items = data.get("list", []) if isinstance(data, dict) else []
        for it in items:
            if (it.get("login_name") or "").strip().lower() == login_name_l:
                uid = it.get("user_id")
                return str(uid) if uid is not None else None
    return None


def _build_gv_username(local_user_id: int) -> str:
    """
    Build the GV username using your configured prefix/suffix.
    Example: tzgm0_ + 123 + _gv
    """
    return f"{GV_USERNAME_PREFIX}{local_user_id}{GV_USERNAME_SUFFIX}"


# -------------------- Provision / ensure mapping --------------------
def ensure_mapping(u: User) -> None:
    """
    Ensure the given local user has a corresponding GameVault account and a local mapping.
    - If already mapped (ExternalAccount has vendor_user_id), do nothing.
    - Else, create/read the GV user and store the mapping.
    """
    # Already mapped?
    existing = ExternalAccount.query.filter_by(user_id=u.id, vendor=VENDOR).first()
    if existing and (existing.vendor_user_id or existing.vendor_username):
        print(f"✔ user {u.id} already mapped → {existing.vendor_username or existing.vendor_user_id}")
        return

    gv_username = _build_gv_username(u.id)

    # 1) Try to find the GV user
    gv_id = gv_lookup_user_id(gv_username)

    # 2) If not found, create it; then look up again to get numeric id
    if not gv_id:
        try:
            resp = create_user(gv_username)  # default password handled in gamevault_automation
        except Exception as e:
            print(f"❌ GV create error for user {u.id} ({gv_username}): {e}")
            return

        # Some panels return the id in the response; try to read it
        if isinstance(resp, dict):
            gv_id = str((resp.get("data") or {}).get("user_id") or resp.get("user_id") or "").strip() or None

        # If still not present, page through the list to pick it up
        if not gv_id:
            gv_id = gv_lookup_user_id(gv_username)

    if not gv_id:
        print(f"❌ could not resolve/create GV user for local user {u.id} ({gv_username})")
        return

    # 3) Write/Update local mapping
    rec = get_or_create_external_account(
        user_id=u.id,
        vendor=VENDOR,
        vendor_user_id=gv_id,
        vendor_username=gv_username,
    )
    db.session.add(rec)
    db.session.commit()
    print(f"✅ linked user {u.id} → {gv_username} ({gv_id})")


# -------------------- CLI main --------------------
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