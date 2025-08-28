from datetime import datetime
import re
import random
from flask import Blueprint, render_template, render_template_string, request, redirect, url_for, flash, abort
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
    ReferralCode,   # <-- REFERRALS
    notify,
)

# ---------------------------------------------
# Blueprints
# ---------------------------------------------
# Existing player blueprint (kept so nothing breaks)
player_bp = Blueprint("playerbp", __name__, url_prefix="/player")

# NEW: Clean, no-prefix blueprint for short professional URLs
# Register this in app.py:  app.register_blueprint(short_bp)
short_bp = Blueprint("short_bp", __name__, url_prefix="")

# ---------- tiny kv fallback (no migration needed) ----------
def _ensure_kv():
    try:
        bind = db.session.get_bind()
        dialect = bind.dialect.name
        if dialect in ("postgresql", "postgres"):
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """))
        elif dialect in ("mysql", "mariadb"):
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    `key` VARCHAR(191) PRIMARY KEY,
                    `value` TEXT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
        else:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """))
        db.session.commit()
    except Exception:
        db.session.rollback()

def _kv_get(k: str) -> str | None:
    _ensure_kv()
    try:
        row = db.session.execute(text("SELECT value FROM kv_store WHERE key=:k"), {"k": k}).fetchone()
        return row[0] if row else None
    except Exception:
        db.session.rollback()
        return None

def _kv_first(*keys, default: str | None = None) -> str | None:
    for k in keys:
        v = _kv_get(k)
        if v not in (None, ""):
            return v
    return default

# ---------- helpers ----------
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
    """Gracefully fall back if a template file hasn't been created yet."""
    try:
        render_template(name)  # will raise if missing
        return True
    except Exception:
        return False

# alias sets (match admin)
PROMO1_ALIASES = ("promo_line1", "news_line1", "ticker_line1", "headline1", "news1")
PROMO2_ALIASES = ("promo_line2", "news_line2", "ticker_line2", "headline2", "news2")
TREND_ALIASES  = ("trending_game_ids", "trending_ids", "trending_csv", "trending")

# ---------- small internal render helpers (used by both blueprints) ----------
def _render_deposit_step1(preselect_game_id: int | None):
    if not _player_like():
        return abort(403)
    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    return render_template(
        "player_deposit_step1.html",
        games=games,
        preselect_game_id=preselect_game_id,
        page_title="Deposit • Step 1"
    )

def _render_deposit_step2(game_id: int | None, method: str, amount: int):
    if not _player_like():
        return abort(403)
    if not amount or amount <= 0:
        flash("Start over and enter a valid amount.", "error")
        return redirect(url_for("playerbp.deposit_step1"))
    method = (method or "CRYPTO").upper()
    if method not in ("CRYPTO", "CHIME"):
        flash("Invalid payment method.", "error")
        return redirect(url_for("playerbp.deposit_step1"))
    game = db.session.get(Game, game_id) if game_id else None
    settings = _get_settings()
    return render_template(
        "player_deposit_step2.html",
        amount=amount,
        method=method,
        game=game,
        settings=settings,
        page_title="Deposit • Step 2"
    )

def _render_withdraw(game_id: int | None):
    if not _player_like():
        return abort(403)
    settings = _get_settings()
    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    return render_template(
        "player_withdraw.html",
        settings=settings,
        games=games,
        preselect_game_id=game_id,
        page_title="Withdraw • NeonSpire Casino"
    )

# ---------- landing ----------
@player_bp.get("")
@login_required
def player_root():
    return redirect(url_for("playerbp.player_dashboard"))

