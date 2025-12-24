# player_bp.py
# -----------------------------------------------------------------------------
# Player flows (request ID, deposits/withdraws, dashboards, referrals)
# Includes a pluggable Provider interface for game automations (Juwa, Milkyway,
# UltraPanda, Vblink, GameVault). To add a new game later, create a Provider
# subclass and register it in the PROVIDERS dict.
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
    BonusSettings,  # NEW: Import BonusSetting model
    BonusRecord,   # NEW: Import BonusRecord model
    apply_bonus_to_deposit,  # NEW: Import bonus helper functions
    get_player_next_bonus
)
from models import notify
from models import DepositRequest as Deposit  # back-compat alias

# NOWPayments Crypto invoices
from nowpayments_client import create_invoice as np_create_invoice, NowPaymentsError

# =============================================================================
# Optional dependencies (guard every import)
# =============================================================================

# JUWA API
# JUWA UI bot (Playwright) – use juwa_ui_bot instead of juwa_api
try:
    import asyncio
    from automation import juwa_ui_bot as juwa_bot

    def juwa_create_sync() -> dict:
        async def _run():
            bundle = await juwa_bot._launch()
            pw, br, ctx, page = bundle
            try:
                lr = await juwa_bot.juwa_login(page)
                if not lr.get("ok"):
                    return {"ok": False, "error": f"login failed: {lr}"}
                # auto username/password (None, None)
                res = await juwa_bot.create_user(page, None, None)
                return res or {"ok": False, "error": "empty result from juwa_ui_bot"}
            finally:
                await juwa_bot._close(bundle)
        return asyncio.run(_run())

    def juwa_recharge_sync(account: str, amount: float) -> dict:
        async def _run():
            bundle = await juwa_bot._launch()
            pw, br, ctx, page = bundle
            try:
                lr = await juwa_bot.juwa_login(page)
                if not lr.get("ok"):
                    return {"ok": False, "error": f"login failed: {lr}"}
                return await juwa_bot.recharge_user(page, account, amount, "recharge")
            finally:
                await juwa_bot._close(bundle)
        return asyncio.run(_run())

    def juwa_redeem_sync(account: str, amount: float) -> dict:
        async def _run():
            bundle = await juwa_bot._launch()
            pw, br, ctx, page = bundle
            try:
                lr = await juwa_bot.juwa_login(page)
                if not lr.get("ok"):
                    return {"ok": False, "error": f"login failed: {lr}"}
                return await juwa_bot.redeem_user(page, account, amount, "redeem")
            finally:
                await juwa_bot._close(bundle)
        return asyncio.run(_run())

except Exception:
    # if anything goes wrong importing the bot, disable Juwa automation
    juwa_create_sync = juwa_recharge_sync = juwa_redeem_sync = None

# GameVault helper (you referenced before)
try:
    from gamevault_automation import gv_create_account
except Exception:
    gv_create_account = None

# Milkyway Playwright UI Bot (your working automation)
try:
    from automation.milkyway_ui_bot import mw_create_player_auto, mw_recharge, mw_redeem
except Exception:
    mw_create_player_auto = mw_recharge = mw_redeem = None

# Vblink Playwright UI Bot (your verified automation)
try:
    from automation import vblink_bot as vblink

    def _vb_create_sync() -> dict:
        import asyncio
        async def _run():
            pw, br, ctx, page = await vblink.launch_browser()
            try:
                lr = await vblink.login(page)
                if not lr.get("ok"):
                    return {"ok": False, "error": f"VB login failed: {lr}"}
                res = await vblink.create_user(
                    page,
                    None,
                    os.getenv("VB_DEFAULT_PASSWORD", "Ab123456")
                )
                if res and (res.get("ok") or res.get("created")):
                    acct = res.get("created") or res.get("account")
                    pwd  = os.getenv("VB_DEFAULT_PASSWORD", "Ab123456")
                    return {"ok": True, "account": acct, "password": pwd, "note": "Auto-provisioned via Vblink"}
                return res or {"ok": False}
            finally:
                await vblink.close_browser((pw, br, ctx, page))
        return asyncio.run(_run())

    def _vb_recharge_sync(account: str, amount: float, remark: str = "") -> dict:
        import asyncio
        async def _run():
            pw, br, ctx, page = await vblink.launch_browser()
            try:
                lr = await vblink.login(page)
                if not lr.get("ok"):
                    return {"ok": False, "error": f"VB login failed: {lr}"}
                res = await vblink.recharge(page, account, amount, remark or "recharge")
                return res or {"ok": False}
            finally:
                await vblink.close_browser((pw, br, ctx, page))
        return asyncio.run(_run())

    def _vb_redeem_sync(account: str, amount: float, remark: str = "") -> dict:
        import asyncio
        async def _run():
            pw, br, ctx, page = await vblink.launch_browser()
            try:
                lr = await vblink.login(page)
                if not lr.get("ok"):
                    return {"ok": False, "error": f"VB login failed: {lr}"}
                res = await vblink.redeem(page, account, amount, remark or "redeem")
                return res or {"ok": False}
            finally:
                await vblink.close_browser((pw, br, ctx, page))
        return asyncio.run(_run())

except Exception:
    vblink = None
    _vb_create_sync = _vb_recharge_sync = _vb_redeem_sync = None


# YOLO Playwright UI Bot (your verified automation from terminal)
try:
    from automation.yolo_bot import (
        recharge_sync as yolo_recharge_sync,
        redeem_sync as yolo_redeem_sync,
        auto_create_sync as yolo_create_sync,  # we made this in yolo_bot
    )
except Exception:
    # keep app running even if Playwright not installed
    yolo_recharge_sync = None
    yolo_redeem_sync = None
    yolo_create_sync = None


# Gameroom Playwright UI Bot (terminal verified)
try:
    from automation.gameroom_bot import (
        recharge_sync as gameroom_recharge_sync,
        redeem_sync as gameroom_redeem_sync,
        auto_create_sync as gameroom_create_sync,  # we exposed this in gameroom.py / bot
    )
except Exception:
    gameroom_recharge_sync = None
    gameroom_redeem_sync = None
    gameroom_create_sync = None


