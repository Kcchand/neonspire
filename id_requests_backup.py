from models import User
from celery import shared_task
# id_requests.py

import logging
import time
from datetime import datetime
import os


from sqlalchemy.exc import OperationalError

from models import (
    db,
    User,
    Game,
    GameAccountRequest,
    Notification,
    notify,
)

# 👇 providers & helper from player_bp (DO NOT TOUCH player_bp.py)
from player_bp import PROVIDERS, _save_or_update_game_account

log = logging.getLogger("id_requests")

# =============================================================================
#                           REDIS + PROVIDER LOCKS
# =============================================================================
import redis

# Use same Redis as Celery broker by default
REDIS_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(REDIS_URL)


def _progress_key(req_id: int) -> str:
    return f"req_progress:{req_id}"


def update_progress(req_id: int, text: str):
    """
    Store a short, player-friendly progress string in Redis.
    NOTE: keep this text generic – no 'Retry 2/3' etc.
    """
    if not req_id:
        return
    try:
        redis_client.set(_progress_key(req_id), text, ex=3600)
    except Exception:
        log.exception("Failed to update progress for request %s", req_id)


def clear_progress(req_id: int):
    if not req_id:
        return
    try:
        redis_client.delete(_progress_key(req_id))
    except Exception:
        pass


def acquire_provider_lock(provider_code: str):
    """
    Simple provider-level lock to avoid multiple concurrent logins
    for the same backend (Vblink, GameVault, etc.).

    One provider_code = only one active create() at a time.
    """
    if not provider_code:
        return  # no-op if we can't figure out the provider

    key = f"prov_lock:{provider_code}"
    while True:
        # NX = only set if not exists, EX = expire in 300s (safety)
        if redis_client.set(key, "1", nx=True, ex=300):
            return
        # small delay before retrying lock
        time.sleep(1)


def release_provider_lock(provider_code: str):
    """
    Release the provider-level lock.
    """
    if not provider_code:
        return
    key = f"prov_lock:{provider_code}"
    try:
        redis_client.delete(key)
    except Exception:
        pass


# =============================================================================
#                           GAMEVAULT PROVIDER
# =============================================================================

# Same helper you use elsewhere
try:
    from gamevault_automation import gv_create_account
except Exception:
    gv_create_account = None


class GameVaultProvider:
    """
    Lightweight provider wrapper for GameVault used by Celery.

    Signature: create(self, user, req) so we can use user.name / user.email.
    """

    code = "GAMEVAULT"
    name = "GameVault"

    def create(self, user: User, req: GameAccountRequest) -> dict:
        if not gv_create_account:
            return {"ok": False, "error": "GameVault automation not configured"}

        # same pattern as your old instant mode:
        # gv_create_account(current_user.name or "", current_user.email or "")
        res = gv_create_account(user.name or "", user.email or "") or {}
        return res

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        return {"ok": False, "error": "GameVault recharge not implemented in worker"}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        return {"ok": False, "error": "GameVault redeem not implemented in worker"}


# Make sure PROVIDERS has a GameVault entry (upper-case key)
if "GAMEVAULT" not in PROVIDERS and "gamevault" not in PROVIDERS:
    PROVIDERS["GAMEVAULT"] = GameVaultProvider()
    log.info("Registered GameVaultProvider in PROVIDERS for worker")


# =============================================================================
#                       PROVIDER LOOKUP (BY GAME)
# =============================================================================

def _provider_for_game(game: Game):
    """
    Decide which GameProvider object to use for this Game row.

    Uses the PROVIDERS dict from player_bp.py, with extra
    hard-coded fallbacks for each game name so it ALWAYS finds
    a provider, even if game.code is NULL.
    """
    if not game:
        return None

    # For debugging
    code_attr = getattr(game, "code", None)
    name_attr = getattr(game, "name", None)
    log.info(
        "provider_for_game: game.id=%s name=%r code=%r providers=%r",
        getattr(game, "id", None),
        name_attr,
        code_attr,
        list(PROVIDERS.keys()),
    )

    # 1) Try by code
    code = (code_attr or "").strip()
    if code:
        up = code.upper()
        low = code.lower()
        if up in PROVIDERS:
            return PROVIDERS[up]
        if low in PROVIDERS:
            return PROVIDERS[low]

    # 2) Try by name patterns (robust)
    name = (name_attr or "").strip().lower()
    if not name:
        return None

    # JUWA
    if "juwa" in name:
        return PROVIDERS.get("JUWA") or PROVIDERS.get("juwa")

    # MILKYWAY
    if "milky" in name:
        return PROVIDERS.get("MILKYWAY") or PROVIDERS.get("milkyway")

    # YOLO
    if "yolo" in name:
        return PROVIDERS.get("YOLO") or PROVIDERS.get("yolo")

    # GAMEVAULT
    if "vault" in name:
        return (
            PROVIDERS.get("GAMEVAULT")
            or PROVIDERS.get("gamevault")
            or PROVIDERS.get("gv")
        )

    # GAMEROOM
    if "gameroom" in name or "game room" in name:
        return PROVIDERS.get("GAMEROOM") or PROVIDERS.get("gameroom")

    # ULTRAPANDA
    if "panda" in name:
        return PROVIDERS.get("ULTRAPANDA") or PROVIDERS.get("ultrapanda")

    # VBLINK
    if "vblink" in name or "blink" in name:
        return PROVIDERS.get("VBLINK") or PROVIDERS.get("vblink")

    # 3) Final fallback: exact matches against keys / codes / names
    for key, prov in PROVIDERS.items():
        try:
            if name == key.lower():
                return prov
            if hasattr(prov, "code") and prov.code and name == str(prov.code).lower():
                return prov
            if hasattr(prov, "name") and prov.name and name == str(prov.name).lower():
                return prov
        except Exception:
            continue

    return None