# ---------- PLAYER DASHBOARD ----------
@player_bp.get("/dashboard")
@login_required
def player_dashboard():
    if not _player_like():
        return abort(403)

    settings = _get_settings()
    wallet = _ensure_wallet(current_user.id)
    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()

    my_accounts = GameAccount.query.filter_by(user_id=current_user.id).all()
    accounts_by_game = {}
    for acc in my_accounts:
        accounts_by_game.setdefault(getattr(acc, "game_id", None), []).append(acc)

    notifications = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(10)
        .all()
    )

    # --- promos (aliases + kv fallback)
    promo_line1 = _first_attr(settings, *PROMO1_ALIASES, default=None)
    if promo_line1 in (None, ""):
        promo_line1 = _kv_first(*PROMO1_ALIASES, default="")

    promo_line2 = _first_attr(settings, *PROMO2_ALIASES, default=None)
    if promo_line2 in (None, ""):
        promo_line2 = _kv_first(*PROMO2_ALIASES, default="")

    bonus_percent = getattr(settings, "bonus_percent", None)
    if bonus_percent in (None, ""):
        bp = _kv_first("bonus_percent")
        bonus_percent = int(bp) if (bp and str(bp).isdigit()) else 0

    # --- trending (aliases + kv fallback, preserve order)
    raw_csv = _first_attr(settings, *TREND_ALIASES, default=None)
    if raw_csv in (None, ""):
        raw_csv = _kv_first(*TREND_ALIASES, default="") or ""
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
        "dashboard_player.html",  # <- your template
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

# ---------- REQUEST GAME ACCOUNT ----------
@player_bp.post("/game/<int:game_id>/request-account")
@login_required
def request_game_account(game_id: int):
    if not _player_like():
        return abort(403)

    game = db.session.get(Game, game_id)
    if not game or not game.is_active:
        flash("Game not available.", "error")
        return redirect(url_for("index"))  # redirect to lobby

    exists = (
        GameAccountRequest.query.filter_by(user_id=current_user.id, game_id=game_id)
        .filter(GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"]))
        .first()
    )
    if exists:
        flash("You already have an open request for this game.", "error")
        return redirect(url_for("index"))

    req = GameAccountRequest(
        user_id=current_user.id,
        game_id=game_id,
        status="PENDING",
        created_at=datetime.utcnow(),
    )
    db.session.add(req)
    db.session.commit()

    notify(current_user.id, f"Your access request for {game.name} is being processed.")
    for staff in User.query.filter(User.role.in_(("EMPLOYEE", "ADMIN"))).all():
        pname = current_user.name or current_user.email or f"Player #{current_user.id}"
        notify(staff.id, f"New game access request: {game.name} by {pname}")

    flash("Request submitted. You’ll receive credentials shortly.", "success")
    return redirect(url_for("index"))

# =============  LEGACY DEPOSIT — 2 steps (kept)  =============
@player_bp.get("/deposit/step1")
@login_required
def deposit_step1():
    pre_id = request.args.get("game_id", type=int)
    return _render_deposit_step1(pre_id)

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
    if method not in ("CRYPTO", "CHIME"):
        flash("Invalid payment method.", "error")
        return redirect(url_for("playerbp.deposit_step1"))
    game_id_val = request.form.get("game_id")
    game_id = int(game_id_val) if game_id_val and game_id_val.isdigit() else None
    if game_id:
        g = db.session.get(Game, game_id)
        if not g or not g.is_active:
            flash("Selected game is not available.", "error")
            return redirect(url_for("playerbp.deposit_step1"))
    # redirect to CLEAN step2 path:
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
    game_id = int(game_id_val) if game_id_val and game_id_val.isdigit() else None
    proof_url = (request.form.get("proof_url") or "").strip()
    screenshot_url = (request.form.get("screenshot_url") or "").strip()
    if amount <= 0 or method not in ("CRYPTO", "CHIME"):
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
        proof_url=proof_url or screenshot_url,
        status="PENDING",
        created_at=datetime.utcnow(),
    )
    db.session.add(dep)
    db.session.commit()
    target = f" for game #{game_id}" if game_id else ""
    extra = f" | proof: {proof_url or screenshot_url}" if (proof_url or screenshot_url) else ""
    for staff in User.query.filter(User.role.in_(("EMPLOYEE", "ADMIN"))).all():
        pname = current_user.name or current_user.email or f"Player #{current_user.id}"
        notify(staff.id, f"Deposit request #{dep.id}{target} — {pname}: {amount} via {method}{extra}")
    notify(current_user.id, f"Deposit request #{dep.id} submitted. Your credits will appear shortly.")
    flash("Deposit submitted. We’ll notify you once it’s loaded.", "success")
    return redirect(url_for("playerbp.player_dashboard"))