# UltraPanda Provider integration (via your automation/providers/ultrapanda.py)
try:
    from automation.providers import ultrapanda as up_mod

    def _up_create_sync(username: Optional[str] = None, password: Optional[str] = None) -> dict:
        # password is optional; your provider sets defaults
        return up_mod.create(username=username, password=password) or {}

    def _up_recharge_sync(account: str, amount: float, remark: str = "") -> dict:
        return up_mod.credit(username=account, amount=float(amount), note=remark) or {}

    def _up_redeem_sync(account: str, amount: float, remark: str = "") -> dict:
        return up_mod.redeem(username=account, amount=float(amount), note=remark) or {}

    def up_supported() -> bool:
        return True

    # you can later point this to a background worker
    start_ultrapanda_autoprovision = None


except Exception:
    # fallback when automation/providers/ultrapanda.py is missing or broken
    up_mod = None
    _up_create_sync = _up_recharge_sync = _up_redeem_sync = None

    def up_supported() -> bool:
        return False

    start_ultrapanda_autoprovision = None


    # FireKirin Provider integration (via your automation/firekirin_ui_bot.py)
try:
    from automation.firekirin_ui_bot import (
        recharge_sync as fk_recharge_sync,
        redeem_sync as fk_redeem_sync,
        auto_create_sync as fk_create_sync,
    )
    
    def _fk_create_sync() -> dict:
        return fk_create_sync() or {}
    
    def _fk_recharge_sync(account: str, amount: float, note: str = "") -> dict:
        return fk_recharge_sync(account, amount, note) or {}
    
    def _fk_redeem_sync(account: str, amount: float, note: str = "") -> dict:
        return fk_redeem_sync(account, amount, note) or {}
    
    def fk_supported() -> bool:
        return True

except Exception:
    # fallback when firekirin_ui_bot.py is missing or broken
    fk_recharge_sync = None
    fk_redeem_sync = None
    fk_create_sync = None
    _fk_create_sync = None
    _fk_recharge_sync = None
    _fk_redeem_sync = None
    
    def fk_supported() -> bool:
        return False

        # Orion Stars Provider integration (via your automation/orionstars_ui_bot.py)
try:
    from automation.orionstars_ui_bot import (
        recharge_sync as os_recharge_sync,
        redeem_sync as os_redeem_sync,
        auto_create_sync as os_create_sync,
    )
    
    def _os_create_sync() -> dict:
        return os_create_sync() or {}
    
    def _os_recharge_sync(account: str, amount: float, note: str = "") -> dict:
        return os_recharge_sync(account, amount, note) or {}
    
    def _os_redeem_sync(account: str, amount: float, note: str = "") -> dict:
        return os_redeem_sync(account, amount, note) or {}
    
    def os_supported() -> bool:
        return True

except Exception:
    # fallback when orionstars_ui_bot.py is missing or broken
    os_recharge_sync = None
    os_redeem_sync = None
    os_create_sync = None
    _os_create_sync = None
    _os_recharge_sync = None
    _os_redeem_sync = None
    
    def os_supported() -> bool:
        return False

# Optional Cash App invoice creator
try:
    from payments.safepay import create_cashapp_invoice  # noqa: F401
except Exception:
    create_cashapp_invoice = None

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = logging.getLogger("player.request")

# Optional minimum Crypto deposit in USD (NOWPAYMENTS_MIN_USD in .env)
NOWPAY_MIN_USD = float(os.getenv("NOWPAYMENTS_MIN_USD", "0") or 0)

USE_ID_QUEUE = True

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
    """
    Used on deposit/withdraw to ensure there's at least a login.
    In AUTO-ONLY mode: do NOT open manual requests, just report missing login.
    """
    if not game_id:
        return (False, "")
    existing = GameAccount.query.filter_by(user_id=user_id, game_id=game_id).first()
    if existing:
        return (True, f"login={_acc_username(existing) or '—'}")

    # No login, no auto-queue – just tell the caller
    return (False, "no login yet (please Request ID first)")

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

# ==================== NEW BONUS HELPER FUNCTIONS ====================

def _get_current_bonus_settings():
    """Get current bonus settings from database"""
    bonus_settings = BonusSettings.query.order_by(BonusSettings.updated_at.desc()).first()
    if not bonus_settings:
        # Create default settings if none exist
        bonus_settings = BonusSettings()
        db.session.add(bonus_settings)
        db.session.commit()
    return bonus_settings

def _get_player_bonus_info(player_id: int):
    """Get comprehensive bonus info for a player"""
    player = User.query.get(player_id)
    if not player:
        return None
    
    bonus_settings = _get_current_bonus_settings()
    
    # Calculate next bonus type
    next_bonus_type = get_player_next_bonus(player)
    
    # Get bonus records
    bonus_records = BonusRecord.query.filter_by(
        player_id=player_id
    ).order_by(BonusRecord.created_at.desc()).limit(10).all()
    
    return {
        'player': player,
        'bonus_settings': bonus_settings,
        'next_bonus_type': next_bonus_type,
        'bonus_records': bonus_records,
        'signup_bonus_claimed': player.signup_bonus_claimed,
        'deposit_count': player.deposit_count,
        'available_bonus': player.available_bonus
    }

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
        if not _up_create_sync:
            return {"ok": False, "error": "UltraPanda automation not configured"}
        # Provider-level create keeps no overrides; API endpoint can pass overrides.
        return _up_create_sync() or {}

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not _up_recharge_sync:
            return {"ok": False, "error": "UltraPanda recharge not configured"}
        return _up_recharge_sync(account, float(amount), note) or {}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        if not _up_redeem_sync:
            return {"ok": False, "error": "UltraPanda redeem not configured"}
        return _up_redeem_sync(account, float(amount), note) or {}

# ---- Vblink Provider (Playwright UI Bot) ------------------------------------
class VBlinkProvider(GameProvider):
    code = "VBLINK"
    name = "Vblink"

    def create(self) -> dict:
        if not _vb_create_sync:
            return {"ok": False, "error": "Vblink automation not configured"}
        return _vb_create_sync() or {}

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not _vb_recharge_sync:
            return {"ok": False, "error": "Vblink recharge not configured"}
        return _vb_recharge_sync(account, float(amount), note) or {}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        if not _vb_redeem_sync:
            return {"ok": False, "error": "Vblink redeem not configured"}
        return _vb_redeem_sync(account, float(amount), note) or {}
    
    # ---- YOLO Provider (Playwright UI Bot) --------------------------------------