def _friendly_player_label(u: User) -> str:
    return (
        (getattr(u, "name", None) or "").strip()
        or (getattr(u, "username", None) or "").strip()
        or (getattr(u, "email", None) or "").strip()
        or f"Player #{getattr(u, 'id', '—')}"
    )


# =============================================================================
#                          SAFE COMMIT WRAPPER
# =============================================================================

def _safe_commit():
    """
    Commit with recovery for 'SSL SYSCALL error: EOF detected' and similar.

    If commit fails due to OperationalError, we rollback, dispose the engine,
    and retry once with a fresh DB connection.
    """
    try:
        db.session.commit()
    except OperationalError as e:
        log.warning("DB commit failed (OperationalError), retrying once: %s", e)
        db.session.rollback()
        try:
            db.engine.dispose()
        except Exception:
            pass
        # retry once
        db.session.commit()


# =============================================================================
#                           CELERY TASK
# =============================================================================

@shared_task(name="process_id_request")
def process_id_request(req_id: int):
    from app import create_app
    app = create_app()

    with app.app_context():
        req = db.session.get(GameAccountRequest, req_id)
    if not req:
        log.error("process_id_request: Request %s not found", req_id)
        return

    # initial progress (queued)
    update_progress(req.id, "Queued with automation engine…")

    if getattr(req, "status", "PENDING") not in ("PENDING", "IN_PROGRESS"):
        log.info(
            "process_id_request: Request %s already in status %s",
            req_id,
            req.status,
        )
        return

    # # ===== BLOCK MULTI-GAME REQUESTS (GLOBAL RULE) =====
#     other_active = GameAccountRequest.query.filter(
#         GameAccountRequest.user_id == req.user_id,
#         GameAccountRequest.id != req.id,
#         GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"]),
#     ).first()

#     if other_active:
#         req.status = "FAILED"
#         if hasattr(req, "last_error"):
#             req.last_error = "Another ID request is already in progress."
#         if hasattr(req, "updated_at"):
#             req.updated_at = datetime.utcnow()
#         _safe_commit()

#         # Professional but short notification
#         try:
#             notify(
#                 req.user_id,
#                 "You already have an active ID request. Please wait for it to finish before starting a new one.",
#             )
#         except Exception:
#             log.exception("Notify failed for %s", req.user_id)

#         update_progress(req.id, "Blocked — another ID request is already in progress.")
#         log.warning(
#             "User %s attempted multiple ID requests simultaneously. Blocking.",
#             req.user_id,
#         )
#         return