# =============  WITHDRAW (kept)  =============
@player_bp.get("/withdraw")
@login_required
def withdraw_get():
    return _render_withdraw(game_id=None)

@player_bp.post("/withdraw")
@login_required
def withdraw_post():
    if not _player_like():
        return abort(403)
    total_amount = request.form.get("total_amount", type=int) or 0
    keep_amount  = request.form.get("keep_amount", type=int) or 0
    tip_amount   = request.form.get("tip_amount", type=int) or 0
    amount = request.form.get("amount", type=int) or 0
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
    pname = current_user.name or current_user.email or f"Player #{current_user.id}"
    parts = [
        f"Withdraw request #{wr.id} from {pname}",
        f"total={total_amount}",
        f"keep={keep_amount}",
        f"tip={tip_amount}",
        f"cashout={amount}",
        f"via {method}"
    ]
    if game_id:
        parts.append(f"(game #{game_id})")
    if address:
        parts.append(f"dest: {address}")
    staff_msg = " | ".join(parts)
    for staff in User.query.filter(User.role.in_(("EMPLOYEE", "ADMIN"))).all():
        notify(staff.id, staff_msg)
    notify(current_user.id, f"Withdrawal request #{wr.id} submitted. You’ll be notified once processed.")
    flash("Withdrawal request submitted. You’ll be notified once processed.", "success")
    return redirect(url_for("playerbp.withdraw_get"))

# =============  NOTIFICATIONS: mark read  =============
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

# =============  MY LOGINS (accounts + requests)  =============
@player_bp.get("/logins", endpoint="accounts_page")
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
    reqs = []
    for req, game_name in raw_reqs:
        reqs.append({
            "game": game_name,
            "status": req.status or "PENDING",
            "created_at": req.created_at,
        })
    return render_template("player_accounts.html", accounts=accounts, requests=reqs, page_title="My Logins • NeonSpire Casino")

# ---------- Endpoint aliases (keep old links working) ----------
player_bp.add_url_rule("/my-logins", endpoint="logins", view_func=accounts_page)
player_bp.add_url_rule("/deposit", endpoint="deposit_get", view_func=deposit_step1)

# =================================================================
#                          REFERRALS
# =================================================================

def _generate_ref_code_for(user: User) -> str:
    """Two letters from name (A-Z) + 4 digits => 6 chars total, unique."""
    base = "".join(re.findall(r"[A-Za-z]", (user.name or "").strip()))[:2].upper()
    if len(base) < 2:
        base = (base + "XX")[:2]
    # loop until a unique code is found
    for _ in range(50):
        num = random.randint(0, 9999)
        code = f"{base}{num:04d}"
        if not ReferralCode.query.filter_by(code=code).first():
            return code
    # extreme fallback with random prefix
    return f"RX{random.randint(0, 9999):04d}"

def _ensure_referral_for(user_id: int) -> ReferralCode:
    rc = ReferralCode.query.filter_by(user_id=user_id).first()
    if rc:
        return rc
    user = db.session.get(User, user_id)
    code = _generate_ref_code_for(user)
    rc = ReferralCode(user_id=user_id, code=code, created_at=datetime.utcnow())
    db.session.add(rc)
    db.session.commit()
    return rc