class YoloProvider(GameProvider):
    code = "YOLO"
    name = "Yolo"

    def create(self) -> dict:
        # only works if your yolo_bot exposes auto_create_sync
        if not yolo_create_sync:
            return {"ok": False, "error": "YOLO automation not configured"}
        return yolo_create_sync() or {}

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not yolo_recharge_sync:
            return {"ok": False, "error": "YOLO recharge not configured"}
        # your yolo_bot takes (account, amount, note)
        return yolo_recharge_sync(account, float(amount), note or "recharge") or {}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        if not yolo_redeem_sync:
            return {"ok": False, "error": "YOLO redeem not configured"}
        return yolo_redeem_sync(account, float(amount), note or "redeem") or {}
    
    
    # ---- Gameroom Provider (Playwright UI Bot) ----------------------------------
class GameroomProvider(GameProvider):
    code = "GAMEROOM"
    name = "Gameroom"

    def create(self) -> dict:
        if not gameroom_create_sync:
            return {"ok": False, "error": "Gameroom automation not configured"}

        raw = gameroom_create_sync() or {}

        # normalize common shapes from terminal bot
        acct = (
            raw.get("account")
            or raw.get("username")
            or raw.get("created")
            or raw.get("user")
        )
        pwd = raw.get("password") or raw.get("pass") or raw.get("pwd")

        if acct:
            return {
                "ok": True,
                "account": acct,
                "password": pwd or acct,
                "note": raw.get("note") or "Auto-provisioned via Gameroom (normalized)",
            }

        return raw

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not gameroom_recharge_sync:
            return {"ok": False, "error": "Gameroom recharge not configured"}
        return gameroom_recharge_sync(account, float(amount), note or "recharge") or {}

    def redeem(self,account: str, amount: float, note: str = "") -> dict:
        if not gameroom_redeem_sync:
            return {"ok": False, "error": "Gameroom redeem not configured"}
        return gameroom_redeem_sync(account, float(amount), note or "redeem") or {}

        # ---- FireKirin Provider (Playwright UI Bot) ---------------------------------
class FireKirinProvider(GameProvider):
    code = "FIREKIRIN"
    name = "FireKirin"

    def create(self) -> dict:
        if not _fk_create_sync:
            return {"ok": False, "error": "FireKirin automation not configured"}
        
        raw = _fk_create_sync() or {}
        
        # normalize common shapes from terminal bot
        acct = (
            raw.get("account")
            or raw.get("username")
            or raw.get("created")
            or raw.get("user")
        )
        pwd = raw.get("password") or raw.get("pass") or raw.get("pwd")
        
        if acct:
            return {
                "ok": True,
                "account": acct,
                "password": pwd or acct,
                "note": raw.get("note") or "Auto-provisioned via FireKirin (normalized)",
            }
        
        return raw

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not _fk_recharge_sync:
            return {"ok": False, "error": "FireKirin recharge not configured"}
        return _fk_recharge_sync(account, float(amount), note or "recharge") or {}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        if not _fk_redeem_sync:
            return {"ok": False, "error": "FireKirin redeem not configured"}
        return _fk_redeem_sync(account, float(amount), note or "redeem") or {}

        # ---- Orion Stars Provider (Playwright UI Bot) -------------------------------
class OrionStarsProvider(GameProvider):
    code = "ORIONSTARS"
    name = "Orion Stars"

    def create(self) -> dict:
        if not _os_create_sync:
            return {"ok": False, "error": "Orion Stars automation not configured"}
        
        raw = _os_create_sync() or {}
        
        # normalize common shapes from terminal bot
        acct = (
            raw.get("account")
            or raw.get("username")
            or raw.get("created")
            or raw.get("user")
        )
        pwd = raw.get("password") or raw.get("pass") or raw.get("pwd")
        
        if acct:
            return {
                "ok": True,
                "account": acct,
                "password": pwd or acct,
                "note": raw.get("note") or "Auto-provisioned via Orion Stars (normalized)",
            }
        
        return raw

    def recharge(self, account: str, amount: float, note: str = "") -> dict:
        if not _os_recharge_sync:
            return {"ok": False, "error": "Orion Stars recharge not configured"}
        return _os_recharge_sync(account, float(amount), note or "recharge") or {}

    def redeem(self, account: str, amount: float, note: str = "") -> dict:
        if not _os_redeem_sync:
            return {"ok": False, "error": "Orion Stars redeem not configured"}
        return _os_redeem_sync(account, float(amount), note or "redeem") or {}

