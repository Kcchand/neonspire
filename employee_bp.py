# employee_bp.py
from datetime import datetime
from collections import defaultdict

from flask import Blueprint, render_template, render_template_string, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from sqlalchemy import text, or_, func, and_

from models import (
    db,
    User,
    Game,
    PlayerBalance,
    GameAccount,
    GameAccountRequest,
    DepositRequest,
    WithdrawRequest,
    ReferralCode,
    notify,
    PaymentSettings,   # <-- added for bonus %
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
    return (user.name or user.email or f"User #{user.id}").strip()


def _template_exists(name: str) -> bool:
    try:
        render_template(name)
        return True
    except Exception:
        return False


# small helper to read first existing attribute name on a row
def _first_attr(obj, *names, default=""):
    if not obj:
        return default
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v not in (None, ""):
                return v
    return default


# ---------- tiny kv fallback (shared with admin) ----------
def _ensure_kv():
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

def _backend_url_for(game: Game) -> str | None:
    val = getattr(game, "backend_url", None)
    if val:
        return val
    return _kv_get(f"game:{game.id}:backend_url") or None


def _refcodes_for_user_ids(user_ids):
    if not user_ids:
        return {}
    rows = ReferralCode.query.filter(ReferralCode.user_id.in_(list(set(user_ids)))).all()
    return {r.user_id: r.code for r in rows}


# -------------------- home ----------------------
@employee_bp.get("/")
@login_required
def employee_home():
    pending_deposits  = DepositRequest.query.filter_by(status="PENDING").count()
    pending_requests  = GameAccountRequest.query.filter(
        GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"])
    ).count()
    pending_withdraws = WithdrawRequest.query.filter_by(status="PENDING").count()

    recent_players = (
        User.query.filter_by(role="PLAYER")
        .order_by(User.created_at.desc())
        .limit(25)
        .all()
    )
    player_ids = [p.id for p in recent_players]

    refcodes = _refcodes_for_user_ids(player_ids)

    accounts_count = defaultdict(int)
    if player_ids:
        for uid, cnt in (
            db.session.query(GameAccount.user_id, func.count(GameAccount.id))
            .filter(GameAccount.user_id.in_(player_ids))
            .group_by(GameAccount.user_id)
            .all()
        ):
            accounts_count[uid] = int(cnt or 0)

    last_req_by_user = {}
    if player_ids:
        sub = (
            db.session.query(
                GameAccountRequest.user_id,
                func.max(GameAccountRequest.created_at).label("last_dt")
            )
            .filter(GameAccountRequest.user_id.in_(player_ids))
            .group_by(GameAccountRequest.user_id)
        ).subquery()

        for u_id, g_id, _dt in (
            db.session.query(
                GameAccountRequest.user_id,
                GameAccountRequest.game_id,
                GameAccountRequest.created_at
            )
            .join(sub, (sub.c.user_id == GameAccountRequest.user_id) & (sub.c.last_dt == GameAccountRequest.created_at))
            .all()
        ):
            last_req_by_user[u_id] = g_id

    games_map = {g.id: g for g in Game.query.all()}

    player_rows = []
    for p in recent_players:
        g_last_id = last_req_by_user.get(p.id)
        g_last = games_map.get(g_last_id) if g_last_id else None
        player_rows.append({
            "id": p.id,
            "name": (p.name or "").strip() or None,
            "email": (p.email or "").strip(),
            "mobile": (p.mobile or "").strip() or None,
            "joined": p.created_at,
            "refcode": refcodes.get(p.id, None),
            "games_count": accounts_count.get(p.id, 0),
            "last_game": g_last.name if g_last else None,
            "last_game_id": g_last.id if g_last else None,
        })

    return render_template(
        "dashboard_employee.html",
        page_title="Employee",
        pending_deposits=pending_deposits,
        pending_requests=pending_requests,
        pending_withdraws=pending_withdraws,
        players=player_rows,
        games=games_map,
    )


# -------------------- DEPOSITS LIST --------------------
@employee_bp.get("/deposits")
@login_required
def deposits_list():
    """
    - If a valid status is chosen (PENDING/RECEIVED/LOADED/REJECTED) or `q` is provided,
      return a unified filtered list as `items` (template uses the 'items' branch).
    - Otherwise, return the legacy split view: `pending` + `recent`, plus enriched rows for
      inline account details (pending_rows/recent_rows).
    """
    ALLOWED = {"PENDING", "RECEIVED", "LOADED", "REJECTED"}
    status = (request.args.get("status") or "").upper().strip()
    q = (request.args.get("q") or "").strip()

    settings = db.session.get(PaymentSettings, 1)

    # ---- Filtered path (unified list) ----
    if status in ALLOWED or q:
        base = DepositRequest.query

        if status in ALLOWED:
            base = base.filter(DepositRequest.status == status)

        if q:
            # Search: user name/email or deposit id
            base = (
                base.outerjoin(User, User.id == DepositRequest.user_id)
                    .filter(
                        or_(
                            func.lower(func.coalesce(User.name, "")).like(f"%{q.lower()}%"),
                            func.lower(func.coalesce(User.email, "")).like(f"%{q.lower()}%"),
                            func.cast(DepositRequest.id, db.String).ilike(f"%{q}%"),
                        )
                    )
            )

        items = base.order_by(DepositRequest.created_at.desc()).all()

        user_ids = [d.user_id for d in items if d.user_id]
        users_map = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
        refcodes = _refcodes_for_user_ids(user_ids)

        return render_template(
            "employee_deposits.html",
            page_title="Deposits",
            items=items,               # <- template will render the unified 'items' table
            users=users_map,
            refcodes=refcodes,
            status=status,
            q=q,
            settings=settings,
        )

    # ---- Legacy split view (no filter/query) ----
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

    # Users & refcodes
    user_ids = [d.user_id for d in (pending + recent) if d.user_id]
    users_map = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    refcodes = _refcodes_for_user_ids(user_ids)

    # ---------- Enriched rows with Game + GameAccount (login details) ----------
    def build_rows(deps):
        if not deps:
            return []

        u_ids = list({d.user_id for d in deps if d.user_id})
        g_ids = list({d.game_id for d in deps if d.game_id})

        users = {u.id: u for u in User.query.filter(User.id.in_(u_ids)).all()} if u_ids else {}
        games = {g.id: g for g in Game.query.filter(Game.id.in_(g_ids)).all()} if g_ids else {}

        # pull all accounts for (user_id, game_id) pairs
        accounts = {}
        if u_ids and g_ids:
            rows = (
                GameAccount.query
                .filter(GameAccount.user_id.in_(u_ids))
                .filter(GameAccount.game_id.in_(g_ids))
                .all()
            )
            for a in rows:
                accounts[(a.user_id, a.game_id)] = a

        out = []
        for d in deps:
            u = users.get(d.user_id)
            g = games.get(d.game_id) if d.game_id else None
            acc = accounts.get((d.user_id, d.game_id)) if d.user_id and d.game_id else None
            out.append({
                "dep": d,
                "user_name": _display_name(u) if u else f"User #{d.user_id}" if d.user_id else "—",
                "game_name": g.name if g else ("—" if d.game_id is None else f"#{d.game_id}"),
                "login_user": _first_attr(acc, "account_username", "username", "login", "user", default=""),
                "login_pass": _first_attr(acc, "account_password", "password", "passcode", "pin", default=""),
                "login_note": _first_attr(acc, "extra", "note", "notes", "remark", default=""),
            })
        return out

    pending_rows = build_rows(pending)
    recent_rows  = build_rows(recent)

    return render_template(
        "employee_deposits.html",
        page_title="Deposits",
        # original context (kept)
        pending=pending,
        recent=recent,
        users=users_map,
        refcodes=refcodes,
        status="",
        q="",
        settings=settings,
        # new enriched rows (for showing login details nicely, if your template uses them)
        pending_rows=pending_rows,
        recent_rows=recent_rows,
    )


# New: Deposit detail page
@employee_bp.get("/deposits/<int:dep_id>", endpoint="deposit_detail")
@login_required
def deposit_detail(dep_id: int):
    dep = db.session.get(DepositRequest, dep_id)
    if not dep:
        flash("Deposit not found.", "error")
        return redirect(url_for("employeebp.deposits_list"))

    user = db.session.get(User, dep.user_id) if dep.user_id else None
    game = db.session.get(Game, dep.game_id) if dep.game_id else None
    refcode = None
    if dep.user_id:
        rc = ReferralCode.query.filter_by(user_id=dep.user_id).first()
        refcode = rc.code if rc else None

    ctx = dict(
        page_title=f"Deposit #{dep.id}",
        dep=dep,
        user=user,
        game=game,
        refcode=refcode,
    )

    if _template_exists("employee_deposit_detail.html"):
        return render_template("employee_deposit_detail.html", **ctx)

    return render_template_string("""{% extends "base.html" %}{% block content %}
    <div class="shell">
      <div class="panel" style="display:flex;align-items:center;justify-content:space-between">
        <div class="h3">Deposit #{{ dep.id }}</div>
        <a class="btn" href="{{ url_for('employeebp.deposits_list') }}">← Back to Deposits</a>
      </div>
      <div class="panel">
        <div class="grid-2" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
          <div>
            <div class="muted">Player</div>
            <div class="h4">{{ user.name if user else ('User #' ~ dep.user_id) }}</div>
            {% if user and user.email %}<div class="muted">{{ user.email }}</div>{% endif %}
            {% if refcode %}<div style="margin-top:6px"><strong>Referral Code:</strong> {{ refcode }}</div>{% endif %}
            {% if game %}<div style="margin-top:6px"><strong>Game:</strong> {{ game.name }}</div>{% endif %}
          </div>
          <div>
            <div><strong>Amount:</strong> {{ dep.amount }}</div>
            <div><strong>Method:</strong> {{ dep.method }}</div>
            <div><strong>Status:</strong> {{ dep.status }}</div>
            <div><strong>Created:</strong> {{ dep.created_at }}</div>
            {% if dep.loaded_at %}<div><strong>Loaded at:</strong> {{ dep.loaded_at }}</div>{% endif %}
          </div>
        </div>
        {% if dep.proof_url %}
          <hr>
          <div>
            <div class="muted">Payment Proof</div>
            <p><a class="btn" target="_blank" rel="noopener" href="{{ dep.proof_url }}">Open Proof</a></p>
            {% set is_img = dep.proof_url.lower().endswith(('.png','.jpg','.jpeg','.webp','.gif')) %}
            {% if is_img %}<div style="max-width:480px"><img src="{{ dep.proof_url }}" alt="Payment proof" style="max-width:100%;border-radius:10px"></div>{% endif %}
          </div>
        {% endif %}
        <hr>
        <div style="display:flex;gap:8px">
          <form method="post" action="{{ url_for('employeebp.deposits_loaded', dep_id=dep.id) }}"><button class="btn btn-primary" type="submit">Approve &nbsp;✓</button></form>
          <form method="post" action="{{ url_for('employeebp.deposits_reject', dep_id=dep.id) }}"><button class="btn" type="submit">Reject ✕</button></form>
        </div>
      </div>
    </div>{% endblock %}""", **ctx)


@employee_bp.post("/deposits/<int:dep_id>/loaded")
@login_required
def deposits_loaded(dep_id: int):
    dep = db.session.get(DepositRequest, dep_id)
    if not dep:
        flash("Deposit not found.", "error")
        return redirect(url_for("employeebp.deposits_list"))

    dep.status = "LOADED"
    dep.loaded_at = datetime.utcnow()
    if hasattr(dep, "loaded_by"):
        dep.loaded_by = current_user.id

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

    user_ids = [r.user_id for r in (open_reqs + recent)]
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    refcodes = _refcodes_for_user_ids(user_ids)

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
        refcodes=refcodes,
    )


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

    user_ids = [r.user_id for r in (open_reqs + recent)]
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    refcodes = _refcodes_for_user_ids(user_ids)

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
        refcodes=refcodes,
    )


