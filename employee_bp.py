# employee_bp.py
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from sqlalchemy import text, or_

from models import (
    db,
    User,
    Game,
    PlayerBalance,
    GameAccount,
    GameAccountRequest,
    DepositRequest,
    WithdrawRequest,   # manage cash-out requests
    notify,
)

employee_bp = Blueprint("employeebp", __name__, url_prefix="/employee")


# -------------------- guards --------------------
@employee_bp.before_request
def require_employee():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login_get", next=url_for("employeebp.employee_home")))
    if current_user.role not in ("EMPLOYEE", "ADMIN"):
        return abort(403)


def _display_name(user: User) -> str:
    """Nicer names for notifications."""
    return (user.name or user.email or f"User #{user.id}").strip()


# ---------- tiny kv fallback (shared with admin) ----------
def _ensure_kv():
    """Create kv_store if it doesn't exist (works for sqlite/mysql/postgres)."""
    try:
        bind = db.session.get_bind()
        dialect = bind.dialect.name if bind else "sqlite"
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
        else:  # sqlite or others
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

def _backend_url_for(game: Game) -> str | None:
    """Prefer Game.backend_url if present; otherwise read kv key game:{id}:backend_url."""
    val = getattr(game, "backend_url", None)
    if val:
        return val
    return _kv_get(f"game:{game.id}:backend_url") or None


# -------------------- home ----------------------
@employee_bp.get("/")
@login_required
def employee_home():
    pending_deposits  = DepositRequest.query.filter_by(status="PENDING").count()
    pending_requests  = GameAccountRequest.query.filter(
        GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"])
    ).count()
    pending_withdraws = WithdrawRequest.query.filter_by(status="PENDING").count()

    return render_template(
        "dashboard_employee.html",
        page_title="Employee",
        pending_deposits=pending_deposits,
        pending_requests=pending_requests,
        pending_withdraws=pending_withdraws,
    )


# -------------------- DEPOSITS LIST --------------------
@employee_bp.get("/deposits")
@login_required
def deposits_list():
    pending = (
        DepositRequest.query.filter_by(status="PENDING")
        .order_by(DepositRequest.created_at.desc())
        .all()
    )
    recent = (
        DepositRequest.query.filter(DepositRequest.status != "PENDING")
        .order_by(DepositRequest.created_at.desc())
        .limit(30)
        .all()
    )
    return render_template(
        "employee_deposits.html",
        page_title="Deposits",
        pending=pending,
        recent=recent,
    )


@employee_bp.post("/deposits/<int:dep_id>/loaded")
@login_required
def deposits_loaded(dep_id: int):
    dep = db.session.get(DepositRequest, dep_id)
    if not dep:
        flash("Deposit not found.", "error")
        return redirect(url_for("employeebp.deposits_list"))

    dep.status = "LOADED"
    dep.loaded_at = datetime.utcnow()

    # Credit wallet if available
    if dep.amount and dep.user_id:
        wallet = PlayerBalance.query.filter_by(user_id=dep.user_id).first()
        if wallet:
            wallet.balance = (wallet.balance or 0) + dep.amount

    db.session.commit()

    player = db.session.get(User, dep.user_id)
    pname = _display_name(player) if player else f"User #{dep.user_id}"
    notify(dep.user_id, f"✅ {pname}, your deposit #{dep.id} of {dep.amount} has been loaded.")
    flash("Deposit marked as LOADED.", "success")
    return redirect(url_for("employeebp.deposits_list"))


@employee_bp.post("/deposits/<int:dep_id>/reject")
@login_required
def deposits_reject(dep_id: int):
    dep = db.session.get(DepositRequest, dep_id)
    if not dep:
        flash("Deposit not found.", "error")
        return redirect(url_for("employeebp.deposits_list"))

    dep.status = "REJECTED"
    db.session.commit()

    player = db.session.get(User, dep.user_id)
    pname = _display_name(player) if player else f"User #{dep.user_id}"
    notify(dep.user_id, f"⚠️ {pname}, your deposit #{dep.id} was rejected. Please contact support.")
    flash("Deposit marked as REJECTED.", "success")
    return redirect(url_for("employeebp.deposits_list"))


# -------------------- GAME ACCESS REQUESTS --------------------
@employee_bp.get("/requests")
@login_required
def requests_list():
    open_reqs = (
        GameAccountRequest.query.filter(
            GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"])
        )
        .order_by(GameAccountRequest.created_at.desc())
        .all()
    )
    recent = (
        GameAccountRequest.query.filter(GameAccountRequest.status == "APPROVED")
        .order_by(GameAccountRequest.created_at.desc())
        .limit(30)
        .all()
    )
    games = {g.id: g for g in Game.query.all()}
    users = {u.id: u for u in User.query.all()}
    backend_urls = {gid: _backend_url_for(g) for gid, g in games.items()}

    return render_template(
        "employee_requests.html",
        page_title="Requests",
        open_reqs=open_reqs,
        recent=recent,
        games=games,
        users=users,
        backend_urls=backend_urls,
        selected_game=None,
    )