@player_bp.get("/referral")
@login_required
def referral_home():
    """Player-facing page that shows their code and a shareable link."""
    if not _player_like():
        return abort(403)
    rc = _ensure_referral_for(current_user.id)
    # share link points to a public landing under this blueprint
    share_url = url_for("playerbp.referral_landing", code=rc.code, _external=True)

    if _template_exists("player_referral.html"):
        return render_template("player_referral.html",
                               page_title="Referral • NeonSpire Casino",
                               code=rc.code,
                               link=share_url)
    # Inline fallback
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
        <p class="muted" style="margin-top:10px">Format is first 2 letters of your name + 4 digits, so staff can recognize who referred.</p>
      </div>
    </div>
    {% endblock %}
    """, code=rc.code, link=share_url)

# ---- NEW: JSON endpoint used by the lobby popup "Get My Link" ----
@player_bp.get("/referral/my-link.json", endpoint="referral_my_link_json")
@login_required
def referral_my_link_json():
    """Return the player's referral code and full link as JSON."""
    if not _player_like():
        return abort(403)
    rc = _ensure_referral_for(current_user.id)
    share_url = url_for("playerbp.referral_landing", code=rc.code, _external=True)
    return {"code": rc.code, "link": share_url}, 200

@player_bp.post("/referral/new")
@login_required
def referral_new_code():
    """Optional: allow a player to rotate their code (keeps unique)."""
    if not _player_like():
        return abort(403)
    rc = ReferralCode.query.filter_by(user_id=current_user.id).first()
    if not rc:
        rc = _ensure_referral_for(current_user.id)
        flash("Referral code created.", "success")
        return redirect(url_for("playerbp.referral_home"))

    # generate a new unique code and save
    rc.code = _generate_ref_code_for(current_user)
    rc.created_at = datetime.utcnow()
    db.session.commit()
    flash("Referral code updated.", "success")
    return redirect(url_for("playerbp.referral_home"))

@player_bp.get("/ref/<string:code>")
def referral_landing(code: str):
    """
    Public landing for a referral link (kept under /player).
    """
    code = (code or "").strip().upper()
    rc = ReferralCode.query.filter_by(code=code).first()
    if not rc:
        flash("Referral code not found.", "error")
        return redirect(url_for("auth.register_get"))
    # (Optional) track usage
    if hasattr(rc, "uses"):
        rc.uses = (rc.uses or 0) + 1
        db.session.commit()
    return redirect(url_for("auth.register_get") + f"?ref={code}")

# -----------------------------------------------------------
# CLEAN, SHORT ROUTES (no /player prefix) via short_bp
# -----------------------------------------------------------

# Your existing clean routes (kept)
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
    return accounts_page()

# -------- EXTRA short aliases you requested (do not break anything) --------
# 1) /dep  (Deposit Step 1 - no preselect)
@short_bp.get("/dep")
@login_required
def dep_root():
    return redirect(url_for("playerbp.deposit_step1"))

# 1b) /dep/<game_id> (Deposit Step 1 with preselect)
@short_bp.get("/dep/<int:game_id>")
@login_required
def dep_with_game(game_id: int):
    # just reuse clean deposit step1
    return redirect(url_for("short_bp.deposit_step1_clean", game_id=game_id))

# 2) /dep/<method>?amount=&game_id=   (Deposit Step 2 short)
#    Example: /dep/CRYP?amount=10&game_id=12
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
    return redirect(url_for("short_bp.deposit_step2_clean",
                            game_id=game_id or 0, method=method.upper(), amount=amount))

# 3) /withd  (Withdraw page — optional ?game_id=)
@short_bp.get("/withd")
@login_required
def withd_short():
    game_id = request.args.get("game_id", type=int)
    if game_id:
        return redirect(url_for("short_bp.withdraw_clean", game_id=game_id))
    return _render_withdraw(game_id=None)

# 4) /log   (My Logins)
@short_bp.get("/log")
@login_required
def log_short():
    return redirect(url_for("playerbp.accounts_page"))

# 5) /reg   (Register)
@short_bp.get("/reg")
def reg_short():
    return redirect(url_for("auth.register_get"))

# 6) /lob   (Lobby)
@short_bp.get("/lob")
def lob_short():
    return redirect(url_for("index"))