@employee_bp.post("/requests/<int:req_id>/provide")
@login_required
def requests_provide(req_id: int):
    """
    Save credentials (or progress) for a request. On approve:
    - ensure a GameAccount exists
    - write the credentials
    - ALWAYS stamp the issuer (so admin can see who created it)
    - close the request and stamp approver/handler
    """
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    action   = (request.form.get("action") or "approve").lower().strip()
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    note     = (request.form.get("note") or "").strip()

    # Save progress only
    if action == "save":
        req.status = "IN_PROGRESS"
        if hasattr(req, "note") and note:
            req.note = note
        if hasattr(req, "handled_by"):
            req.handled_by = current_user.id
        if hasattr(req, "updated_at"):
            req.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Progress saved. Request marked IN_PROGRESS.", "success")
        if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
            return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
        return redirect(url_for("employeebp.requests_list"))

    # Approve: create/update account
    acc = GameAccount.query.filter_by(user_id=req.user_id, game_id=req.game_id).first()
    if not acc:
        acc = GameAccount(user_id=req.user_id, game_id=req.game_id)
        # ensure created_at for new rows if the column exists
        if hasattr(acc, "created_at") and not getattr(acc, "created_at", None):
            acc.created_at = datetime.utcnow()
        db.session.add(acc)

    # Always link request
    if hasattr(acc, "request_id"):
        acc.request_id = req.id

    # credentials
    if hasattr(acc, "account_username"):
        acc.account_username = username
    elif hasattr(acc, "username"):
        acc.username = username
    elif hasattr(acc, "login"):
        acc.login = username

    if hasattr(acc, "account_password"):
        acc.account_password = password
    elif hasattr(acc, "password"):
        acc.password = password
    elif hasattr(acc, "passcode"):
        acc.passcode = password

    # note
    if hasattr(acc, "extra"):
        acc.extra = note
    elif hasattr(acc, "note"):
        acc.note = note
    if hasattr(req, "note") and note:
        req.note = note

    # 🔴 ALWAYS stamp issuer (so admin "Issued By" works)
    if hasattr(acc, "issued_by_id"):
        acc.issued_by_id = current_user.id
    # always stamp time if the column exists
    if hasattr(acc, "issued_at"):
        acc.issued_at = datetime.utcnow()

    # close request + stamp approver/handler variants
    req.status = "APPROVED"
    if hasattr(req, "approved_by_id"):
        req.approved_by_id = current_user.id
    if hasattr(req, "approved_by"):
        req.approved_by = current_user.id
    if hasattr(req, "handled_by"):
        req.handled_by = current_user.id
    if hasattr(req, "approved_at"):
        req.approved_at = datetime.utcnow()
    if hasattr(req, "updated_at"):
        req.updated_at = datetime.utcnow()

    db.session.commit()

    player = db.session.get(User, req.user_id)
    pname  = _display_name(player) if player else f"User #{req.user_id}"
    game   = db.session.get(Game, req.game_id)
    gname  = game.name if game else "your game"
    notify(req.user_id, f"🔐 {pname}, credentials added for {gname}. Check My Logins.")

    if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
        return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
    return redirect(url_for("employeebp.requests_list"))


