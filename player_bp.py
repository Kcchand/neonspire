# player_bp.py
# -----------------------------------------------------------------------------
# Player flows (request ID, deposits/withdraws, dashboards, referrals)
# Includes a pluggable Provider interface for game automations (Juwa, Milkyway,
# UltraPanda, GameVault). To add a new game later, create a Provider subclass
# and register it in the PROVIDERS dict near the middle of this file.
# -----------------------------------------------------------------------------

from datetime import datetime, timedelta
import re
import random
import os
import time
import logging
from typing import Optional

from werkzeug.utils import secure_filename
from flask import (
    Blueprint, render_template, render_template_string, request,
    redirect, url_for, flash, abort, current_app, jsonify
)
from flask_login import login_required, current_user
from sqlalchemy import text

from models import (
    db,
    User,
    Game,
    PlayerBalance,
    GameAccount,
    GameAccountRequest,
    DepositRequest,
    WithdrawRequest,
    PaymentSettings,
    Notification,
    ReferralCode,
)
from models import notify
from models import DepositRequest as Deposit  # back-compat alias

# =============================================================================
# Optional dependencies (guard every import)
# =============================================================================

# JUWA API
try:
    from automation.juwa_api import (
        create_sync as juwa_create_sync,
        recharge_sync as juwa_recharge_sync,
        redeem_sync as juwa_redeem_sync,
    )
except Exception:
    juwa_create_sync = juwa_recharge_sync = juwa_redeem_sync = None

# GameVault helper (you referenced before)
try:
    from gamevault_automation import gv_create_account
except Exception:
    gv_create_account = None

# UltraPanda API
try:
    from automation.ultrapanda_api import (
        up_create, up_recharge, up_redeem, up_supported, start_ultrapanda_autoprovision
    )
except Exception:
    def up_supported() -> bool:  # graceful fallback
        return False
    up_create = up_recharge = up_redeem = None
    start_ultrapanda_autoprovision = None

# Milkyway Playwright UI Bot (your working automation)
try:
    from automation.milkyway_ui_bot import mw_create_player_auto, mw_recharge, mw_redeem
except Exception:
    mw_create_player_auto = mw_recharge = mw_redeem = None

# Optional Cash App invoice creator
try:
    from payments.safepay import create_cashapp_invoice  # noqa: F401
except Exception:
    create_cashapp_invoice = None

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = logging.getLogger("player.request")

# -----------------------------------------------------------------------------
# Blueprints
# -----------------------------------------------------------------------------
player_bp = Blueprint("playerbp", __name__, url_prefix="/player")
short_bp  = Blueprint("short_bp", __name__, url_prefix="")

# =============================================================================
#                    SHARED UTILITIES / HELPERS
# =============================================================================