# Filter by one game
@employee_bp.get("/requests/game/<int:game_id>")
@login_required
def requests_list_by_game(game_id: int):
    game = db.session.get(Game, game_id)
    if not game:
        flash("Game not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    open_reqs = (
        GameAccountRequest.query.filter(
            GameAccountRequest.game_id == game_id,
            GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"])
        )
        .order_by(GameAccountRequest.created_at.desc())
        .all()
    )
    recent = (
        GameAccountRequest.query.filter(
            GameAccountRequest.game_id == game_id,
            GameAccountRequest.status == "APPROVED"
        )
        .order_by(GameAccountRequest.created_at.desc())
        .limit(30)
        .all()
    )
    games = {g.id: g for g in Game.query.all()}
    users = {u.id: u for u in User.query.all()}
    backend_urls = {gid: _backend_url_for(g) for gid, g in games.items()}

    return render_template(
        "employee_requests.html",
        page_title=f"Requests • {game.name}",
        open_reqs=open_reqs,
        recent=recent,
        games=games,
        users=users,
        backend_urls=backend_urls,
        selected_game=game,
    )


@employee_bp.post("/requests/<int:req_id>/provide")
@login_required
def requests_provide(req_id: int):
    """
    Provide credentials or save progress on a request.
    Supports 'action' in form:
      - 'save'     -> mark IN_PROGRESS (no notification)
      - 'approve'  -> save credentials & mark APPROVED (notify player)
    """
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    action   = (request.form.get("action") or "approve").lower().strip()
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    note     = (request.form.get("note") or "").strip()

    # If saving progress only, just mark IN_PROGRESS and optionally store note on req
    if action == "save":
        req.status = "IN_PROGRESS"
        if hasattr(req, "note") and note:
            req.note = note
        db.session.commit()
        flash("Progress saved. Request marked IN_PROGRESS.", "success")
        # return to the most relevant list
        if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
            return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
        return redirect(url_for("employeebp.requests_list"))

    # Default: approve (create/update GameAccount + APPROVED)
    # create/update a GameAccount row for this player+game
    acc = GameAccount.query.filter_by(user_id=req.user_id, game_id=req.game_id).first()
    if not acc:
        acc = GameAccount(user_id=req.user_id, game_id=req.game_id)
        if hasattr(acc, "request_id"):
            acc.request_id = req.id
        if hasattr(acc, "created_at") and getattr(acc, "created_at", None) is None:
            acc.created_at = datetime.utcnow()
        db.session.add(acc)
    else:
        if hasattr(acc, "request_id") and (getattr(acc, "request_id", None) in (None, 0)):
            acc.request_id = req.id

    # --- set credential fields (support multiple model variants) ---
    # username
    if hasattr(acc, "account_username"):
        acc.account_username = username
    elif hasattr(acc, "username"):
        acc.username = username
    elif hasattr(acc, "login"):
        acc.login = username

    # password
    if hasattr(acc, "account_password"):
        acc.account_password = password
    elif hasattr(acc, "password"):
        acc.password = password
    elif hasattr(acc, "passcode"):
        acc.passcode = password

    # extra / note on account
    if hasattr(acc, "extra"):
        acc.extra = note
    elif hasattr(acc, "note"):
        acc.note = note
    # also reflect note on request if field exists
    if hasattr(req, "note") and note:
        req.note = note

    # close the request
    req.status = "APPROVED"
    if hasattr(req, "approved_at") and getattr(req, "approved_at", None) is None:
        req.approved_at = datetime.utcnow()

    db.session.commit()

    # notify the player (with player name shown in the message)
    player = db.session.get(User, req.user_id)
    pname  = _display_name(player) if player else f"User #{req.user_id}"
    game   = db.session.get(Game, req.game_id)
    gname  = game.name if game else "your game"
    notify(req.user_id, f"🔐 {pname}, credentials added for {gname}. Check My Logins.")

    # Redirect back to the most relevant list
    if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
        return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
    return redirect(url_for("employeebp.requests_list"))