@employee_bp.post("/requests/<int:req_id>/reject")
@login_required
def requests_reject(req_id: int):
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    req.status = "REJECTED"
    reason = (request.form.get("reason") or "").strip()
    if hasattr(req, "note") and reason:
        req.note = reason
    if hasattr(req, "handled_by"):
        req.handled_by = current_user.id
    if hasattr(req, "updated_at"):
        req.updated_at = datetime.utcnow()

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


# Quick link to open a game's backend
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
    wd = db.session.get(WithdrawRequest, wd_id)
    if not wd:
        flash("Withdrawal not found.", "error")
        return redirect(url_for("employeebp.withdrawals_list"))

    tip = request.form.get("tip_amount", type=int) or 0
    if tip < 0:
        tip = 0

    wd.status = "PAID"
    if hasattr(wd, "paid_at"):
        wd.paid_at = datetime.utcnow()

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

@employee_bp.post("/withdrawals/<int:w_id>/paid", endpoint="withdrawals_mark_paid")
@login_required
def _withdrawals_mark_paid(w_id: int):
    return withdrawals_paid(w_id)

@employee_bp.post("/withdrawals/<int:w_id>/reject", endpoint="withdrawals_mark_reject")
@login_required
def _withdrawals_mark_reject(w_id: int):
    return withdrawals_reject(w_id)