# ---- Register providers here (easy to extend) --------------------------------
PROVIDERS: dict[str, GameProvider] = {
    "JUWA": JuwaProvider(),
    "MILKYWAY": MilkywayProvider(),
    "ULTRAPANDA": UltraPandaProvider(),
    "VBLINK": VBlinkProvider(),
    "YOLO": YoloProvider(),
    "GAMEROOM": GameroomProvider(),   # ← NEW
    "FIREKIRIN": FireKirinProvider(),
    "ORIONSTARS": OrionStarsProvider(),
    # "GAMEVAULT": GameVaultProvider(),
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

    # 🔒 NEW: For CRYPTO / CHIME, a game is REQUIRED
    if method in ("CRYPTO", "CHIME") and not game:
        flash("Please select a game before depositing with Crypto or Chime.", "error")
        return redirect(url_for("playerbp.deposit_step1"))

    # 🔒 If a game is selected, user must already have a login for it
    if game and not GameAccount.query.filter_by(user_id=current_user.id, game_id=game.id).first():
        flash("You need a game login before depositing. Tap Request ID on the lobby first.", "error")
        return redirect(url_for("playerbp.mylogin"))

    settings = _get_settings()
    return render_template(
        "player_deposit_step2.html",
        amount=amount,
        method=method,
        game=game,
        settings=settings,
        page_title="Deposit • Step 2",
    )

def _render_withdraw(game_id: Optional[int]):
    if not _player_like():
        return abort(403)

    # 🔒 NEW: if a game is preselected, user must already have an account for it
    if game_id:
        if not GameAccount.query.filter_by(user_id=current_user.id, game_id=game_id).first():
            flash("You need a game login before withdrawing from this title. Tap Request ID on the lobby first.", "error")
            return redirect(url_for("index"))

    settings = _get_settings()
    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()

    # Figure out which game is selected (if any) so the template can show its name
    selected_game = None
    if game_id:
        for g in games:
            if g.id == game_id:
                selected_game = g
                break

    return render_template(
        "player_withdraw.html",
        settings=settings,
        games=games,
        preselect_game_id=game_id,
        selected_game=selected_game,
        page_title="Withdraw • NeonSpire Casino",
    )

# =============================================================================
#                    GENERIC GAME ROUTES (provider-based)
# =============================================================================

@player_bp.post("/<string:game_slug>/request-id", endpoint="generic_request_id")
@login_required
def generic_request_id(game_slug: str):
    """
    Request ID for a provider by slug.

    - In queue mode (USE_ID_QUEUE=True): just create a PENDING GameAccountRequest
      and let id_request_worker.py handle all automation.
    - In instant mode (USE_ID_QUEUE=False): keep old behavior (direct provider.create()).
    """
    provider = _provider_for_slug(game_slug)
    if not provider:
        flash("Game automation not configured.", "error")
        return redirect(url_for("playerbp.mylogin"))

    game = provider.ensure_game()

    # Prevent duplicates
    if GameAccount.query.filter_by(user_id=current_user.id, game_id=game.id).first():
        flash(f"You already have a {game.name} login. Check My Logins.", "error")
        return redirect(url_for("playerbp.mylogin"))

    # Create the request row
    req = GameAccountRequest(
        user_id=current_user.id,
        game_id=game.id,
        status="PENDING",
        created_at=datetime.utcnow(),
    )
    db.session.add(req)
    db.session.flush()

    # 🟢 QUEUE MODE → enqueue Celery task and send back to lobby (index)
    if USE_ID_QUEUE:
        db.session.commit()

        try:
            # Lazy import to avoid circular import
            from id_requests import enqueue_game_account_request
            enqueue_game_account_request(req.id)
        except Exception as e:
            # fallback: mark as failed if Celery not reachable
            req.status = "FAILED"
            if hasattr(req, "last_error"):
                req.last_error = f"Celery enqueue failed: {e}"
            if hasattr(req, "updated_at"):
                req.updated_at = datetime.utcnow()
            db.session.commit()
            flash("Automatic ID queue is temporarily unavailable.", "error")
            return redirect(url_for("playerbp.mylogin"))

        # no flash, no notify – let index.html show loading
        return redirect(url_for("index", req_id=req.id, game_id=game.id))

    # 🔻 OLD INSTANT MODE BELOW (unchanged, only runs if USE_ID_QUEUE=False)
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

    # Fallback: DO NOT keep failed request as pending in instant mode
    try:
        db.session.delete(req)
        db.session.commit()
    except Exception:
        db.session.rollback()

    flash(f"{provider.name} automatic ID creation failed: {auto_err}", "error")
    notify(
        current_user.id,
        f"⚠️ {provider.name} automatic ID creation failed. Please try again later."
    )
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
    return generic_request_id("milkyway")

@player_bp.post("/milkyway/recharge", endpoint="milkyway_recharge")
@login_required
def milkyway_recharge_alias():
    return generic_recharge.__wrapped__("milkyway")

@player_bp.post("/milkyway/redeem", endpoint="milkyway_redeem")
@login_required
def milkyway_redeem_alias():
    return generic_redeem.__wrapped__("milkyway")

@player_bp.post("/vblink/request-id", endpoint="vblink_request_id")
@login_required
def vblink_request_id_alias():
    return generic_request_id.__wrapped__("vblink")

@player_bp.post("/vblink/recharge", endpoint="vblink_recharge")
@login_required
def vblink_recharge_alias():
    return generic_recharge.__wrapped__("vblink")

@player_bp.post("/vblink/redeem", endpoint="vblink_redeem")
@login_required
def vblink_redeem_alias():
    return generic_redeem.__wrapped__("vblink")


##### yolo #######
@player_bp.post("/yolo/request-id", endpoint="yolo_request_id")
@login_required
def yolo_request_id_alias():
    return generic_request_id.__wrapped__("yolo")


@player_bp.post("/yolo/recharge", endpoint="yolo_recharge")
@login_required
def yolo_recharge_alias():
    return generic_recharge.__wrapped__("yolo")


@player_bp.post("/yolo/redeem", endpoint="yolo_redeem")
@login_required
def yolo_redeem_alias():
    return generic_redeem.__wrapped__("yolo")

##### gameroom #######
@player_bp.post("/gameroom/request-id", endpoint="gameroom_request_id")
@login_required
def gameroom_request_id_alias():
    return generic_request_id.__wrapped__("gameroom")


@player_bp.post("/gameroom/recharge", endpoint="gameroom_recharge")
@login_required
def gameroom_recharge_alias():
    return generic_recharge.__wrapped__("gameroom")


@player_bp.post("/gameroom/redeem", endpoint="gameroom_redeem")
@login_required
def gameroom_redeem_alias():
    return generic_redeem.__wrapped__("gameroom")

    ##### firekirin #######
@player_bp.post("/firekirin/request-id", endpoint="firekirin_request_id")
@login_required
def firekirin_request_id_alias():
    return generic_request_id.__wrapped__("firekirin")


@player_bp.post("/firekirin/recharge", endpoint="firekirin_recharge")
@login_required
def firekirin_recharge_alias():
    return generic_recharge.__wrapped__("firekirin")


@player_bp.post("/firekirin/redeem", endpoint="firekirin_redeem")
@login_required
def firekirin_redeem_alias():
    return generic_redeem.__wrapped__("firekirin")


        ##### orionstars #######
@player_bp.post("/orionstars/request-id", endpoint="orionstars_request_id")
@login_required
def orionstars_request_id_alias():
    return generic_request_id.__wrapped__("orionstars")

@player_bp.post("/orionstars/recharge", endpoint="orionstars_recharge")
@login_required
def orionstars_recharge_alias():
    return generic_recharge.__wrapped__("orionstars")

@player_bp.post("/orionstars/redeem", endpoint="orionstars_redeem")
@login_required
def orionstars_redeem_alias():
    return generic_redeem.__wrapped__("orionstars")

# Optional: UltraPanda explicit aliases (not strictly needed since generic routes work)
@player_bp.post("/ultrapanda/request-id", endpoint="ultrapanda_request_id")
@login_required
def ultrapanda_request_id_alias():
    return generic_request_id.__wrapped__("ultrapanda")

@player_bp.post("/ultrapanda/recharge", endpoint="ultrapanda_recharge")
@login_required
def ultrapanda_recharge_alias():
    return generic_recharge.__wrapped__("ultrapanda")

@player_bp.post("/ultrapanda/redeem", endpoint="ultrapanda_redeem")
@login_required
def ultrapanda_redeem_alias():
    return generic_redeem.__wrapped__("ultrapanda")

# =============================================================================
#                              DASHBOARD / PAGES
# =============================================================================

@player_bp.get("/dashboard")
@login_required
def player_dashboard():
    if not _player_like():
        return abort(403)

    # 🔹 NEW: check if a queued request was passed in URL (from queued_requests.py)
    pending_req_id = request.args.get("req_id", type=int)
    pending_req_game_id = None
    if pending_req_id:
        req = db.session.get(GameAccountRequest, pending_req_id)
        if req and req.user_id == current_user.id:
            pending_req_game_id = req.game_id

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
    
    # 🔹 NEW: Get bonus settings and player bonus info
    bonus_settings = _get_current_bonus_settings()
    player_bonus_info = _get_player_bonus_info(current_user.id)

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
        # 🔹 NEW: Pass bonus info to template
        bonus_settings=bonus_settings,
        player_bonus_info=player_bonus_info,
        # 🔹 NEW: info for loading state
        pending_req_id=pending_req_id,
        pending_req_game_id=pending_req_game_id,
    )