# ---- tiny KV fallback (for promo/trending text) ------------------------------
def _ensure_kv():
    try:
        bind = db.session.get_bind()
        dialect = bind.dialect.name
        if dialect in ("postgresql", "postgres"):
            db.session.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT)"))
        elif dialect in ("mysql", "mariadb"):
            db.session.execute(text(
                "CREATE TABLE IF NOT EXISTS kv_store (`key` VARCHAR(191) PRIMARY KEY, `value` TEXT) "
                "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            ))
        else:
            db.session.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT)"))
        db.session.commit()
    except Exception:
        db.session.rollback()

def _kv_get(k: str) -> Optional[str]:
    _ensure_kv()
    try:
        row = db.session.execute(text("SELECT value FROM kv_store WHERE key=:k"), {"k": k}).fetchone()
        return row[0] if row else None
    except Exception:
        db.session.rollback()
        return None

def _kv_first(*keys, default: Optional[str] = None) -> Optional[str]:
    for k in keys:
        v = _kv_get(k)
        if v not in (None, ""):
            return v
    return default

# ---- uploads ----------------------------------------------------------------
_ALLOWED_PROOF_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

def _uploads_dir() -> str:
    updir = os.path.join(current_app.static_folder, "uploads")
    os.makedirs(updir, exist_ok=True)
    return updir

def _save_image(file_storage, prefix: str) -> Optional[str]:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None
    filename = secure_filename(file_storage.filename or "")
    if not filename:
        return None
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_PROOF_EXTS:
        flash("Only PNG, JPG, WEBP or GIF images are allowed for proof.", "error")
        return None
    ts = int(time.time())
    uid = current_user.id if current_user and current_user.is_authenticated else "anon"
    new_name = f"{prefix}_{uid}_{ts}{ext}"
    dest = os.path.join(_uploads_dir(), new_name)
    try:
        file_storage.save(dest)
    except Exception:
        flash("Could not save your screenshot. Please try a different image.", "error")
        return None
    return url_for("static", filename=f"uploads/{new_name}")

# ---- misc helpers ------------------------------------------------------------
def _ensure_wallet(user_id: int) -> PlayerBalance:
    wallet = PlayerBalance.query.filter_by(user_id=user_id).first()
    if not wallet:
        wallet = PlayerBalance(user_id=user_id, balance=0)
        db.session.add(wallet)
        db.session.commit()
    return wallet

def _player_like() -> bool:
    return current_user.is_authenticated and current_user.role in ("PLAYER", "ADMIN")

def _get_settings():
    s = db.session.get(PaymentSettings, 1)
    if not s:
        s = PaymentSettings(id=1, min_redeem=0, max_redeem=0)
        db.session.add(s)
        db.session.commit()
    return s

def _first_attr(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n):
            val = getattr(obj, n)
            if val not in (None, ""):
                return val
    return default

def _template_exists(name: str) -> bool:
    try:
        render_template(name)
        return True
    except Exception:
        return False

def _acc_username(acc: Optional[GameAccount]) -> str:
    if not acc:
        return ""
    return (_first_attr(acc, "account_username", "username", "login", "user", default="") or "")

def _ensure_login_request_if_missing(user_id: int, game_id: int) -> tuple[bool, str]:
    """Used on deposit/withdraw to ensure there's at least a pending account request."""
    if not game_id:
        return (False, "")
    existing = GameAccount.query.filter_by(user_id=user_id, game_id=game_id).first()
    if existing:
        return (True, f"login={_acc_username(existing) or '—'}")
    open_req = (
        GameAccountRequest.query.filter_by(user_id=user_id, game_id=game_id)
        .filter(GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"]))
        .first()
    )
    if open_req:
        return (False, "no login yet (request already open)")
    new_req = GameAccountRequest(user_id=user_id, game_id=game_id, status="PENDING", created_at=datetime.utcnow())
    db.session.add(new_req)
    db.session.commit()
    return (False, "no login yet (auto-opened request)")

def _player_label(u: User) -> str:
    return (
        (getattr(u, "name", None) or "").strip()
        or (getattr(u, "username", None) or "").strip()
        or (getattr(u, "email", None) or "").strip()
        or f"Player #{getattr(u, 'id', '—')}"
    )

def _save_or_update_game_account(
    user_id: int, game_id: int, username: str, password: str, note: str, request_id: Optional[int] = None
) -> GameAccount:
    """Write username/password/note to GameAccount regardless of column names."""
    acc = GameAccount.query.filter_by(user_id=user_id, game_id=game_id).first()
    if not acc:
        acc = GameAccount(user_id=user_id, game_id=game_id)
        if hasattr(acc, "created_at") and not getattr(acc, "created_at", None):
            acc.created_at = datetime.utcnow()
        db.session.add(acc)

    # username
    for name in ("account_username", "username", "login", "user"):
        if hasattr(acc, name):
            setattr(acc, name, username)
            break
    # password
    for name in ("account_password", "password", "passcode", "pin"):
        if hasattr(acc, name):
            setattr(acc, name, password)
            break
    # note
    for name in ("extra", "note", "notes", "remark"):
        if hasattr(acc, name):
            setattr(acc, name, note)
            break
    # optional link to request row
    if request_id and hasattr(acc, "request_id"):
        acc.request_id = request_id

    db.session.flush()
    return acc

def _approve_request(req: GameAccountRequest):
    req.status = "APPROVED"
    if hasattr(req, "approved_at"): req.approved_at = datetime.utcnow()
    if hasattr(req, "updated_at"):  req.updated_at  = datetime.utcnow()

# =============================================================================
#                       GAME PROVIDER INTERFACE
# =============================================================================

class GameProvider:
    """Minimal interface every provider implements. Add new providers easily."""
    code: str = ""
    name: str = ""

    def ensure_game(self) -> Game:
        """Find or create the Game row matching this provider."""
        g = Game.query.filter_by(code=self.code).first()
        if not g:
            g = Game.query.filter(Game.name.ilike(f"%{self.name}%")).first()
        if not g:
            g = Game(name=self.name, code=self.code, is_active=True)
            db.session.add(g)
            db.session.commit()
        return g

    # Implement in subclasses:
    def create(self) -> dict: raise NotImplementedError
    def recharge(self, account: str, amount: float, note: str = "") -> dict: raise NotImplementedError
    def redeem(self, account: str, amount: float, note: str = "") -> dict: raise NotImplementedError

# ---- JUWA Provider -----------------------------------------------------------
class JuwaProvider(GameProvider):
    code = "JUWA"
    name = "Juwa"

    def create(self) -> dict:
        if not juwa_create_sync:
            return {"ok": False, "error": "Juwa automation not configured"}
        try:
            return juwa_create_sync() or {}
        except TypeError:
            return juwa_create_sync(None, None) or {}

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not juwa_recharge_sync:
            return {"ok": False, "error": "Juwa recharge not configured"}
        return juwa_recharge_sync(account, amount) or {}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        if not juwa_redeem_sync:
            return {"ok": False, "error": "Juwa redeem not configured"}
        return juwa_redeem_sync(account, amount) or {}

# ---- Milkyway Provider (Playwright UI Bot) ----------------------------------
class MilkywayProvider(GameProvider):
    code = "MILKYWAY"
    name = "Milkyway"

    def create(self) -> dict:
        if not mw_create_player_auto:
            return {"ok": False, "error": "Milkyway automation not configured"}
        res = mw_create_player_auto() or {}
        # Normalize shape from your bot
        if res.get("created") and res.get("account"):
            return {"ok": True, "account": res["account"], "password": res.get("password") or res.get("account")}
        return res

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not mw_recharge:
            return {"ok": False, "error": "Milkyway recharge not configured"}
        mw_recharge(account, amount, note=note)
        return {"ok": True}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        if not mw_redeem:
            return {"ok": False, "error": "Milkyway redeem not configured"}
        mw_redeem(account, amount, note=note)
        return {"ok": True}

# ---- UltraPanda Provider -----------------------------------------------------
class UltraPandaProvider(GameProvider):
    code = "ULTRAPANDA"
    name = "UltraPanda"

    def create(self) -> dict:
        if not up_create:
            return {"ok": False, "error": "UltraPanda disabled"}
        return up_create(None, None) or {}

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not up_recharge:
            return {"ok": False, "error": "UltraPanda disabled"}
        return up_recharge(account, amount, note) or {}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        if not up_redeem:
            return {"ok": False, "error": "UltraPanda disabled"}
        return up_redeem(account, amount, note) or {}

# ---- Register providers here (easy to extend) --------------------------------
PROVIDERS: dict[str, GameProvider] = {
    "JUWA": JuwaProvider(),
    "MILKYWAY": MilkywayProvider(),
    "ULTRAPANDA": UltraPandaProvider(),
    # "GAMEVAULT": GameVaultProvider(),  # Example if you add one later
}

def _provider_for_slug(slug: str) -> Optional[GameProvider]:
    """Map route slug to provider (accepts code or name, case-insensitive)."""
    s = (slug or "").strip().lower()
    for code, p in PROVIDERS.items():
        if s in (code.lower(), p.name.lower()):
            return p
    return None

# =============================================================================
#          UI RENDER HELPERS / CONSTANTS (dash, deposit/withdraw)
# =============================================================================
PROMO1_ALIASES = ("promo_line1", "news_line1", "ticker_line1", "headline1", "news1")
PROMO2_ALIASES = ("promo_line2", "news_line2", "ticker_line2", "headline2", "news2")
TREND_ALIASES  = ("trending_game_ids", "trending_ids", "trending_csv", "trending")

def _render_deposit_step1(preselect_game_id: Optional[int]):
    if not _player_like():
        return abort(403)
    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    return render_template(
        "player_deposit_step1.html",
        games=games,
        preselect_game_id=preselect_game_id,
        page_title="Deposit • Step 1"
    )

def _render_deposit_step2(game_id: Optional[int], method: str, amount: int):
    if not _player_like():
        return abort(403)
    if not amount or amount <= 0:
        flash("Start over and enter a valid amount.", "error")
        return redirect(url_for("playerbp.deposit_step1"))
    method = (method or "CRYPTO").upper()
    if method not in ("CRYPTO", "CHIME", "CASHAPP"):
        flash("Invalid payment method.", "error")
        return redirect(url_for("playerbp.deposit_step1"))
    game = db.session.get(Game, game_id) if game_id else None
    settings = _get_settings()
    return render_template(
        "player_deposit_step2.html",
        amount=amount, method=method, game=game, settings=settings,
        page_title="Deposit • Step 2"
    )

def _render_withdraw(game_id: Optional[int]):
    if not _player_like():
        return abort(403)
    settings = _get_settings()
    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    return render_template(
        "player_withdraw.html",
        settings=settings, games=games, preselect_game_id=game_id,
        page_title="Withdraw • NeonSpire Casino"
    )

# =============================================================================
#                    GENERIC GAME ROUTES (provider-based)
# =============================================================================

@player_bp.post("/<string:game_slug>/request-id", endpoint="generic_request_id")
@login_required
def generic_request_id(game_slug: str):
    """Instant account creation for any registered provider."""
    provider = _provider_for_slug(game_slug)
    if not provider:
        flash("Game automation not configured.", "error")
        return redirect(url_for("playerbp.mylogin"))

    game = provider.ensure_game()

    # Prevent duplicates
    if GameAccount.query.filter_by(user_id=current_user.id, game_id=game.id).first():
        flash(f"You already have a {game.name} login. Check My Logins.", "error")
        return redirect(url_for("playerbp.mylogin"))

    # Track request row
    req = GameAccountRequest(
        user_id=current_user.id, game_id=game.id,
        status="PENDING", created_at=datetime.utcnow()
    )
    db.session.add(req); db.session.flush()

    # Try auto-provision
    auto_err = ""
    try:
        res = provider.create() or {}
        if res.get("ok") or res.get("account") or res.get("username"):
            username = (res.get("account") or res.get("username") or f"user_{int(time.time())}").strip()
            password = (res.get("password") or username or "changeme123").strip()
            note = res.get("note") or f"Auto-provisioned via {provider.name}"
            _save_or_update_game_account(current_user.id, game.id, username, password, note, req.id)
            _approve_request(req)
            db.session.commit()
            notify(current_user.id, f"🔐 Your {game.name} login is ready. Check My Logins.")
            flash(f"{game.name} ID created 🎉", "success")
            return redirect(url_for("playerbp.mylogin"))
        auto_err = res.get("error") or f"{provider.name} automation failed"
    except Exception as e:
        auto_err = str(e)
        log.exception("%s instant create failed", provider.name)

    # Fallback: keep request queued for staff
    req.status = "PENDING"
    if hasattr(req, "updated_at"): req.updated_at = datetime.utcnow()
    db.session.commit()
    notify(current_user.id, f"🕓 Your {provider.name} access request is being processed.")
    flash("Request submitted. You’ll be notified once it’s ready.", "success")

    pname = _player_label(current_user)
    for staff in User.query.filter(User.role.in_(("EMPLOYEE", "ADMIN"))).all():
        notify(staff.id, f"New {provider.name} access request by {pname} | auto note: {auto_err}")
    return redirect(url_for("playerbp.mylogin"))

@player_bp.post("/<string:game_slug>/recharge", endpoint="generic_recharge")
@login_required
def generic_recharge(game_slug: str):
    provider = _provider_for_slug(game_slug)
    if not provider:
        flash("Game automation not configured.", "error")
        return redirect(url_for("playerbp.mylogin"))

    account = (request.form.get("account") or "").strip()
    amount = request.form.get("amount", type=float)
    if not account or not amount or amount <= 0:
        flash("Enter a valid account and amount.", "error")
        return redirect(url_for("playerbp.mylogin"))

    try:
        res = provider.recharge(account, amount, note=f"user:{current_user.id}") or {}
        if res.get("ok", True):
            flash(f"{provider.name} recharge completed.", "success")
        else:
            flash(f"{provider.name} recharge failed: {res}", "danger")
    except Exception as e:
        flash(f"{provider.name} recharge failed: {e}", "danger")

    return redirect(url_for("playerbp.mylogin"))

@player_bp.post("/<string:game_slug>/redeem", endpoint="generic_redeem")
@login_required
def generic_redeem(game_slug: str):
    provider = _provider_for_slug(game_slug)
    if not provider:
        flash("Game automation not configured.", "error")
        return redirect(url_for("playerbp.mylogin"))

    account = (request.form.get("account") or "").strip()
    amount = request.form.get("amount", type=float)
    if not account or not amount or amount <= 0:
        flash("Enter a valid account and amount.", "error")
        return redirect(url_for("playerbp.mylogin"))

    try:
        res = provider.redeem(account, amount, note=f"user:{current_user.id}") or {}
        if res.get("ok", True):
            flash(f"{provider.name} redeem completed.", "success")
        else:
            flash(f"{provider.name} redeem failed: {res}", "danger")
    except Exception as e:
        flash(f"{provider.name} redeem failed: {e}", "danger")

    return redirect(url_for("playerbp.mylogin"))

# ---- Backward-compatible named endpoints (templates can keep old paths) ------
@player_bp.post("/juwa/request-id", endpoint="juwa_request_id")
@login_required
def juwa_request_id_compat():
    return generic_request_id.__wrapped__("juwa")

@player_bp.post("/juwa/recharge", endpoint="juwa_recharge")
@login_required
def juwa_recharge_compat():
    return generic_recharge.__wrapped__("juwa")

@player_bp.post("/juwa/redeem", endpoint="juwa_redeem")
@login_required
def juwa_redeem_compat():
    return generic_redeem.__wrapped__("juwa")

@player_bp.post("/milkyway/request-id", endpoint="milkyway_request_id")
@login_required
def milkyway_request_id_alias():
    return generic_request_id.__wrapped__("milkyway")

@player_bp.post("/milkyway/recharge", endpoint="milkyway_recharge")
@login_required
def milkyway_recharge_alias():
    return generic_recharge.__wrapped__("milkyway")

@player_bp.post("/milkyway/redeem", endpoint="milkyway_redeem")
@login_required
def milkyway_redeem_alias():
    return generic_redeem.__wrapped__("milkyway")

# =============================================================================
#                              DASHBOARD / PAGES
# =============================================================================

@player_bp.get("/dashboard")
@login_required
def player_dashboard():
    if not _player_like():
        return abort(403)

    settings = _get_settings()
    wallet = _ensure_wallet(current_user.id)
    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()

    # Accounts grouped by game
    my_accounts = GameAccount.query.filter_by(user_id=current_user.id).all()
    accounts_by_game = {}
    for acc in my_accounts:
        accounts_by_game.setdefault(getattr(acc, "game_id", None), []).append(acc)

    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(
        Notification.created_at.desc()
    ).limit(10).all()

    promo_line1 = _first_attr(settings, *PROMO1_ALIASES, default=None) or _kv_first(*PROMO1_ALIASES, default="")
    promo_line2 = _first_attr(settings, *PROMO2_ALIASES, default=None) or _kv_first(*PROMO2_ALIASES, default="")
    bonus_percent = getattr(settings, "bonus_percent", None)
    if bonus_percent in (None, ""):
        bp = _kv_first("bonus_percent")
        bonus_percent = int(bp) if (bp and str(bp).isdigit()) else 0

    raw_csv = _first_attr(settings, *TREND_ALIASES, default=None) or (_kv_first(*TREND_ALIASES, default="") or "")
    trending_ids: list[int] = []
    for token in str(raw_csv).split(","):
        t = token.strip()
        if t.isdigit():
            gid = int(t)
            if gid not in trending_ids:
                trending_ids.append(gid)

    trending_games = []
    if trending_ids:
        found = Game.query.filter(Game.id.in_(trending_ids)).all()
        by_id = {g.id: g for g in found}
        for gid in trending_ids:
            if gid in by_id:
                trending_games.append(by_id[gid])

    return render_template(
        "dashboard_player.html",
        page_title="Player Dashboard • NeonSpire Casino",
        wallet=wallet,
        games=games,
        accounts_by_game=accounts_by_game,
        notifications=notifications,
        trending_games=trending_games,
        promo_line1=promo_line1,
        promo_line2=promo_line2,
        bonus_percent=bonus_percent,
    )

# =============================================================================
#                         ULTRAPANDA REST API (optional)
# =============================================================================

def _ensure_ultrapanda_game() -> Game:
    g = Game.query.filter_by(code="ULTRAPANDA").first()
    if not g:
        g = Game(name="UltraPanda", code="ULTRAPANDA", is_active=True)
        db.session.add(g)
        db.session.commit()
    return g

@player_bp.post("/api/ultrapanda/create")
@login_required
def api_ultrapanda_create():
    if not up_supported():
        return jsonify({"ok": False, "error": "UltraPanda disabled"}), 400

    payload = request.get_json(silent=True) or {}
    want_user = payload.get("username") or None
    want_pwd  = payload.get("password") or None

    res = up_create(want_user, want_pwd)
    if not res.get("ok"):
        return jsonify(res), 400

    game = _ensure_ultrapanda_game()
    acct_name = res.get("account") or want_user
    if acct_name:
        ga = GameAccount.query.filter_by(
            user_id=current_user.id, game_id=game.id, username=acct_name
        ).first()
        if not ga:
            ga = GameAccount(user_id=current_user.id, game_id=game.id, username=acct_name)
            if want_pwd:
                try:
                    setattr(ga, "password", want_pwd)
                except Exception:
                    pass
            db.session.add(ga)
            db.session.commit()

    return jsonify({"ok": True, "account": acct_name, "details": res})

@player_bp.post("/api/ultrapanda/recharge")
@login_required
def api_ultrapanda_recharge():
    if not up_supported():
        return jsonify({"ok": False, "error": "UltraPanda disabled"}), 400

    payload = request.get_json(silent=True) or {}
    account = (payload.get("account") or "").strip()
    amount  = float(payload.get("amount") or 0)
    remark  = payload.get("remark") or f"user:{current_user.id}"

    if not account or amount <= 0:
        return jsonify({"ok": False, "error": "account and positive amount required"}), 400

    res = up_recharge(account, amount, remark)
    if not res.get("ok"):
        return jsonify(res), 400

    # Optional: record a deposit history row
    try:
        game = _ensure_ultrapanda_game()
        dep = DepositRequest(
            user_id=current_user.id,
            game_id=game.id,
            amount=amount,
            status="LOADED",
            provider="ULTRAPANDA",
            backend_url=os.getenv("ULTRAPANDA_BASE_URL", "")
        )
        db.session.add(dep)
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify({"ok": True, "result": res})

@player_bp.post("/api/ultrapanda/redeem")
@login_required
def api_ultrapanda_redeem():
    if not up_supported():
        return jsonify({"ok": False, "error": "UltraPanda disabled"}), 400

    payload = request.get_json(silent=True) or {}
    account = (payload.get("account") or "").strip()
    amount  = float(payload.get("amount") or 0)
    remark  = payload.get("remark") or f"user:{current_user.id}"

    if not account or amount <= 0:
        return jsonify({"ok": False, "error": "account and positive amount required"}), 400

    res = up_redeem(account, amount, remark)
    if not res.get("ok"):
        return jsonify(res), 400

    return jsonify({"ok": True, "result": res})

@player_bp.post("/api/ultrapanda/request")
@login_required
def api_ultrapanda_request():
    game = _ensure_ultrapanda_game()

    # Ensure a single pending request
    req = GameAccountRequest.query.filter_by(
        user_id=current_user.id, game_id=game.id, status="PENDING"
    ).first()
    if not req:
        req = GameAccountRequest(
            user_id=current_user.id, game_id=game.id,
            status="PENDING", created_at=datetime.utcnow()
        )
        db.session.add(req); db.session.flush()

    # If external autoprovision worker exists, enqueue
    try:
        if up_supported() and start_ultrapanda_autoprovision:
            notify(current_user.id, "🕓 Your UltraPanda request is being processed.")
            start_ultrapanda_autoprovision(
                user_id=current_user.id,
                on_notify=lambda uid, t: notify(uid, t),
                on_success=lambda uid, r: notify(uid, "🔓 Your UltraPanda login is ready."),
                on_error=lambda uid, m: notify(uid, f"⚠️ UltraPanda request failed: {m}")
            )
            return jsonify({"ok": True, "queued": True}), 202
    except Exception:
        pass

    # Local instant fallback
    username = f"ultrapanda_{current_user.id}_{int(time.time())}"
    password = "changeme123"
    note = "Auto-provisioned via UltraPanda (local)"
    try:
        if up_supported() and up_create:
            r = up_create(None, None) or {}
            if r.get("ok"):
                username = (r.get("account") or r.get("username") or username).strip()
                password = (r.get("password") or password).strip()
                note = "Auto-provisioned via UltraPanda"
    except Exception:
        pass

    _save_or_update_game_account(current_user.id, game.id, username, password, note, req.id)
    _approve_request(req); db.session.commit()
    notify(current_user.id, "🔓 Your UltraPanda login is ready. Check My Logins.")
    return jsonify({"ok": True, "instant": True, "account": username}), 200

# =============================================================================
#                   REQUEST ACCOUNT (generic/manual by game_id)
# =============================================================================

@player_bp.post("/game/<int:game_id>/request-account", endpoint="request_game_account")
@login_required
def request_game_account(game_id: int):
    """Legacy path by numeric game_id; still attempts instant automation when possible."""
    if not _player_like():
        return abort(403)

    game = db.session.get(Game, game_id)
    if not game or not game.is_active:
        flash("Game not available.", "error")
        return redirect(url_for("index"))

    if GameAccount.query.filter_by(user_id=current_user.id, game_id=game_id).first():
        flash("⚠️ You already have this account.", "error")
        notify(current_user.id, "⚠️ You already have this game account. Check My Logins.")
        return redirect(url_for("playerbp.mylogin", noinfo=1))

    req = GameAccountRequest(user_id=current_user.id, game_id=game_id, status="PENDING")
    db.session.add(req); db.session.flush()

    gname = (game.name or "").strip().lower()
    is_gv   = ("gamevault" in gname.replace(" ", ""))
    is_juwa = ("juwa" in gname)
    is_mw   = ("milky" in gname)

    def _finish(res: dict, note_default: str):
        username = (res.get("username") or res.get("account") or f"user_{int(time.time())}").strip()
        password = (res.get("password") or "changeme123").strip()
        note = (res.get("note") or "") or note_default
        _save_or_update_game_account(current_user.id, game_id, username, password, note, req.id)
        _approve_request(req); db.session.commit()
        notify(current_user.id, f"🔐 Your {game.name} login is ready.")
        flash("Your login is ready 🎉", "success")
        return redirect(url_for("playerbp.mylogin", noinfo=1))

    auto_err = ""
    try:
        if is_gv and gv_create_account:
            res = gv_create_account(current_user.name or "", current_user.email or "")
            if res and res.get("ok"):
                return _finish(res, "Auto-provisioned via GameVault (instant)")
            auto_err = (res or {}).get("error", "GV auto-provision failed")

        elif is_juwa and juwa_create_sync:
            try:
                res = juwa_create_sync()
            except TypeError:
                res = juwa_create_sync(None, None)
            if res and res.get("ok"):
                return _finish(res, "Auto-provisioned via Juwa (instant)")
            auto_err = (res or {}).get("error", "Juwa auto-provision failed")

        elif is_mw and mw_create_player_auto:
            res = mw_create_player_auto() or {}
            if (res.get("created") and res.get("account")) or res.get("ok"):
                return _finish(
                    {"account": res.get("account"), "password": res.get("password") or res.get("account")},
                    "Auto-provisioned via Milkyway (instant)"
                )
            auto_err = (res or {}).get("error", "Milkyway auto-provision failed")

    except Exception as e:
        auto_err = str(e)
        log.exception("REQ-ID autoprov exception")

    # Queue for staff if automation failed
    req.status = "PENDING"; db.session.commit()
    notify(current_user.id, f"🕓 Your access request for {game.name} is being processed.")
    flash("✅ Request submitted. You’ll receive credentials soon.", "success")
    pname = _player_label(current_user)
    for staff in User.query.filter(User.role.in_(("EMPLOYEE", "ADMIN"))).all():
        notify(staff.id, f"New game access request: {game.name} by {pname} | auto note: {auto_err}")
    return redirect(url_for("index"))

# =============================================================================
#                          DEPOSIT (2 steps)
# =============================================================================

@player_bp.get("/deposit/step1")
@login_required
def deposit_step1():
    pre_id = request.args.get("game_id", type=int)
    if pre_id:
        return redirect(url_for("short_bp.deposit_step1_clean", game_id=pre_id))
    return _render_deposit_step1(preselect_game_id=pre_id)

@player_bp.post("/deposit/step1")
@login_required
def deposit_step1_post():
    if not _player_like():
        return abort(403)
    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0
    if amount <= 0:
        flash("Amount must be greater than zero.", "error")
        return redirect(url_for("playerbp.deposit_step1"))

    method = (request.form.get("method") or "CRYPTO").upper()
    if method not in ("CRYPTO", "CHIME", "CASHAPP"):
        flash("Invalid payment method.", "error")
        return redirect(url_for("playerbp.deposit_step1"))

    game_id_val = request.form.get("game_id")
    game_id = int(game_id_val) if game_id_val and str(game_id_val).isdigit() else None
    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            flash("Selected game is not available.", "error")
            return redirect(url_for("playerbp.deposit_step1"))

    return redirect(url_for("short_bp.deposit_step2_clean", game_id=game_id or 0, method=method, amount=amount))

@player_bp.get("/deposit/step2")
@login_required
def deposit_step2():
    amount = request.args.get("amount", type=int)
    method = (request.args.get("method") or "CRYPTO").upper()
    game_id = request.args.get("game_id", type=int)
    return _render_deposit_step2(game_id, method, amount)

@player_bp.post("/deposit/submit")
@login_required
def deposit_submit():
    if not _player_like():
        return abort(403)

    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0
    method = (request.form.get("method") or "CRYPTO").upper()
    game_id_val = request.form.get("game_id")
    game_id = int(game_id_val) if game_id_val and str(game_id_val).isdigit() else None

    # Optional proof image/url
    proof_file = request.files.get("proof_file")
    uploaded_url = _save_image(proof_file, "deposit_proof") if proof_file else None
    proof_url_text = (request.form.get("proof_url") or "").strip()
    screenshot_url = (request.form.get("screenshot_url") or "").strip()
    proof_url = uploaded_url or proof_url_text or screenshot_url

    if amount <= 0 or method not in ("CRYPTO", "CHIME", "CASHAPP"):
        flash("Invalid deposit details.", "error")
        return redirect(url_for("playerbp.deposit_step1"))

    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            flash("Selected game is not available.", "error")
            return redirect(url_for("playerbp.deposit_step1"))

    dep = DepositRequest(
        user_id=current_user.id,
        game_id=game_id,
        amount=amount,
        method=method,
        proof_url=proof_url or "",
        status="PENDING",
        provider="safepay" if method == "CASHAPP" else None,
        created_at=datetime.utcnow(),
    )
    db.session.add(dep)
    db.session.commit()

    # Staff notification details
    game_name = ""
    acc_note = ""
    if game_id:
        game = db.session.get(Game, game_id)
        game_name = f"{game.name} (#{game_id})" if game else f"Game #{game_id}"
        has_login, acc_note = _ensure_login_request_if_missing(current_user.id, game_id)
        if has_login and not acc_note:
            acc = GameAccount.query.filter_by(user_id=current_user.id, game_id=game_id).first()
            acc_note = f"login={_acc_username(acc) or '—'}"

    pname = _player_label(current_user)
    extra = f" | proof: {proof_url}" if proof_url else ""
    target = f" for {game_name}" if game_name else ""
    suffix = f" | {acc_note}" if acc_note else ""
    staff_msg = f"Deposit request #{dep.id}{target} — {pname}: {amount} via {method}{extra}{suffix}"
    for staff in User.query.filter(User.role.in_(("EMPLOYEE", "ADMIN"))).all():
        notify(staff.id, staff_msg)

    # Optional: Cash App invoice handoff
    if method == "CASHAPP" and create_cashapp_invoice:
        try:
            info = create_cashapp_invoice(float(amount), f"Deposit#{dep.id}", current_app.config)
            dep.provider = info.get("provider", "safepay-ui")
            dep.status = "RECEIVED"
            dep.pay_url = info.get("pay_url") or ""
            dep.backend_url = info.get("backend_url") or ""
            dep.provider_order_id = info.get("provider_order_id") or ""
            dep.meta = {"provider": dep.provider, "raw": info.get("raw")}
            db.session.commit()

            if dep.pay_url:
                return redirect(dep.pay_url)

            flash("Opening payment page… If nothing happens, please try again.", "success")
            return render_template("player_deposit_processing.html", deposit=dep, page_title="Processing…")

        except Exception as e:
            dep.status = "REJECTED"
            dep.meta = {"error": str(e)}
            db.session.commit()
            flash("Payment preparation failed. Please try again.", "error")
            return redirect(url_for("playerbp.deposit_step1"))

    notify(current_user.id, f"Deposit request #{dep.id} submitted. Your credits will appear shortly.")
    flash("Deposit submitted. We’ll notify you once it’s loaded.", "success")
    return redirect(url_for("index"))

# =============================================================================
#                         CASH APP status helpers
# =============================================================================

@player_bp.get("/deposit/<int:dep_id>/processing", endpoint="deposit_processing")
@login_required
def deposit_processing(dep_id: int):
    dep = db.session.get(DepositRequest, dep_id)
    if not dep or dep.user_id != current_user.id:
        return abort(404)
    return render_template("player_deposit_processing.html", deposit=dep, page_title="Processing…")

@player_bp.get("/deposit/<int:dep_id>/status.json", endpoint="deposit_status_json")
@login_required
def deposit_status_json(dep_id: int):
    dep = db.session.get(DepositRequest, dep_id)
    if not dep or dep.user_id != current_user.id:
        return jsonify({"ok": False}), 404
    err = ""
    try:
        if dep.meta and isinstance(dep.meta, dict):
            err = dep.meta.get("error", "") or dep.meta.get("reason", "")
    except Exception:
        err = ""
    return jsonify({"ok": True, "status": dep.status or "", "pay_url": dep.pay_url or "", "err": err})

# =============================================================================
#                               WITHDRAW
# =============================================================================

@player_bp.get("/withdraw")
@login_required
def withdraw_get():
    gid = request.args.get("game_id", type=int)
    if gid:
        return redirect(url_for("short_bp.withdraw_clean", game_id=gid))
    return _render_withdraw(game_id=None)

@player_bp.post("/withdraw")
@login_required
def withdraw_post():
    if not _player_like():
        return abort(403)

    total_amount = request.form.get("total_amount", type=int) or 0
    keep_amount  = request.form.get("keep_amount", type=int) or 0
    tip_amount   = request.form.get("tip_amount", type=int) or 0
    amount       = request.form.get("amount", type=int) or 0
    if amount <= 0 or total_amount <= 0:
        flash("Please enter a valid total and cash-out amount.", "error")
        return redirect(url_for("playerbp.withdraw_get"))

    game_id = request.form.get("game_id", type=int)
    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            flash("Selected game is not available.", "error")
            return redirect(url_for("playerbp.withdraw_get"))

    s = _get_settings()
    if s and s.min_redeem and amount < s.min_redeem:
        flash(f"Minimum withdraw is {s.min_redeem}.", "error")
        return redirect(url_for("playerbp.withdraw_get"))
    if s and s.max_redeem and amount > s.max_redeem:
        flash(f"Maximum withdraw is {s.max_redeem}.", "error")
        return redirect(url_for("playerbp.withdraw_get"))

    method = (request.form.get("method") or "CRYPTO").upper()
    if method not in ("CRYPTO", "CHIME"):
        method = "MANUAL"
    address = (request.form.get("address") or "").strip()

    wr = WithdrawRequest(
        user_id=current_user.id,
        game_id=game_id,
        amount=amount,
        method=method,
        status="PENDING",
        created_at=datetime.utcnow(),
    )
    db.session.add(wr)
    db.session.commit()

    game_name = ""
    acc_note = ""
    if game_id:
        game = db.session.get(Game, game_id)
        game_name = f"{game.name} (#{game_id})" if game else f"Game #{game_id}"
        has_login, acc_note = _ensure_login_request_if_missing(current_user.id, game_id)
        if has_login and not acc_note:
            acc = GameAccount.query.filter_by(user_id=current_user.id, game_id=game_id).first()
            acc_note = f"login={_acc_username(acc) or '—'}"

    pname = _player_label(current_user)
    parts = [
        f"Withdraw request #{wr.id} from {pname}",
        f"total={total_amount}",
        f"keep={keep_amount}",
        f"tip={tip_amount}",
        f"cashout={amount}",
        f"via {method}",
    ]
    if game_name:
        parts.append(f"game={game_name}")
    if address:
        parts.append(f"dest: {address}")
    if acc_note:
        parts.append(acc_note)
    staff_msg = " | ".join(parts)

    for staff in User.query.filter(User.role.in_(("EMPLOYEE", "ADMIN"))).all():
        notify(staff.id, staff_msg)

    notify(current_user.id, f"Withdrawal request #{wr.id} submitted. You’ll be notified once processed.")
    flash("Withdrawal request submitted. You’ll be notified once processed.", "success")
    return redirect(url_for("playerbp.withdraw_get"))

# =============================================================================
#                         NOTIFICATIONS
# =============================================================================

@player_bp.post("/notifications/<int:nid>/read", endpoint="note_mark_read")
@login_required
def note_mark_read(nid: int):
    n = db.session.get(Notification, nid)
    if not n or n.user_id != current_user.id:
        flash("Notification not found.", "error")
        return redirect(url_for("playerbp.player_dashboard"))
    n.is_read = True
    db.session.commit()
    return redirect(url_for("playerbp.player_dashboard"))

# =============================================================================
#                         MY LOGINS / ACCOUNTS
# =============================================================================

@player_bp.get("/mylogin", endpoint="mylogin")
@login_required
def accounts_page():
    if not _player_like():
        return abort(403)

    raw_accounts = (
        db.session.query(GameAccount, Game.name.label("game_name"))
        .join(Game, Game.id == GameAccount.game_id)
        .filter(GameAccount.user_id == current_user.id)
        .order_by(GameAccount.id.desc())
        .all()
    )
    accounts = []
    for acc, game_name in raw_accounts:
        accounts.append({
            "game": game_name,
            "username": _first_attr(acc, "account_username", "username", "login", "user", default=""),
            "password": _first_attr(acc, "account_password", "password", "passcode", "pin", default=""),
            "note": _first_attr(acc, "extra", "note", "notes", "remark", default=""),
        })

    raw_reqs = (
        db.session.query(GameAccountRequest, Game.name.label("game_name"))
        .join(Game, Game.id == GameAccountRequest.game_id)
        .filter(GameAccountRequest.user_id == current_user.id)
        .order_by(GameAccountRequest.created_at.desc())
        .all()
    )
    reqs = [{"game": gname, "status": req.status or "PENDING", "created_at": req.created_at}
            for req, gname in raw_reqs]

    juwa_has_login = any(gname.lower().strip() == "juwa" for gname in [a["game"] for a in accounts])
    return render_template(
        "player_accounts.html",
        accounts=accounts,
        requests=reqs,
        page_title="My Logins • NeonSpire Casino",
        juwa_has_login=juwa_has_login
    )

@player_bp.get("/logins")
@login_required
def legacy_logins_redirect():
    return redirect(url_for("playerbp.mylogin"))

# Keep your old alias too
player_bp.add_url_rule("/deposit", endpoint="deposit_get", view_func=deposit_step1)

# =============================================================================
#                               REFERRALS
# =============================================================================

def _generate_ref_code_for(user: User) -> str:
    base = "".join(re.findall(r"[A-Za-z]", (user.name or "").strip()))[:2].upper()
    if len(base) < 2:
        base = (base + "XX")[:2]
    for _ in range(50):
        num = random.randint(0, 9999)
        code = f"{base}{num:04d}"
        if not ReferralCode.query.filter_by(code=code).first():
            return code
    return f"RX{random.randint(0, 9999):04d}"

def _ensure_referral_for(user_id: int) -> ReferralCode:
    rc = ReferralCode.query.filter_by(user_id=user_id).first()
    if rc:
        return rc
    user = db.session.get(User, user_id)
    code = _generate_ref_code_for(user)
    rc = ReferralCode(user_id=user_id, code=code, created_at=datetime.utcnow())
    db.session.add(rc); db.session.commit()
    return rc

@player_bp.get("/referral")
@login_required
def referral_home():
    if not _player_like(): return abort(403)
    rc = _ensure_referral_for(current_user.id)
    share_url = url_for("playerbp.referral_landing", code=rc.code, _external=True)
    if _template_exists("player_referral.html"):
        return render_template("player_referral.html",
                               page_title="Referral • NeonSpire Casino",
                               code=rc.code, link=share_url)
    # minimal fallback template
    return render_template_string("""
    {% extends "base.html" %}
    {% block content %}
      <div class="shell">
        <div class="panel" style="display:flex;align-items:center;justify-content:space-between">
          <div class="h3">Referral Program</div>
          <a class="btn" href="{{ url_for('playerbp.player_dashboard') }}">← Back</a>
        </div>
        <div class="panel">
          <p>Your referral code:</p>
          <div class="h3" style="letter-spacing:2px">{{ code }}</div>
          <p style="margin-top:10px">Share this link with your friends:</p>
          <input class="input" style="width:100%" value="{{ link }}" readonly onclick="this.select()">
          <p class="muted" style="margin-top:10px">Format is first 2 letters of your name + 4 digits.</p>
        </div>
      </div>
    {% endblock %}
    """, code=rc.code, link=share_url)

@player_bp.get("/referral/my-link.json", endpoint="referral_my_link_json")
@login_required
def referral_my_link_json():
    if not _player_like(): return abort(403)
    rc = _ensure_referral_for(current_user.id)
    share_url = url_for("playerbp.referral_landing", code=rc.code, _external=True)
    return {"code": rc.code, "link": share_url}, 200

@player_bp.post("/referral/new")
@login_required
def referral_new_code():
    if not _player_like(): return abort(403)
    rc = ReferralCode.query.filter_by(user_id=current_user.id).first()
    if not rc:
        rc = _ensure_referral_for(current_user.id)
        flash("Referral code created.", "success")
        return redirect(url_for("playerbp.referral_home"))
    rc.code = _generate_ref_code_for(current_user); rc.created_at = datetime.utcnow()
    db.session.commit()
    flash("Referral code updated.", "success")
    return redirect(url_for("playerbp.referral_home"))

@player_bp.get("/ref/<string:code>")
def referral_landing(code: str):
    code = (code or "").strip().upper()
    rc = ReferralCode.query.filter_by(code=code).first()
    if not rc:
        flash("Referral code not found.", "error")
        return redirect(url_for("auth.register_get"))
    if hasattr(rc, "uses"):
        rc.uses = (rc.uses or 0) + 1; db.session.commit()
    return redirect(url_for("auth.register_get") + f"?ref={code}")

# =====================================================================
#                       SHORT / CLEAN ROUTES
# =====================================================================

@short_bp.get("/deposit/<int:game_id>", endpoint="deposit_step1_clean")
@login_required
def deposit_step1_clean(game_id: int):
    g = db.session.get(Game, game_id)
    if not g or not g.is_active:
        flash("Game not available.", "error")
        return redirect(url_for("index"))
    return _render_deposit_step1(preselect_game_id=game_id)

@short_bp.get("/deposit/<int:game_id>/step2/<string:method>/<int:amount>", endpoint="deposit_step2_clean")
@login_required
def deposit_step2_clean(game_id: int, method: str, amount: int):
    return _render_deposit_step2(game_id=game_id, method=method, amount=amount)

@short_bp.get("/withdraw/<int:game_id>", endpoint="withdraw_clean")
@login_required
def withdraw_clean(game_id: int):
    g = db.session.get(Game, game_id)
    if not g or not g.is_active:
        flash("Game not available.", "error")
        return redirect(url_for("index"))
    return _render_withdraw(game_id=game_id)

@short_bp.get("/logins", endpoint="logins_clean")
@login_required
def logins_clean():
    return redirect(url_for("playerbp.mylogin"))

# convenience aliases
@short_bp.get("/dep")
@login_required
def dep_root(): return redirect(url_for("playerbp.deposit_step1"))

@short_bp.get("/dep/<int:game_id>")
@login_required
def dep_with_game(game_id: int):
    return redirect(url_for("short_bp.deposit_step1_clean", game_id=game_id))

@short_bp.get("/dep/<string:method>")
@login_required
def dep_method_short(method: str):
    amount = request.args.get("amount", type=int)
    game_id = request.args.get("game_id", type=int)
    if not amount or amount <= 0:
        flash("Enter a valid amount.", "error")
        return redirect(url_for("playerbp.deposit_step1"))
    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            flash("Selected game is not available.", "error")
            return redirect(url_for("playerbp.deposit_step1"))
    return redirect(url_for("short_bp.deposit_step2_clean", game_id=game_id or 0, method=method.upper(), amount=amount))

@short_bp.get("/withd")
@login_required
def withd_short():
    game_id = request.args.get("game_id", type=int)
    if game_id:
        return redirect(url_for("short_bp.withdraw_clean", game_id=game_id))
    return _render_withdraw(game_id=None)

@short_bp.get("/log")
@login_required
def log_short(): return redirect(url_for("playerbp.mylogin"))

@short_bp.get("/reg")
def reg_short(): return redirect(url_for("auth.register_get"))

@short_bp.get("/lob")
def lob_short(): return redirect(url_for("index"))

@short_bp.get("/mylogin", endpoint="mylogin_clean")
@login_required
def mylogin_clean(): return accounts_page()