@employee_bp.post("/requests/<int:req_id>/reject")
@login_required
def requests_reject(req_id: int):
    """
    Reject a game access request (button sits next to Save/Approve).
    Sends a notification to the player.
    """
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    req.status = "REJECTED"
    # optional reason from form
    reason = (request.form.get("reason") or "").strip()
    if hasattr(req, "note") and reason:
        req.note = reason

    db.session.commit()

    player = db.session.get(User, req.user_id)
    pname  = _display_name(player) if player else f"User #{req.user_id}"
    game   = db.session.get(Game, req.game_id)
    gname  = game.name if game else "your game"
    msg    = f"⚠️ {pname}, your login request for {gname} was rejected."
    if reason:
        msg += f" Reason: {reason}"
    notify(req.user_id, msg)

    flash("Request rejected and player notified.", "success")
    if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
        return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
    return redirect(url_for("employeebp.requests_list"))


# Quick link to open a game's backend (redirect). Safe even if column missing — uses kv fallback.
@employee_bp.get("/games/<int:game_id>/backend")
@login_required
def open_game_backend(game_id: int):
    game = db.session.get(Game, game_id)
    if not game:
        flash("Game not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    backend_url = _backend_url_for(game)
    if backend_url:
        return redirect(backend_url)

    flash("This game has no backend URL configured.", "error")
    return redirect(url_for("employeebp.requests_list_by_game", game_id=game_id))


# -------------------- WITHDRAW REQUESTS --------------------
@employee_bp.get("/withdrawals")
@login_required
def withdrawals_list():
    """Show pending cash-out requests and a short history."""
    pending = (
        WithdrawRequest.query.filter_by(status="PENDING")
        .order_by(WithdrawRequest.created_at.desc())
        .all()
    )
    recent = (
        WithdrawRequest.query.filter(WithdrawRequest.status != "PENDING")
        .order_by(WithdrawRequest.created_at.desc())
        .limit(30)
        .all()
    )

    users   = {u.id: u for u in User.query.all()}
    games   = {g.id: g for g in Game.query.all()}
    wallets = {w.user_id: w for w in PlayerBalance.query.all()}

    return render_template(
        "employee_withdrawals.html",
        page_title="Withdrawals",
        pending=pending,
        recent=recent,
        users=users,
        games=games,
        wallets=wallets,
    )


@employee_bp.post("/withdrawals/<int:wd_id>/paid")
@login_required
def withdrawals_paid(wd_id: int):
    """Approve and mark a withdrawal as PAID. Optionally deduct a tip."""
    wd = db.session.get(WithdrawRequest, wd_id)
    if not wd:
        flash("Withdrawal not found.", "error")
        return redirect(url_for("employeebp.withdrawals_list"))

    tip = request.form.get("tip_amount", type=int) or 0
    if tip < 0:
        tip = 0

    wd.status = "PAID"
    # handle presence/absence of paid_at column safely
    if hasattr(wd, "paid_at"):
        wd.paid_at = datetime.utcnow()

    # Deduct from wallet (amount + optional tip)
    wallet = PlayerBalance.query.filter_by(user_id=wd.user_id).first()
    if wallet:
        wallet.balance = max(0, (wallet.balance or 0) - (wd.amount or 0) - tip)

    db.session.commit()

    player = db.session.get(User, wd.user_id)
    pname  = _display_name(player) if player else f"User #{wd.user_id}"
    method = wd.method or "MANUAL"
    notify(wd.user_id, f"💸 {pname}, your withdrawal #{wd.id} for {wd.amount} via {method} has been paid.")
    flash("Withdrawal marked as PAID.", "success")
    return redirect(url_for("employeebp.withdrawals_list"))


@employee_bp.post("/withdrawals/<int:wd_id>/reject")
@login_required
def withdrawals_reject(wd_id: int):
    wd = db.session.get(WithdrawRequest, wd_id)
    if not wd:
        flash("Withdrawal not found.", "error")
        return redirect(url_for("employeebp.withdrawals_list"))

    wd.status = "REJECTED"
    db.session.commit()

    player = db.session.get(User, wd.user_id)
    pname  = _display_name(player) if player else f"User #{wd.user_id}"
    notify(wd.user_id, f"⚠️ {pname}, your withdrawal #{wd.id} was rejected. Please contact support.")
    flash("Withdrawal marked as REJECTED.", "success")
    return redirect(url_for("employeebp.withdrawals_list"))


# -------------------- COMPATIBILITY ALIASES --------------------
employee_bp.add_url_rule("/game-requests", endpoint="game_requests", view_func=requests_list)
employee_bp.add_url_rule("/deposits",      endpoint="deposits",      view_func=deposits_list)

# Old endpoints some templates still reference (w_id -> wd_id)
@employee_bp.post("/withdrawals/<int:w_id>/paid", endpoint="withdrawals_mark_paid")
@login_required
def _withdrawals_mark_paid(w_id: int):
    return withdrawals_paid(w_id)

@employee_bp.post("/withdrawals/<int:w_id>/reject", endpoint="withdrawals_mark_reject")
@login_required
def _withdrawals_mark_reject(w_id: int):
    return withdrawals_reject(w_id)