# =============================================================================
#                         NEW BONUS SYSTEM ROUTES
# =============================================================================

@player_bp.get("/bonus-info")
@login_required
def bonus_info():
    """Get bonus info for player dashboard (JSON API)"""
    if not _player_like():
        return abort(403)
    
    player_bonus_info = _get_player_bonus_info(current_user.id)
    if not player_bonus_info:
        return jsonify({"error": "Player not found"}), 404
    
    return jsonify({
        "signup_bonus": {
            "active": player_bonus_info['bonus_settings'].signup_active,
            "percentage": player_bonus_info['bonus_settings'].signup_percentage,
            "max_amount": player_bonus_info['bonus_settings'].signup_max_amount,
            "min_deposit": player_bonus_info['bonus_settings'].signup_min_deposit,
            "wagering": player_bonus_info['bonus_settings'].signup_wagering,
            "eligible": not player_bonus_info['player'].signup_bonus_claimed
        },
        "regular_bonus": {
            "active": player_bonus_info['bonus_settings'].regular_active,
            "percentage": player_bonus_info['bonus_settings'].regular_percentage,
            "max_amount": player_bonus_info['bonus_settings'].regular_max_amount,
            "min_deposit": player_bonus_info['bonus_settings'].regular_min_deposit,
            "wagering": player_bonus_info['bonus_settings'].regular_wagering,
            "eligible": player_bonus_info['player'].deposit_count > 0
        },
        "next_bonus": player_bonus_info['next_bonus_type'],
        "available_bonus": player_bonus_info['player'].available_bonus,
        "deposit_count": player_bonus_info['player'].deposit_count,
        "signup_bonus_claimed": player_bonus_info['player'].signup_bonus_claimed
    })

@player_bp.post("/apply-bonus")
@login_required
def apply_bonus_api():
    """Apply bonus to a deposit (called by employee after approval)"""
    if not _player_like():
        return abort(403)
    
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
    
    player_id = data.get("player_id")
    deposit_id = data.get("deposit_id")
    deposit_amount = float(data.get("deposit_amount", 0))
    
    if not player_id or not deposit_id:
        return jsonify({"error": "Missing player_id or deposit_id"}), 400
    
    # Get player and deposit
    player = User.query.get(player_id)
    if not player:
        return jsonify({"error": "Player not found"}), 404
    
    deposit = DepositRequest.query.get(deposit_id)
    if not deposit:
        return jsonify({"error": "Deposit not found"}), 404
    
    # Get bonus settings
    bonus_settings = _get_current_bonus_settings()
    
    # Determine bonus type
    if not player.signup_bonus_claimed and player.deposit_count == 0:
        bonus_type = "signup"
    else:
        bonus_type = "regular"
    
    try:
        # Apply bonus using the helper function from models
        bonus_amount, total_credited = apply_bonus_to_deposit(
            player, deposit, bonus_type, bonus_settings
        )
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "bonus_amount": bonus_amount,
            "total_credited": total_credited,
            "bonus_type": bonus_type,
            "available_bonus": player.available_bonus
        })
        
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Bonus application failed: {str(e)}"}), 500

@player_bp.get("/bonus-history")
@login_required
def bonus_history():
    """Get bonus history for player"""
    if not _player_like():
        return abort(403)
    
    bonus_records = BonusRecord.query.filter_by(
        player_id=current_user.id
    ).order_by(BonusRecord.created_at.desc()).limit(50).all()
    
    history = []
    for record in bonus_records:
        history.append({
            "id": record.id,
            "bonus_type": record.bonus_type,
            "amount": record.amount,
            "status": record.status,
            "wagering_completed": record.wagering_completed,
            "created_at": record.created_at.isoformat() if record.created_at else None
        })
    
    return jsonify({
        "success": True,
        "history": history,
        "total_count": len(bonus_records)
    })

@player_bp.get("/bonus/settings")
@login_required
def get_bonus_settings():
    """Get current bonus settings for display"""
    if not _player_like():
        return abort(403)
    
    bonus_settings = _get_current_bonus_settings()
    
    return jsonify({
        "signup_bonus": {
            "active": bonus_settings.signup_active,
            "percentage": bonus_settings.signup_percentage,
            "max_amount": bonus_settings.signup_max_amount,
            "min_deposit": bonus_settings.signup_min_deposit,
            "wagering": bonus_settings.signup_wagering
        },
        "regular_bonus": {
            "active": bonus_settings.regular_active,
            "percentage": bonus_settings.regular_percentage,
            "max_amount": bonus_settings.regular_max_amount,
            "min_deposit": bonus_settings.regular_min_deposit,
            "wagering": bonus_settings.regular_wagering
        }
    })