#     # ---- load game & user (THIS was missing on VPS) ----
    game = db.session.get(Game, req.game_id)
    user = db.session.get(User, req.user_id)

    if not game or not user:
        log.error("process_id_request: Request %s refers to missing Game/User", req.id)
        req.status = "FAILED"
        if hasattr(req, "last_error"):
            req.last_error = "Game or user not found"
        if hasattr(req, "updated_at"):
            req.updated_at = datetime.utcnow()
        _safe_commit()
        update_progress(req.id, "Request failed — game or player not found.")
        return

    provider = _provider_for_game(game)
    if not provider:
        msg = (
            f"No GameProvider configured for game '{game.name}' "
            f"(code={getattr(game, 'code', None)})"
        )
        log.warning("Request %s: %s", req.id, msg)
        req.status = "FAILED"
        if hasattr(req, "last_error"):
            req.last_error = msg
        if hasattr(req, "updated_at"):
            req.updated_at = datetime.utcnow()
        _safe_commit()
        update_progress(req.id, "Request failed — provider not configured for this game.")
        return

    pname = _friendly_player_label(user)
    log.info(
        "Processing request %s for %s → game %s (code=%s) via provider %s",
        req.id,
        pname,
        game.name,
        getattr(game, "code", None),
        getattr(
            provider,
            "code",
            getattr(provider, "name", type(provider).__name__),
        ),
    )

    update_progress(req.id, f"Connecting securely to {game.name}…")

    # mark IN_PROGRESS
    if hasattr(req, "status"):
        req.status = "IN_PROGRESS"
    if hasattr(req, "updated_at"):
        req.updated_at = datetime.utcnow()
    _safe_commit()

    # ---- PROVIDER-LEVEL LOCK (queue per provider) ----
    provider_code = (
        str(getattr(provider, "code", "") or "").upper()
        or str(getattr(provider, "name", "") or "").upper()
        or str(game.name or "").upper()
    )

    acquire_provider_lock(provider_code)
    try:
        # ---- retries and provider.create(...) ----
        result = {}
        last_err = ""
        max_attempts = 10

        for attempt in range(1, max_attempts + 1):
            try:
                # Friendly, NO attempt numbers to player
                if attempt == 1:
                    update_progress(req.id, f"Creating your {game.name} ID…")
                else:
                    update_progress(
                        req.id,
                        f"Re-connecting to {game.name} — still setting up your ID…",
                    )

                # Prefer GameVault-style create(user, req)
                try:
                    result = provider.create(user, req)  # type: ignore[arg-type]
                except TypeError:
                    # Juwa/Milkyway/YOLO/Gameroom/Vblink/UltraPanda style
                    result = provider.create()  # type: ignore[call-arg]
            except Exception as e:
                last_err = str(e)
                log.exception(
                    "provider.create() crashed for %s (attempt %s/%s)",
                    game.name,
                    attempt,
                    max_attempts,
                )
                result = {}

            acct = (
                result.get("account")
                or result.get("username")
                or result.get("user")
            )

            if acct:
                if attempt > 1:
                    log.info(
                        "Request %s for %s succeeded on retry %s/%s",
                        req.id,
                        game.name,
                        attempt,
                        max_attempts,
                    )
                break

            if attempt < max_attempts:
                log.warning(
                    "No account returned for %s on attempt %s/%s, retrying shortly...",
                    game.name,
                    attempt,
                    max_attempts,
                )
                # PLAYER TEXT: generic "still working" (no numbers)
                update_progress(
                    req.id,
                    f"{game.name} is taking a bit longer than usual — still working on your ID…",
                )
                time.sleep(15)
            else:
                log.error(
                    "All %s attempts failed for %s (request %s)",
                    max_attempts,
                    game.name,
                    req.id,
                )
    finally:
        release_provider_lock(provider_code)

    # ---- AFTER RETRIES ----
    acct = (
        result.get("account")
        or result.get("username")
        or result.get("user")
    )
    pwd = (
        result.get("password")
        or result.get("pass")
        or result.get("pwd")
        or acct
    )

    if not acct:
        err_text = (
            result.get("error")
            or last_err
            or f"{game.name} automation returned no account"
        )
        req.status = "FAILED"
        if hasattr(req, "last_error"):
            req.last_error = err_text
        if hasattr(req, "updated_at"):
            req.updated_at = datetime.utcnow()
        _safe_commit()

        update_progress(
            req.id,
            f"We couldn’t automatically create your {game.name} ID. Please contact support.",
        )

        try:
            notify(
                req.user_id,
                f"⚠️ We couldn’t automatically create your {game.name} ID. Please reach out to Support.",
            )
        except Exception:
            log.exception("Failed to notify user %s about failure", req.user_id)
        return

    username = str(acct).strip()
    password = str(pwd or username or "changeme123").strip()
    note = result.get("note") or f"Auto-provisioned via {game.name}"

    _save_or_update_game_account(
        user_id=req.user_id,
        game_id=req.game_id,
        username=username,
        password=password,
        note=note,
        request_id=req.id,
    )

    req.status = "APPROVED"
    if hasattr(req, "approved_at"):
        req.approved_at = datetime.utcnow()
    if hasattr(req, "updated_at"):
        req.updated_at = datetime.utcnow()
    _safe_commit()

    update_progress(
        req.id,
        f"Done — your {game.name} login is ready in My Logins.",
    )

    try:
        notify(
            req.user_id,
            f"🔓 Your {game.name} login is ready. Check My Logins.",
        )
    except Exception:
        log.exception("Failed to notify user %s about success", req.user_id)

    log.info(
        "Request %s for %s COMPLETED (user %s, username=%s)",
        req.id,
        game.name,
        req.user_id,
        username,
    )

