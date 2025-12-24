"""
id_requests.py

Celery worker for processing GameAccountRequest in the background.
Uses the same provider interface as player_bp.PROVIDERS so many
players can request IDs at once without blocking the web app.
"""

import logging
import time

from celery import Celery

# Try both names, depending on how celery_app is written
from celery_app import celery as celery_app

from app import app
from models import (
    db,
    Game,
    GameAccount,
    GameAccountRequest,
    User,
    notify,
)

# Reuse provider interface + helpers from player_bp (no UI stuff)
from player_bp import (
    PROVIDERS,
    _save_or_update_game_account,
    _approve_request,
)

# Optional GameVault helper if available
try:
    from gamevault_automation import gv_create_account
except Exception:
    gv_create_account = None

log = logging.getLogger("id_requests")

celery: Celery = celery_app

# ===== NEW: retry config =====
MAX_ATTEMPTS = 10          # how many times to try
RETRY_DELAY_SECONDS = 25   # seconds between attempts


def _set_request_status(req: GameAccountRequest, status: str, error: str | None = None):
    """Update status + last_error safely."""
    req.status = status
    if hasattr(req, "updated_at"):
        req.updated_at = db.func.now()
    if error and hasattr(req, "last_error"):
        req.last_error = error[:500]  # avoid huge blobs
    db.session.commit()


def _provider_for_game(game: Game):
    """
    Find a provider for a Game row.

    Preference:
    1) Use game.code with PROVIDERS (ULTRAPANDA, JUWA, MILKYWAY, YOLO, GAMEROOM, VBLINK, ORIONSTAR).
    2) Fallback by name for old rows (handles spaces like "Ultra Panda" vs "UltraPanda").
    """
    if not game:
        return None

    # 1) Try explicit code field if your model has it (safe even if it doesn't)
    if getattr(game, "code", None):
        code = (game.code or "").strip().upper()
        if code in PROVIDERS:
            return PROVIDERS[code]

    # 2) Name-based matching
    gname_raw = (game.name or "").strip()
    gname = gname_raw.lower()
    gname_compact = gname.replace(" ", "")

    # GameVault is still handled via the special helper
    if "gamevault" in gname_compact or "vault" in gname_compact:
        return "GAMEVAULT"

    # 🔹 Explicit Ultra Panda fallback (because of space vs no-space)
    if "panda" in gname:
        prov = PROVIDERS.get("ULTRAPANDA") or PROVIDERS.get("ultrapanda")
        if prov:
            return prov

    # 🔹 Explicit Orion Star fallback
    if "orion" in gname and "star" in gname:
        # Try different possible codes
        prov = PROVIDERS.get("ORIONSTAR") or PROVIDERS.get("ORION_STAR") or PROVIDERS.get("orionstar")
        if prov:
            return prov

    # 3) Generic provider name matching (ignore spaces)
    for code, p in PROVIDERS.items():
        pname_raw = getattr(p, "name", "") or str(code)
        pname = pname_raw.strip().lower()
        pname_compact = pname.replace(" ", "")

        # Either exact match or "UltraPanda" inside "ultrapanda" etc.
        if pname_compact and (
            gname_compact == pname_compact or pname_compact in gname_compact
        ):
            return p

    # 4) Special case for "Orion" alone
    if "orion" in gname_compact:
        prov = PROVIDERS.get("ORIONSTAR") or PROVIDERS.get("ORION_STAR")
        if prov:
            return prov

    return None