@player_bp.post("/bonus/claim")
@login_required
def claim_bonus_for_deposit():
    """Player claims bonus for a specific deposit"""
    if not _player_like():
        return abort(403)
    
    deposit_id = request.form.get("deposit_id", type=int)
    if not deposit_id:
        flash("Invalid deposit ID.", "error")
        return redirect(url_for("playerbp.player_dashboard"))
    
    # This endpoint is for player UI - actual bonus application happens
    # when employee approves deposit and clicks "Approve & Load"
    flash("Bonus will be applied when your deposit is approved by staff.", "info")
    return redirect(url_for("playerbp.player_dashboard"))

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
    """
    Create an UltraPanda account for the current user.
    Accepts optional JSON payload: { "username": "...", "password": "..." }
    Always persists to GameAccount using the generic saver to handle any column schema.
    """
    if not up_supported():
        return jsonify({"ok": False, "error": "UltraPanda disabled"}), 400

    payload = request.get_json(silent=True) or {}
    want_user = (payload.get("username") or "").strip() or None
    want_pwd  = (payload.get("password") or "").strip() or None

    # Run the UI bot to create a user (honor overrides if provided)
    res = _up_create_sync(username=want_user, password=want_pwd)
    if not res or not res.get("ok"):
        return jsonify(res or {"ok": False, "error": "UltraPanda create failed"}), 400

    game = _ensure_ultrapanda_game()
    acct_name = (res.get("account") or want_user or "").strip()

    if acct_name:
        # Persist using the generic helper so we don't care about column names
        username = acct_name
        password = want_pwd or res.get("password") or username or "changeme123"
        note     = res.get("note") or "Auto-provisioned via UltraPanda"
        _save_or_update_game_account(
            user_id=current_user.id,
            game_id=game.id,
            username=username,
            password=password,
            note=note,
            request_id=None,
        )
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

    res = _up_recharge_sync(account, amount, remark)
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

    res = _up_redeem_sync(account, amount, remark)
    if not res.get("ok"):
        return jsonify(res), 400

    return jsonify({"ok": True, "result": res})

