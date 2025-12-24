"""
Background worker for game ID auto-creation.

- Reads GameAccountRequest rows with status='PENDING'
- For each request, calls the correct provider via player_bp.PROVIDERS
  (same providers your code uses for Juwa / Milkyway / Vblink / Yolo / Gameroom / UltraPanda / GameVault / FireKirin / Orion Stars)
- On success: creates/updates GameAccount, marks request APPROVED
- On failure: marks request FAILED and optionally stores last_error
- Not used by Flask directly; run in a *separate terminal*:
    (venv) python id_request_worker.py
"""

import time
import logging
from datetime import datetime

from app import app  # your Flask app instance

from models import (
    db,
    User,
    Game,
    GameAccount,
    GameAccountRequest,
    Notification,
    notify,
)

# 👇 Link to your providers + helper from player_bp.py
from player_bp import PROVIDERS, _save_or_update_game_account

log = logging.getLogger("id_request_worker")

# -----------------------------------------------------------------------------
# GameVault integration (queue mode)
# -----------------------------------------------------------------------------

# Same helper you already use in player_bp.py
try:
    from gamevault_automation import gv_create_account
except Exception:
    gv_create_account = None


class GameVaultProvider:
    """
    Lightweight provider wrapper for GameVault used only by the worker.

    We design create(self, user, req) so we can use user.name / user.email.
    The worker will call provider.create(user, req) and automatically
    fall back to provider.create() for providers that don't need arguments.
    """
    code = "GAMEVAULT"
    name = "GameVault"

    def create(self, user: User, req: GameAccountRequest) -> dict:
        if not gv_create_account:
            return {"ok": False, "error": "GameVault automation not configured"}

        # Use the same pattern as your old instant mode:
        # gv_create_account(current_user.name or "", current_user.email or "")
        res = gv_create_account(user.name or "", user.email or "") or {}
        return res

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        # Implement later if you build a GameVault recharge bot
        return {"ok": False, "error": "GameVault recharge not implemented in worker"}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        # Implement later if you build a GameVault redeem bot
        return {"ok": False, "error": "GameVault redeem not implemented in worker"}


# Make sure PROVIDERS has a GameVault entry (upper-case key)
if "GAMEVAULT" not in PROVIDERS and "gamevault" not in PROVIDERS:
    PROVIDERS["GAMEVAULT"] = GameVaultProvider()
    log.info("Registered GameVaultProvider in PROVIDERS for worker")


# ---------------------------------------------------------------------------
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

    # FIREKIRIN
    if "firekirin" in name or "fire kirin" in name:
        return PROVIDERS.get("FIREKIRIN") or PROVIDERS.get("firekirin")

    # ORION STARS - NEW ADDITION
    if "orionstars" in name or "orion stars" in name or "orion-stars" in name or "orion" in name:
        return (
            PROVIDERS.get("ORIONSTARS") 
            or PROVIDERS.get("orionstars") 
            or PROVIDERS.get("ORION")
            or PROVIDERS.get("orion")
            or PROVIDERS.get("OS")
            or PROVIDERS.get("os")
        )

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


# ---------------------------------------------------------------------------
# Core worker logic
# ---------------------------------------------------------------------------

def _process_single_request(req: GameAccountRequest):
    """
    Process ONE GameAccountRequest row in the queue.

    This runs inside app.app_context() and inside a DB transaction.
    """
    game = db.session.get(Game, req.game_id)
    user = db.session.get(User, req.user_id)

    if not game or not user:
        log.error("Request %s refers to missing Game/User", req.id)
        req.status = "FAILED"
        if hasattr(req, "last_error"):
            req.last_error = "Game or user not found"
        if hasattr(req, "updated_at"):
            req.updated_at = datetime.utcnow()
        return

    provider = _provider_for_game(game)
    if not provider:
        msg = f"No GameProvider configured for game '{game.name}' (code={getattr(game, 'code', None)})"
        log.warning("Request %s: %s", req.id, msg)
        req.status = "FAILED"
        if hasattr(req, "last_error"):
            req.last_error = msg
        if hasattr(req, "updated_at"):
            req.updated_at = datetime.utcnow()
        return

    pname = _friendly_player_label(user)
    log.info(
        "Processing request %s for %s → game %s (code=%s)",
        req.id,
        pname,
        game.name,
        getattr(game, "code", None),
    )

    # ---- call provider.create() with retries (supports both signatures) ----
    result = {}
    last_err = ""
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            # Prefer create(user, req) (needed for GameVault)
            try:
                result = provider.create(user, req)  # type: ignore[arg-type]
            except TypeError:
                # Other providers in player_bp use create(self) with no args
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

        # Normalize username like player_bp does
        acct = (
            result.get("account")
            or result.get("username")
            or result.get("user")
        )

        if acct:
            # success → stop retrying
            if attempt > 1:
                log.info(
                    "Request %s for %s succeeded on retry %s/%s",
                    req.id,
                    game.name,
                    attempt,
                    max_attempts,
                )
            break

        # no account yet, decide whether to retry
        if attempt < max_attempts:
            log.warning(
                "No account returned for %s on attempt %s/%s, retrying after short delay...",
                game.name,
                attempt,
                max_attempts,
            )
            time.sleep(15)  # small delay between attempts
        else:
            log.error(
                "All %s attempts failed for %s (request %s)",
                max_attempts,
                game.name,
                req.id,
            )

    # Normalize password after the loop
    pwd = (
        result.get("password")
        or result.get("pass")
        or result.get("pwd")
        or acct
    )

    if not acct:
        # no account returned → failure
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

        try:
            notify(
                req.user_id,
                f"⚠️ We couldn't automatically create your {game.name} ID. Please try again later."
            )
        except Exception:
            log.exception("Failed to notify user %s about failure", req.user_id)

        log.error("Request %s for %s FAILED: %s", req.id, game.name, err_text)
        return

    # ---- success: save login (reuse helper from player_bp) ------------
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

    try:
        notify(
            req.user_id,
            f"🔓 Your {game.name} login is ready. Check My Logins."
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


def _worker_loop():
    """
    Infinite loop: always look for the next PENDING request and process it.
    Uses transactions + robust error handling so it does not crash.
    """
    with app.app_context():
        log.info("ID request worker started. Waiting for jobs...")

        while True:
            try:
                # Find the oldest PENDING request
                req = (
                    GameAccountRequest.query
                    .filter(GameAccountRequest.status == "PENDING")
                    .order_by(GameAccountRequest.created_at.asc())
                    .first()
                )

                if not req:
                    # nothing to do; sleep a bit
                    time.sleep(3)
                    continue

                log.info(
                    "Picked request %s (user=%s, game_id=%s)",
                    req.id,
                    req.user_id,
                    req.game_id,
                )

                # Mark as IN_PROGRESS (if you have that status column)
                if hasattr(req, "status"):
                    req.status = "IN_PROGRESS"
                if hasattr(req, "updated_at"):
                    req.updated_at = datetime.utcnow()
                db.session.commit()

                # Process it
                _process_single_request(req)
                db.session.commit()

                # Cooldown between accounts (helps avoid bans/rate limits)
                time.sleep(15)

            except Exception:
                # Any exception here should never crash the worker
                log.exception("Fatal error while processing queue item")
                db.session.rollback()
                # small backoff to avoid hot loop
                time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    _worker_loop()