@celery.task(name="process_id_request", queue="id_requests")
def process_id_request(req_id: int):
    """
    Background task: create a game login for a GameAccountRequest row.

    - Only processes rows with status == "PENDING".
    - Uses provider.create() or GameVault helper.
    - On success: saves GameAccount + marks APPROVED.
    - On failure: retries up to MAX_ATTEMPTS, then marks FAILED and stores last_error.
    """
    with app.app_context():
        log.info("process_id_request starting: req_id=%s", req_id)

        req = db.session.get(GameAccountRequest, req_id)
        if not req:
            log.error("process_id_request: Request %s not found", req_id)
            return

        # Only work on fresh requests
        if (req.status or "").upper() != "PENDING":
            log.info(
                "process_id_request: Request %s already in status %s",
                req.id,
                req.status,
            )
            return

        game = db.session.get(Game, req.game_id) if req.game_id else None
        user = db.session.get(User, req.user_id) if req.user_id else None

        if not game or not user:
            err = "Missing game or user"
            log.error("process_id_request: %s for req_id=%s", err, req_id)
            _set_request_status(req, "FAILED", err)
            return

        # Mark as PROCESSING so we know worker picked it up
        req.status = "PROCESSING"
        if hasattr(req, "updated_at"):
            req.updated_at = db.func.now()
        db.session.commit()

        provider = _provider_for_game(game)
        gname = (game.name or "").strip()
        gname_lower = gname.lower()

        # ===== NEW: retry loop around existing logic =====
        last_error_msg = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            raw_res = {}
            error_text = ""
            log.info(
                "process_id_request: req_id=%s attempt %s/%s",
                req.id,
                attempt,
                MAX_ATTEMPTS,
            )

            try:
                # ---- Special case: GameVault via direct helper ----------------
                if provider == "GAMEVAULT":
                    if not gv_create_account:
                        error_text = "GameVault automation not configured"
                        raise RuntimeError(error_text)

                    raw_res = gv_create_account(user.name or "", user.email or "") or {}
                    if raw_res.get("ok"):
                        acct = (
                            raw_res.get("account")
                            or raw_res.get("username")
                            or raw_res.get("created")
                        )
                        pwd = raw_res.get("password") or acct or "changeme123"
                        note = (
                            raw_res.get("note")
                            or "Auto-provisioned via GameVault (Celery worker)"
                        )
                        _save_or_update_game_account(
                            user_id=user.id,
                            game_id=game.id,
                            username=acct,
                            password=pwd,
                            note=note,
                            request_id=req.id,
                        )
                        _approve_request(req)
                        db.session.commit()
                        notify(
                            user.id,
                            f"🔐 Your {gname} login is ready. Check My Logins.",
                        )
                        log.info(
                            "process_id_request: SUCCESS for req_id=%s (GameVault)",
                            req.id,
                        )
                        return

                    error_text = (
                        raw_res.get("error") or "GameVault auto-provision failed"
                    )
                    raise RuntimeError(error_text)

                # ---- Normal providers (Juwa, Milkyway, UltraPanda, Vblink, YOLO, Gameroom, OrionStar)
                if not provider or provider == "GAMEVAULT":
                    error_text = f"No automation provider configured for game {gname}"
                    raise RuntimeError(error_text)

                raw_res = provider.create() or {}
                log.info(
                    "process_id_request: provider=%s raw_res=%s",
                    getattr(provider, "name", "unknown"),
                    raw_res,
                )

                if raw_res.get("ok") or raw_res.get("account") or raw_res.get("username"):
                    username = (
                        raw_res.get("account")
                        or raw_res.get("username")
                        or f"user_{int(time.time())}"
                    )
                    password = raw_res.get("password") or username or "changeme123"
                    note = (
                        raw_res.get("note")
                        or f"Auto-provisioned via {getattr(provider, 'name', gname)} (Celery worker)"
                    )

                    _save_or_update_game_account(
                        user_id=user.id,
                        game_id=game.id,
                        username=username.strip(),
                        password=password.strip(),
                        note=note,
                        request_id=req.id,
                    )
                    _approve_request(req)
                    db.session.commit()

                    notify(
                        user.id,
                        f"🔐 Your {gname} login is ready. Check My Logins.",
                    )
                    log.info("process_id_request: SUCCESS for req_id=%s", req.id)
                    return

                error_text = (
                    raw_res.get("error")
                    or f"{getattr(provider, 'name', gname)} automation failed"
                )
                raise RuntimeError(error_text)

            except Exception as e:
                # Build message (same style as before)
                msg = f"{type(e).__name__}: {e}"
                if error_text and error_text not in msg:
                    msg = f"{error_text} | {msg}"

                last_error_msg = msg
                log.exception(
                    "process_id_request: exception for req_id=%s on attempt %s/%s: %s",
                    req.id,
                    attempt,
                    MAX_ATTEMPTS,
                    msg,
                )

                # If we still have attempts left, just wait and try again
                if attempt < MAX_ATTEMPTS:
                    log.warning(
                        "process_id_request: will retry req_id=%s in %s seconds",
                        req.id,
                        RETRY_DELAY_SECONDS,
                    )
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue

                # ===== LAST ATTEMPT → keep your old failure behavior =====
                              
                _set_request_status(req, "FAILED", msg)

                # Let the player know something went wrong (soft + technical)
                try:
                    notify(
                        user.id,
                        (
                            f"⚠️ {gname} server is currently under maintenance or not responding.\n\n"
                            f"Your game ID couldn't be created automatically. "
                            f"Please try again later."
                        ),
                    )
                except Exception:
                    pass

                return  # stop after final failure


def enqueue_game_account_request(req_id: int):
    """
    Helper used from player_bp to push a job into the Celery queue.
    """
    celery_kwargs = {"queue": "id_requests"}
    process_id_request.apply_async(args=[req_id], **celery_kwargs)