@player_bp.post("/api/ultrapanda/request")
@login_required
def api_ultrapanda_request():
    """
    Queue or instant-create an UltraPanda account for the current user.
    Uses external worker if available, otherwise local instant fallback.
    """
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
        if up_supported() and _up_create_sync:
            r = _up_create_sync() or {}
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
    """
    Request a game account by numeric game_id.

    - If USE_ID_QUEUE=True: create a PENDING GameAccountRequest and enqueue the
      Celery task via id_requests.enqueue_game_account_request, then send the
      player back to the lobby so index.html can show a spinner.
    - If USE_ID_QUEUE=False: fall back to the old instant auto-provision logic.
    """
    if not _player_like():
        return abort(403)

    game = db.session.get(Game, game_id)
    if not game or not game.is_active:
        flash("Game not available.", "error")
        return redirect(url_for("index"))

    # Already has this account?
    if GameAccount.query.filter_by(user_id=current_user.id, game_id=game_id).first():
        flash("⚠️ You already have this account.", "error")
        notify(current_user.id, "⚠️ You already have this game account. Check My Logins.")
        return redirect(url_for("playerbp.mylogin", noinfo=1))

    # 🟢 QUEUE MODE → create a request row and enqueue worker, then return to lobby
    if USE_ID_QUEUE:
        req = GameAccountRequest(
            user_id=current_user.id,
            game_id=game.id,
            status="PENDING",
            created_at=datetime.utcnow(),
        )
        db.session.add(req)
        db.session.flush()  # get req.id
        db.session.commit()

        try:
            # Use the same helper as generic_request_id
            from id_requests import enqueue_game_account_request
            enqueue_game_account_request(req.id)
        except Exception as e:
            req.status = "FAILED"
            if hasattr(req, "last_error"):
                req.last_error = f"Celery enqueue failed: {e}"
            if hasattr(req, "updated_at"):
                req.updated_at = datetime.utcnow()
            db.session.commit()
            flash("Automatic ID queue is temporarily unavailable.", "error")
            return redirect(url_for("playerbp.mylogin", noinfo=1))

        # index.html will show card-level loading using ?req_id=&game_id=
        return redirect(url_for("index", req_id=req.id, game_id=game_id))

    # 🔻 OLD INSTANT MODE BELOW (only used if USE_ID_QUEUE=False)
    # Create a request row so we can mark it APPROVED or delete it on failure
    req = GameAccountRequest(
        user_id=current_user.id,
        game_id=game.id,
        status="PENDING",
        created_at=datetime.utcnow(),
    )
    db.session.add(req)
    db.session.flush()

    gname = (game.name or "").strip().lower()
    is_gv   = ("gamevault" in gname.replace(" ", ""))
    is_juwa = ("juwa" in gname)
    is_mw   = ("milky" in gname)
    is_vb   = ("vblink" in gname)   # support Vblink on legacy numeric route
    is_up   = (gname in ("ultrapanda", "ultra panda")) or ("ultrapanda" in gname)
    is_yolo = ("yolo" in gname)
    is_gr   = ("gameroom" in gname) or ("game room" in gname) or (gname == "gr")
    is_fk   = ("firekirin" in gname) or ("fire kirin" in gname)  # ← NEW
    is_os   = ("orionstars" in gname) or ("orion stars" in gname) or ("orion" in gname)  # 

    def _finish(res: dict, note_default: str):
        username = (res.get("username") or res.get("account") or f"user_{int(time.time())}").strip()
        password = (res.get("password") or "changeme123").strip()
        note = (res.get("note") or "") or note_default
        _save_or_update_game_account(current_user.id, game_id, username, password, note, req.id)
        _approve_request(req)
        db.session.commit()
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
                    "Auto-provisioned via Milkyway (instant)",
                )
            auto_err = (res or {}).get("error", "Milkyway auto-provision failed")

        elif is_vb and _vb_create_sync:
            res = _vb_create_sync() or {}
            if res.get("ok"):
                return _finish(res, "Auto-provisioned via Vblink (instant)")
            auto_err = (res or {}).get("error", "Vblink auto-provision failed")

        elif is_yolo and yolo_create_sync:
            res = yolo_create_sync() or {}
            if res.get("ok") or res.get("account"):
                return _finish(res, "Auto-provisioned via YOLO (instant)")
            auto_err = (res or {}).get("error", "YOLO auto-provision failed")

        elif is_gr and gameroom_create_sync:
            res = gameroom_create_sync() or {}
            if res.get("ok") or res.get("account"):
                return _finish(res, "Auto-provisioned via Gameroom (instant)")
            auto_err = (res or {}).get("error", "Gameroom auto-provision failed")

        elif is_fk and _fk_create_sync:
            res = _fk_create_sync() or {}
            if res.get("ok") or res.get("account"):
                return _finish(res, "Auto-provisioned via FireKirin (instant)")
            auto_err = (res or {}).get("error", "FireKirin auto-provision failed")

        elif is_os and _os_create_sync:
            res = _os_create_sync() or {}
            if res.get("ok") or res.get("account"):
                return _finish(res, "Auto-provisioned via Orion Stars (instant)")
            auto_err = (res or {}).get("error", "Orion Stars auto-provision failed")

        elif is_up and _up_create_sync:
            res = _up_create_sync() or {}
            if res.get("ok") or res.get("account"):
                return _finish(res, "Auto-provisioned via UltraPanda (instant)")
            auto_err = (res or {}).get("error", "UltraPanda auto-provision failed")

    except Exception as e:
        auto_err = str(e)
        log.exception("REQ-ID autoprov exception")

    # DO NOT keep failed request as pending in instant mode
    try:
        db.session.delete(req)
        db.session.commit()
    except Exception:
        db.session.rollback()

    # -------- Friendly error message for the player --------
    err_text = (auto_err or "").lower()
    if "captcha_api_key" in err_text or "captcha" in err_text:
        friendly_msg = f"Automatic ID creation for {game.name} is temporarily unavailable due to a verification issue. Please try again later."
    elif "login" in err_text:
        friendly_msg = f"We couldn't reach the {game.name} server. Please try again in a few minutes."
    elif "network" in err_text or "timeout" in err_text:
        friendly_msg = "Network issue while talking to the game server. Please try again in a few minutes."
    else:
        friendly_msg = f"We couldn't automatically create your {game.name} ID. Please try again shortly."

    flash(friendly_msg, "error")
    notify(current_user.id, f"⚠️ {friendly_msg}")
    return redirect(url_for("playerbp.mylogin", noinfo=1))

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

    # 🔒 For CRYPTO / CHIME, a game is REQUIRED
    if method in ("CRYPTO", "CHIME") and not game_id:
        flash("Please select a game before depositing with Crypto or Chime.", "error")
        return redirect(url_for("playerbp.deposit_step1"))

    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            flash("Selected game is not available.", "error")
            return redirect(url_for("playerbp.deposit_step1"))

        # require that the player already has this game account
        if not GameAccount.query.filter_by(user_id=current_user.id, game_id=game_id).first():
            flash("You need a game login before depositing. Tap Request ID on the lobby first.", "error")
            return redirect(url_for("playerbp.mylogin"))

    return redirect(
        url_for(
            "short_bp.deposit_step2_clean",
            game_id=game_id or 0,
            method=method,
            amount=amount,
        )
    )
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

    # basic fields
    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0

    method = (request.form.get("method") or "CRYPTO").upper()
    game_id_val = request.form.get("game_id")
    game_id = int(game_id_val) if game_id_val and str(game_id_val).isdigit() else None

    # extra info from Step 2 form
    payer_name = (request.form.get("payer_name") or "").strip()
    payer_details = (request.form.get("payer_details") or "").strip()

    # Optional proof image/url
    proof_file = request.files.get("proof_file")
    uploaded_url = _save_image(proof_file, "deposit_proof") if proof_file else None
    proof_url_text = (request.form.get("proof_url") or "").strip()
    screenshot_url = (request.form.get("screenshot_url") or "").strip()
    proof_url = uploaded_url or proof_url_text or screenshot_url

    if amount <= 0 or method not in ("CRYPTO", "CHIME", "CASHAPP"):
        flash("Invalid deposit details.", "error")
        return redirect(url_for("playerbp.deposit_step1"))

    # 🔒 For CRYPTO / CHIME, a game is REQUIRED
    if method in ("CRYPTO", "CHIME") and not game_id:
        flash("Please select a game before depositing with Crypto or Chime.", "error")
        return redirect(url_for("playerbp.deposit_step1"))

    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            flash("Selected game is not available.", "error")
            return redirect(url_for("playerbp.deposit_step1"))

        # require game login before actually creating the deposit
        if not GameAccount.query.filter_by(user_id=current_user.id, game_id=game_id).first():
            flash("You need a game login before depositing. Tap Request ID on the lobby first.", "error")
            return redirect(url_for("playerbp.mylogin"))

    # create the deposit row first
    dep = DepositRequest(
        user_id=current_user.id,
        game_id=game_id,
        amount=amount,
        method=method,
        proof_url=proof_url or "",
        status="PENDING",
        provider="safepay" if method == "CASHAPP" else None,
        created_at=datetime.utcnow(),
        meta={
            "payer_name": payer_name,
            "payer_details": payer_details,
        },
    )
    db.session.add(dep)
    db.session.commit()

    # ==================== NOWPAYMENTS for CRYPTO ====================
    if method == "CRYPTO":
        try:
            # Optional minimum in USD
            if float(amount) < 11:
                flash("Minimum Crypto deposit is $11.00.", "error")
                return redirect(url_for("playerbp.deposit_step1"))

            order_id = f"dep-{dep.id}"
            desc = f"Crypto deposit #{dep.id} for user {current_user.id}"

            np_data = np_create_invoice(
                amount=float(amount),
                order_id=order_id,
                description=desc,
            )

            dep.provider = "nowpayments"
            dep.provider_order_id = str(
                np_data.get("id") or np_data.get("order_id") or order_id
            )
            dep.pay_url = np_data.get("invoice_url", "")
            dep.backend_url = np_data.get("invoice_url", "")
            dep.meta = {
                "provider": "nowpayments",
                "raw": np_data,
                "payer_name": payer_name,
                "payer_details": payer_details,
                "proof_url": proof_url or "",
            }
            db.session.commit()

            # Redirect straight to NowPayments checkout
            if dep.pay_url:
                return redirect(dep.pay_url)

            # Fallback: no URL returned
            flash(
                "Crypto provider did not return a payment link. We will review your request manually.",
                "warning",
            )

        except NowPaymentsError as e:
            dep.status = "REJECTED"
            dep.meta = {"provider": "nowpayments", "error": str(e)}
            db.session.commit()
            flash("Crypto payment provider error. Please try again later.", "error")
            return redirect(url_for("playerbp.deposit_step1"))
        except Exception as e:
            dep.status = "REJECTED"
            dep.meta = {"provider": "nowpayments", "error": str(e)}
            db.session.commit()
            flash("Could not start Crypto payment right now. Please try again.", "error")
            return redirect(url_for("playerbp.deposit_step1"))
    # ================= end NOWPAYMENTS block ========================

    # Staff notification details (unchanged)
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

    # build extra info string for staff (crypto/chime details + proof)
    extra_bits = []
    if proof_url:
        extra_bits.append(f"proof={proof_url}")
    if payer_name:
        extra_bits.append(f"name={payer_name}")
    if payer_details:
        extra_bits.append(f"details={payer_details}")

    extra = " | ".join(extra_bits) if extra_bits else ""
    if extra:
        extra = " | " + extra

    target = f" for {game_name}" if game_name else ""
    suffix = f" | {acc_note}" if acc_note else ""

    staff_msg = (
        f"Deposit request #{dep.id}{target} — {pname}: "
        f"{amount} via {method}{extra}{suffix}"
    )

    for staff in User.query.filter(User.role.in_(("EMPLOYEE", "ADMIN"))).all():
        notify(staff.id, staff_msg)

    # Optional: Cash App invoice handoff (unchanged)
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

            # if provider gave us a direct pay_url, go there
            if dep.pay_url:
                return redirect(dep.pay_url)

            # otherwise show the same polling page
            return render_template(
                "player_deposit_processing.html",
                deposit=dep,
                page_title="Processing…",
            )

        except Exception as e:
            dep.status = "REJECTED"
            dep.meta = {"error": str(e)}
            db.session.commit()
            flash("Payment preparation failed. Please try again.", "error")
            return redirect(url_for("playerbp.deposit_step1"))

    # For CHIME we still show the processing page
    if method == "CHIME":
        return render_template(
            "player_deposit_processing.html",
            deposit=dep,
            page_title="Processing…",
        )

    # fallback (other manual methods)
    notify(
        current_user.id,
        f"Deposit request #{dep.id} submitted. Your credits will appear shortly.",
    )
    flash("Deposit submitted. We'll notify you once it's loaded.", "success")
    return redirect(url_for("index"))


# =============================================================================
#                      GAME ID REQUEST STATUS (for polling)
# =============================================================================

@player_bp.get("/request/<int:req_id>/status.json", endpoint="request_status_json")
@login_required
def request_status_json(req_id: int):
    req = db.session.get(GameAccountRequest, req_id)
    if not req or req.user_id != current_user.id:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "status": req.status or ""})

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
        # front-end JS expects JSON
        return jsonify({"ok": False, "error": "Please enter a valid total and cash-out amount."}), 400

    game_id = request.form.get("game_id", type=int)
    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            return jsonify({"ok": False, "error": "Selected game is not available."}), 400

        # 🔒 NEW: must already have a GameAccount for this game
        if not GameAccount.query.filter_by(user_id=current_user.id, game_id=game_id).first():
            return jsonify({
                "ok": False,
                "error": "You need a game login for that title before requesting a withdrawal."
            }), 400

    s = _get_settings()
    if s and s.min_redeem and amount < s.min_redeem:
        return jsonify({"ok": False, "error": f"Minimum withdraw is {s.min_redeem}."}), 400
    if s and s.max_redeem and amount > s.max_redeem:
        return jsonify({"ok": False, "error": f"Maximum withdraw is {s.max_redeem}."}), 400

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
    # store extra info (not mandatory, but useful)
    wr.total_amount = total_amount
    wr.keep_amount = keep_amount
    wr.tip_amount = tip_amount
    wr.address = address

    db.session.add(wr)
    db.session.commit()

    # notify staff (keep your original logic)
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

    # player in-site notification
    notify(current_user.id, f"Withdrawal request #{wr.id} submitted. You'll be notified once processed.")
        # ✅ Telegram staff alert for new withdrawal
    try:
        from telegram_bot import notify_withdraw_request
        game_name_val = game_name or "—"
        notify_withdraw_request(
            user=current_user,
            amount=amount,
            method=method,
            address=address,
            game_name=game_name_val
        )
    except Exception as e:
        print("[telegram_notify_withdraw] failed:", e)

    # return JSON so the template can show messages
    return jsonify({
        "ok": True,
        "id": wr.id,
        "status": wr.status or "PENDING"
    }), 200
    
@player_bp.get("/withdraw/status/<int:wr_id>")
@login_required
def withdraw_status(wr_id: int):
    wr = db.session.get(WithdrawRequest, wr_id)
    if not wr or wr.user_id != current_user.id:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({
        "ok": True,
        "id": wr.id,
        "status": wr.status or "PENDING",
        "amount": wr.amount,
        "method": wr.method,
        "created_at": wr.created_at.isoformat() if wr.created_at else None
    })

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

    # 🔹 NEW: Get player bonus info
    player_bonus_info = _get_player_bonus_info(current_user.id)

    juwa_has_login = any(gname.lower().strip() == "juwa" for gname in [a["game"] for a in accounts])

    return render_template(
        "player_accounts.html",
        accounts=accounts,
        player_bonus_info=player_bonus_info,  # NEW: Pass bonus info to template
        page_title="My Logins • NeonSpire Casino",
        juwa_has_login=juwa_has_login
        # ❌ do NOT pass `requests=reqs` anymore
    )

@player_bp.get("/logins")
@login_required
def legacy_logins_redirect():
    return redirect(url_for("playerbp.mylogin"))

# Keep your old alias too
player_bp.add_url_rule("/deposit", endpoint="deposit_get", view_func=deposit_step1)

# Proper withdraw GET handler (supports ?game_id=...)
@player_bp.get("/withdraw", endpoint="withdraw_get")
@login_required
def withdraw_get():
    # If called as /player/withdraw?game_id=7 we lock to that game.
    # If no game_id param, user must choose a game on the form.
    game_id = request.args.get("game_id", type=int)
    return _render_withdraw(game_id=game_id)


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


    


# NOWPayments redirect targets (from .env)
@short_bp.get("/deposit/crypto/success")
@login_required
def deposit_crypto_success():
    # At this point NOWPayments has redirected the user after pay.
    # The actual confirmation should come via IPN; this is just UX.
    flash("Crypto payment received or pending blockchain confirmation. We'll load your credits shortly.", "success")
    return redirect(url_for("playerbp.player_dashboard"))

@short_bp.get("/deposit/crypto/cancel")
@login_required
def deposit_crypto_cancel():
    flash("Crypto payment was cancelled or not completed.", "error")
    return redirect(url_for("playerbp.deposit_step1"))

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

    method_up = (method or "").upper()

    # 🔒 For CRYPTO / CHIME, a game is REQUIRED
    if method_up in ("CRYPTO", "CHIME") and not game_id:
        flash("Please select a game before depositing with Crypto or Chime.", "error")
        return redirect(url_for("playerbp.deposit_step1"))

    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            flash("Selected game is not available.", "error")
            return redirect(url_for("playerbp.deposit_step1"))

    return redirect(
        url_for(
            "short_bp.deposit_step2_clean",
            game_id=game_id or 0,
            method=method_up,
            amount=amount,
        )
    )
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