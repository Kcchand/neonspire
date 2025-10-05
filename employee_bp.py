# employee_bp.py
from datetime import datetime
from collections import defaultdict
import asyncio

from flask import (
    Blueprint,
    render_template,
    render_template_string,
    request,
    redirect,
    url_for,
    flash,
    abort,
    jsonify,
)
from flask_login import login_required, current_user
from sqlalchemy import text, or_, func

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
    PaymentSettings,   # bonus %, limits, etc.
    Deposit,           # alias to DepositRequest
)

# ====== Silent mode switch (prevents any external backend UI redirects) ======
SILENT_BACKEND_UI = True

# ===== GameVault automation (credit/redeem) =====
GV_IMPORT_ERROR = None
try:
    from gamevault_automation import gv_credit, gv_redeem
except Exception as e:  # pragma: no cover
    gv_credit = None
    gv_redeem = None
    GV_IMPORT_ERROR = e

# ===== JUWA automation =====
# ===== JUWA automation =====
import logging, traceback
try:
    from automation.juwa_api import recharge_sync, redeem_sync  # recharge/redeem
except Exception as err:  # pragma: no cover
    logging.error("JUWA import failed: %s", err)
    logging.debug("Traceback:\n%s", traceback.format_exc())

    def recharge_sync(*a, **k):  # soft fallback with detail
        return {"ok": False, "error": f"juwa_api import failed: {err.__class__.__name__}: {err}"}

    def redeem_sync(*a, **k):
        return {"ok": False, "error": f"juwa_api import failed: {err.__class__.__name__}: {err}"}

# ===== MILKYWAY automation (UI bot) =====
try:
    from automation.milkyway_ui_bot import (
        mw_recharge as mw_recharge,   # (account, amount, note="")
        mw_redeem as mw_redeem,       # (account, amount, note="")
        mw_create_player_auto as mw_create_user,  # optional auto-create
    )
except Exception:
    def mw_recharge(*a, **k):
        return {"ok": False, "error": "milkyway_ui_bot.mw_recharge not available"}
    def mw_redeem(*a, **k):
        return {"ok": False, "error": "milkyway_ui_bot.mw_redeem not available"}
    def mw_create_user(*a, **k):
        return {"ok": False, "error": "milkyway_ui_bot.mw_create_player_auto not available"}

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

def _safe_notify(user_id: int, message: str):
    """Best-effort notify that never crashes the request."""
    try:
        notify(user_id, message)
    except Exception:
        pass

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

# -------------------- UI helpers --------------------
def _run_maybe_async(func, *args, **kwargs):
    """Call `func` whether it's sync or async."""
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(*args, **kwargs))
    res = func(*args, **kwargs)
    if asyncio.iscoroutine(res):
        return asyncio.run(res)
    return res

def _get_login_username(user_id: int, game_id: int | None) -> str | None:
    """
    Reads the player's saved credentials for a specific game.
    Returns the username/login field or None.
    """
    if not game_id:
        return None
    acc = GameAccount.query.filter_by(user_id=user_id, game_id=game_id).first()
    if not acc:
        return None
    return (
        _first_attr(acc, "account_username", "username", "login", "user", default="")
        or None
    )

# -------- vendor helpers (JUWA vs GameVault vs Milkyway) --------
def _vendor_from_game(game: Game) -> str | None:
    if not game or not getattr(game, "name", None):
        return None
    n = (game.name or "").lower()
    if "juwa" in n:
        return "juwa"
    if "gamevault" in n or "game vault" in n or n.strip() in {"gv", "gvault"}:
        return "gv"
    if "milkyway" in n or "milky" in n:
        return "milkyway"
    return None

def _vendor_for_game(game: Game | None) -> str | None:
    """
    Same as _vendor_from_game, but also allows an explicit 'vendor' field
    or a kv override game:{id}:vendor -> juwa|gv|milkyway.
    """
    if not game:
        return None
    v = getattr(game, "vendor", None)
    if v:
        v = str(v).strip().lower()
        if v in ("juwa", "gv", "gamevault", "milkyway", "milky"):
            if v in ("gv", "gamevault"):
                return "gv"
            if v in ("milkyway", "milky"):
                return "milkyway"
            return "juwa"
    kvv = _kv_get(f"game:{game.id}:vendor")
    if kvv:
        kvv = kvv.strip().lower()
        if kvv in ("juwa", "gv", "gamevault", "milkyway", "milky"):
            if kvv in ("gv", "gamevault"):
                return "gv"
            if kvv in ("milkyway", "milky"):
                return "milkyway"
            return "juwa"
    return _vendor_from_game(game)

def _find_account_username(user_id: int, vendor_preference: str | None = None) -> tuple[str | None, str | None, int | None]:
    """
    Try to find a player's login username and vendor from their saved GameAccount rows.
    Returns (username, vendor, game_id).
    Prefers vendor_preference if provided: 'juwa' | 'gv' | 'milkyway'.
    """
    accounts = GameAccount.query.filter_by(user_id=user_id).all()
    if not accounts:
        return (None, None, None)
    options = []
    for acc in accounts:
        gid = getattr(acc, "game_id", None)
        g = db.session.get(Game, gid) if gid else None
        vendor = _vendor_from_game(g)
        if not vendor:
            continue
        uname = _first_attr(acc, "account_username", "username", "login", "user", default="") or None
        if uname:
            options.append((vendor, uname, gid))
    if not options:
        return (None, None, None)
    if vendor_preference:
        for v, u, gid in options:
            if v == vendor_preference:
                return (u, v, gid)
    for pref in ("juwa", "gv", "milkyway"):
        for v, u, gid in options:
            if v == pref:
                return (u, v, gid)
    v, u, gid = options[0]
    return (u, v, gid)

# ------- vendor result normalization -------
def _result_ok(vendor: str | None, res) -> bool:
    """
    Normalize success across different automation wrappers.
    - dicts: ok/true/success/status=ok
    - True: success
    - None: treat as success for Milkyway UI bot (often returns None)
    - strings containing 'ok'/'success' are success
    """
    if isinstance(res, dict):
        if res.get("ok") is True:
            return True
        if str(res.get("status", "")).lower() in {"ok", "success", "200"}:
            return True
        if res.get("success") is True:
            return True
    if res is True:
        return True
    if res is None and vendor == "milkyway":
        return True
    if isinstance(res, str) and any(k in res.lower() for k in ("ok", "success", "done", "loaded", "credited", "redeemed")):
        return True
    return False

def _result_error_text(res) -> str:
    if isinstance(res, dict):
        return str(res.get("error") or res)
    return str(res)

# -------------------- HOME ----------------------
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
    ALLOWED = {"PENDING", "RECEIVED", "LOADED", "REJECTED"}
    status = (request.args.get("status") or "").upper().strip()
    q = (request.args.get("q") or "").strip()

    settings = db.session.get(PaymentSettings, 1)

    if status in ALLOWED or q:
        base = DepositRequest.query

        if status in ALLOWED:
            base = base.filter(DepositRequest.status == status)

        if q:
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

        game_ids = list({d.game_id for d in items if d.game_id})
        games_map = {g.id: g for g in Game.query.filter(Game.id.in_(game_ids)).all()} if game_ids else {}

        backend_urls = {gid: _backend_url_for(g) for gid, g in games_map.items()}

        return render_template(
            "employee_deposits.html",
            page_title="Deposits",
            items=items,
            users=users_map,
            refcodes=refcodes,
            status=status,
            q=q,
            settings=settings,
            games=games_map,
            backend_urls=backend_urls,
        )

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

    user_ids = [d.user_id for d in (pending + recent) if d.user_id]
    users_map = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    refcodes = _refcodes_for_user_ids(user_ids)

    def build_rows(deps):
        if not deps:
            return []
        u_ids = list({d.user_id for d in deps if d.user_id})
        g_ids = list({d.game_id for d in deps if d.game_id})
        users = {u.id: u for u in User.query.filter(User.id.in_(u_ids)).all()} if u_ids else {}
        games = {g.id: g for g in Game.query.filter(Game.id.in_(g_ids)).all()} if g_ids else {}
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

    legacy_game_ids = list({d.game_id for d in (pending + recent) if d.game_id})
    games_map = {g.id: g for g in Game.query.filter(Game.id.in_(legacy_game_ids)).all()} if legacy_game_ids else {}
    backend_urls = {gid: _backend_url_for(g) for gid, g in games_map.items()}

    return render_template(
        "employee_deposits.html",
        page_title="Deposits",
        pending=pending,
        recent=recent,
        users=users_map,
        refcodes=refcodes,
        status="",
        q="",
        settings=settings,
        pending_rows=pending_rows,
        recent_rows=recent_rows,
        games=games_map,
        backend_urls=backend_urls,
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
            {% if game %}{% if game.name %}<div style="margin-top:6px"><strong>Game:</strong> {{ game.name }}</div>{% endif %}{% endif %}
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
    """
    Manual mark as LOADED (legacy). Automated button is /employee/deposits/<id>/approve.
    """
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
    _safe_notify(dep.user_id, f"✅ {pname}, your deposit #{dep.id} of {dep.amount} has been loaded.")
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
    pname  = _display_name(player) if player else f"User #{dep.user_id}"
    _safe_notify(dep.user_id, f"⚠️ {pname}, your deposit #{dep.id} was rejected. Please contact support.")
    flash("Deposit marked as REJECTED.", "success")
    return redirect(url_for("employeebp.deposits_list"))

# -------------------- AUTOMATION: Approve & Credit (Vendor-aware) --------------------
@employee_bp.post("/deposits/<int:deposit_id>/approve", endpoint="approve_and_credit_deposit")
@login_required
def approve_and_credit_deposit(deposit_id: int):
    """
    Approve a pending/received deposit and credit it on the proper vendor:
      - JUWA      -> recharge_sync(login, amount, note)
      - GameVault -> gv_credit(login, amount, note)
      - Milkyway  -> mw_recharge(login, amount, note)
    Returns JSON for the front-end button.
    """
    dep = db.session.get(DepositRequest, deposit_id)
    if not dep:
        return jsonify({"ok": False, "error": "Deposit not found"}), 404
    if dep.status not in ("PENDING", "RECEIVED"):
        return jsonify({"ok": False, "error": f"Invalid status {dep.status}"}), 409

    game = db.session.get(Game, dep.game_id) if dep.game_id else None
    vendor = _vendor_for_game(game)
    if vendor not in ("juwa", "gv", "milkyway"):
        return jsonify({"ok": False, "error": "This game's vendor is not JUWA, GameVault or Milkyway."}), 422

    acc_username = _get_login_username(dep.user_id, dep.game_id)
    if not acc_username:
        missing = "JUWA" if vendor == "juwa" else ("GameVault" if vendor == "gv" else "Milkyway")
        return jsonify({"ok": False, "error": f"Player has no {missing} login for this game."}), 422

    amt = int(dep.amount or 0)
    if amt <= 0:
        return jsonify({"ok": False, "error": "Amount must be > 0"}), 422

    note = f"Deposit#{dep.id} by {current_user.name or current_user.email or current_user.id}"

    try:
        if vendor == "juwa":
            res = recharge_sync(acc_username, amt, note)
        elif vendor == "gv":
            if not gv_credit:
                err = GV_IMPORT_ERROR or "gv_credit not available"
                return jsonify({"ok": False, "error": f"GameVault not configured: {err}"}), 500
            res = _run_maybe_async(gv_credit, acc_username, amt, note)
        elif vendor == "milkyway":
            res = mw_recharge(acc_username, amt, note)
        else:
            return jsonify({"ok": False, "error": f"Unsupported vendor {vendor}"}), 422
    except Exception as e:
        return jsonify({"ok": False, "error": f"{vendor.upper()} credit error: {e}"}), 500

    if not _result_ok(vendor, res):
        return jsonify({"ok": False, "error": f"{vendor.upper()} credit failed: {_result_error_text(res)}"}), 500

    dep.status = "LOADED"
    dep.loaded_at = datetime.utcnow()
    if hasattr(dep, "loaded_by"):
        dep.loaded_by = current_user.id

    wallet = PlayerBalance.query.filter_by(user_id=dep.user_id).first()
    if wallet:
        wallet.balance = (wallet.balance or 0) + amt

    db.session.commit()

    player = db.session.get(User, dep.user_id)
    pname  = _display_name(player) if player else f"User #{dep.user_id}"
    _safe_notify(dep.user_id, f"✅ {pname}, your deposit #{dep.id} of {amt} has been credited to {vendor.upper()}.")

    return jsonify({"ok": True})

# -------------------- REQUESTS --------------------
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

# -------------------- REQUESTS: provide/reject --------------------
@employee_bp.post("/requests/<int:req_id>/provide")
@login_required
def requests_provide(req_id: int):
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    action   = (request.form.get("action") or "approve").lower().strip()

    # Auto-provision: Milkyway only (Ready Accounts removed)
    if action == "auto":
        game = db.session.get(Game, req.game_id) if req.game_id else None
        if _vendor_for_game(game) == "milkyway":
            try:
                res = mw_create_user()
                if res and (res.get("ok") or res.get("created")) and (res.get("account") or res.get("username")):
                    username = (res.get("account") or res.get("username")).strip()
                    password = (res.get("password") or username or "changeme123").strip()
                    note = "Auto-provisioned via Milkyway"

                    acc = GameAccount.query.filter_by(user_id=req.user_id, game_id=req.game_id).first()
                    if not acc:
                        acc = GameAccount(user_id=req.user_id, game_id=req.game_id)
                        if hasattr(acc, "created_at") and not getattr(acc, "created_at", None):
                            acc.created_at = datetime.utcnow()
                        db.session.add(acc)
                    if hasattr(acc, "request_id"):
                        acc.request_id = req.id
                    for f in ("account_username","username","login","user"):
                        if hasattr(acc, f):
                            setattr(acc, f, username)
                            break
                    for f in ("account_password","password","passcode","pin"):
                        if hasattr(acc, f):
                            setattr(acc, f, password)
                            break
                    for f in ("extra","note","notes","remark"):
                        if hasattr(acc, f):
                            setattr(acc, f, note)
                            break

                    req.status = "APPROVED"
                    if hasattr(req, "approved_by_id"): req.approved_by_id = current_user.id
                    if hasattr(req, "approved_at"): req.approved_at = datetime.utcnow()
                    if hasattr(req, "updated_at"): req.updated_at = datetime.utcnow()
                    db.session.commit()

                    player = db.session.get(User, req.user_id)
                    pname  = _display_name(player) if player else f"User #{req.user_id}"
                    game   = db.session.get(Game, req.game_id)
                    gname  = game.name if game else "your game"
                    _safe_notify(req.user_id, f"🤖 {gname}: your login was issued. Open My Logins to view it, {pname}.")
                    flash("Milkyway account created and provided.", "success")
                    if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
                        return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
                    return redirect(url_for("employeebp.requests_list"))
                else:
                    flash(f"Milkyway auto-create failed: {res}", "error")
            except Exception as e:
                flash(f"Milkyway auto-create error: {e}", "error")
        else:
            flash("Auto-provision is only available for Milkyway.", "error")

    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    note     = (request.form.get("note") or "").strip()

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

    acc = GameAccount.query.filter_by(user_id=req.user_id, game_id=req.game_id).first()
    if not acc:
        acc = GameAccount(user_id=req.user_id, game_id=req.game_id)
        if hasattr(acc, "created_at") and not getattr(acc, "created_at", None):
            acc.created_at = datetime.utcnow()
        db.session.add(acc)

    if hasattr(acc, "request_id"):
        acc.request_id = req.id

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

    if hasattr(acc, "extra"):
        acc.extra = note
    elif hasattr(acc, "note"):
        acc.note = note
    if hasattr(req, "note") and note:
        req.note = note

    if hasattr(acc, "issued_by_id"):
        acc.issued_by_id = current_user.id
    if hasattr(acc, "issued_at"):
        acc.issued_at = datetime.utcnow()

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
    _safe_notify(req.user_id, f"🤖 {gname}: your login was issued. Open My Logins to view it, {pname}.")

    if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
        return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
    return redirect(url_for("employeebp.requests_list"))

@employee_bp.post("/requests/<int:req_id>/reject", endpoint="requests_reject")
@login_required
def requests_reject(req_id: int):
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    if (req.status or "").upper() in ("APPROVED", "PROVIDED", "REJECTED"):
        flash(f"Cannot reject request in status {req.status}.", "error")
        if request.referrer and req.game_id and f"/requests/game/{req.game_id}" in request.referrer:
            return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
        return redirect(url_for("employeebp.requests_list"))

    req.status = "REJECTED"
    if hasattr(req, "handled_by"):
        req.handled_by = current_user.id
    if hasattr(req, "updated_at"):
        req.updated_at = datetime.utcnow()
    db.session.commit()

    player = db.session.get(User, req.user_id) if req.user_id else None
    game   = db.session.get(Game, req.game_id) if req.game_id else None
    pname  = (player.name or player.email or f"User #{player.id}").strip() if player else f"User #{req.user_id}"
    gname  = game.name if game else "your game"
    _safe_notify(req.user_id, f"⚠️ {pname}, your access request for {gname} was rejected. Please contact support if needed.")

    flash("Request rejected.", "success")
    if request.referrer and req.game_id and f"/requests/game/{req.game_id}" in request.referrer:
        return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
    return redirect(url_for("employeebp.requests_list"))

# -------- Silent “backend openers” (no redirects) --------
@employee_bp.get("/games/<int:game_id>/backend")
@login_required
def open_game_backend(game_id: int):
    if SILENT_BACKEND_UI:
        return jsonify({"ok": False, "error": "Backend UI access disabled (silent mode)."}), 409
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

    def build_rows(rows):
        if not rows:
            return []
        u_ids = list({r.user_id for r in rows if r.user_id})
        g_ids = list({r.game_id for r in rows if r.game_id})

        users = {u.id: u for u in User.query.filter(User.id.in_(u_ids)).all()} if u_ids else {}
        games = {g.id: g for g in Game.query.filter(Game.id.in_(g_ids)).all()} if g_ids else {}
        wallets = {w.user_id: w for w in PlayerBalance.query.filter(PlayerBalance.user_id.in_(u_ids)).all()} if u_ids else {}

        acc_map = {}
        if u_ids:
            acc_rows = GameAccount.query.filter(GameAccount.user_id.in_(u_ids)).all()
            for a in acc_rows:
                acc_map.setdefault(a.user_id, []).append(a)

        out = []
        for w in rows:
            user = users.get(w.user_id)
            game = games.get(w.game_id) if w.game_id else None
            vendor = _vendor_from_game(game)
            login_user = None

            if w.game_id:
                login_user = _get_login_username(w.user_id, w.game_id)

            if not login_user:
                opts = acc_map.get(w.user_id, [])
                pick = None
                if vendor:
                    for a in opts:
                        g = games.get(getattr(a, "game_id", None)) or db.session.get(Game, getattr(a, "game_id", None))
                        if _vendor_from_game(g) == vendor:
                            pick = a
                            break
                if not pick and opts:
                    pick = opts[0]
                if pick:
                    login_user = _first_attr(pick, "account_username", "username", "login", "user", default="") or None
                    if not vendor:
                        g = games.get(getattr(pick, "game_id", None)) or db.session.get(Game, getattr(pick, "game_id", None))
                        vendor = _vendor_from_game(g)

            out.append({
                "wd": w,
                "user_name": _display_name(user) if user else f"User #{w.user_id}",
                "game_name": (game.name if game else "—"),
                "vendor": vendor or "unknown",
                "login_user": login_user or "",
                "wallet_balance": (wallets.get(w.user_id).balance if wallets.get(w.user_id) else 0),
            })
        return out

    return render_template(
        "employee_withdrawals.html",
        page_title="Withdrawals",
        pending_rows=build_rows(pending),
        recent_rows=build_rows(recent),
    )

@employee_bp.post("/withdrawals/<int:wd_id>/redeem", endpoint="withdrawals_redeem")
@login_required
def withdrawals_redeem(wd_id: int):
    """
    Try to redeem the player's funds on the vendor first.
    - JUWA      -> automation.juwa_api.redeem_sync(account, amount, remark)
    - GameVault -> gamevault_automation.gv_redeem(username, amount, note)
    - Milkyway  -> automation.milkyway_ui_bot.mw_redeem(account, amount, note)

    On success we set wd.status = 'APPROVED' so staff can finish with Mark Paid.
    Returns JSON for XHR, otherwise flashes and redirects.
    """
    is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    wd = db.session.get(WithdrawRequest, wd_id)
    if not wd:
        msg = "Withdrawal not found"
        return (jsonify({"ok": False, "error": msg}), 404) if is_xhr else (
            flash(msg, "error"),
            redirect(url_for("employeebp.withdrawals_list"))
        )[1]

    game = db.session.get(Game, wd.game_id) if wd.game_id else None
    vendor_raw = (getattr(game, "vendor", "") or "").strip().lower() if game else ""
    name_raw   = (getattr(game, "name", "")   or "").strip().lower() if game else ""

    vendor = None
    if vendor_raw in {"juwa", "gamevault", "gv", "milkyway", "milky"}:
        if vendor_raw in {"gv", "gamevault"}:
            vendor = "gv"
        elif vendor_raw in {"milkyway", "milky"}:
            vendor = "milkyway"
        else:
            vendor = "juwa"
    elif "juwa" in name_raw:
        vendor = "juwa"
    elif "gamevault" in name_raw or name_raw in {"gv", "game vault"}:
        vendor = "gv"
    elif "milkyway" in name_raw or "milky" in name_raw:
        vendor = "milkyway"

    acc_username = _get_login_username(wd.user_id, wd.game_id) if wd.game_id else None

    if not acc_username or not vendor:
        if vendor:
            found_user, found_vendor, _ = _find_account_username(wd.user_id, vendor_preference=vendor)
            if found_user and (not acc_username):
                acc_username = found_user
            if found_vendor and (not vendor):
                vendor = found_vendor

        if not acc_username or not vendor:
            mw_user, mw_vendor, _ = _find_account_username(wd.user_id, vendor_preference="milkyway")
            if mw_user and mw_vendor:
                acc_username = acc_username or mw_user
                vendor = vendor or mw_vendor
            else:
                any_user, any_vendor, _ = _find_account_username(wd.user_id)
                if any_user and any_vendor:
                    acc_username = acc_username or any_user
                    vendor = vendor or any_vendor

    if not acc_username:
        msg = "Redeem not possible: player has no saved login/ID for this game/vendor."
        return (jsonify({"ok": False, "error": msg}), 422) if is_xhr else (
            flash(msg, "error"),
            redirect(url_for("employeebp.withdrawals_list"))
        )[1]

    amount = int(wd.amount or 0)
    if amount <= 0:
        msg = "Redeem amount must be greater than zero."
        return (jsonify({"ok": False, "error": msg}), 422) if is_xhr else (
            flash(msg, "error"),
            redirect(url_for("employeebp.withdrawals_list"))
        )[1]

    if vendor == "juwa":
        def do_redeem():
            remark = f"Withdraw #{wd.id}"
            return redeem_sync(acc_username, amount, remark)

    elif vendor == "gv":
        if not gv_redeem:
            msg = f"GameVault not configured: {GV_IMPORT_ERROR or 'gv_redeem unavailable'}"
            return (jsonify({"ok": False, "error": msg}), 500) if is_xhr else (
                flash(msg, "error"),
                redirect(url_for("employeebp.withdrawals_list"))
            )[1]
        def do_redeem():
            note = f"Withdraw #{wd.id}"
            return _run_maybe_async(gv_redeem, acc_username, amount, note)

    elif vendor == "milkyway":
        def do_redeem():
            remark = f"Withdraw #{wd.id}"
            return mw_redeem(acc_username, amount, remark)

    else:
        msg = "This game's vendor is not recognized as JUWA, GameVault or Milkyway. Handle manually."
        return (jsonify({"ok": False, "error": msg}), 422) if is_xhr else (
            flash(msg, "error"),
            redirect(url_for("employeebp.withdrawals_list"))
        )[1]

    try:
        res = do_redeem()
        if not _result_ok(vendor, res):
            raise RuntimeError(_result_error_text(res))

        wd.status = "APPROVED"
        if hasattr(wd, "acted_by"):
            wd.acted_by = current_user.id
        if hasattr(wd, "acted_at"):
            wd.acted_at = datetime.utcnow()
        db.session.commit()

        player = db.session.get(User, wd.user_id)
        pname  = _display_name(player) if player else f"User #{wd.user_id}"
        _safe_notify(wd.user_id, f"🔓 {pname}, your withdrawal #{wd.id} has been redeemed and is awaiting payout.")

        if is_xhr:
            return jsonify({"ok": True})

        flash(f"Redeem successful via {vendor.upper()} for {acc_username}.", "success")
        return redirect(url_for("employeebp.withdrawals_list"))

    except Exception as e:
        db.session.rollback()
        if is_xhr:
            return jsonify({"ok": False, "error": str(e)})
        flash(f"Redeem failed: {e}", "error")
        return redirect(url_for("employeebp.withdrawals_list"))

@employee_bp.post("/withdrawals/<int:wd_id>/paid")
@login_required
def withdrawals_paid(wd_id: int):
    """
    Mark withdrawal as PAID. If still PENDING and we have a GV/JUWA/MILKYWAY login,
    attempt a vendor redeem first; otherwise just finalize locally.
    """
    wd = db.session.get(WithdrawRequest, wd_id)
    if not wd:
        flash("Withdrawal not found.", "error")
        return redirect(url_for("employeebp.withdrawals_list"))

    tip = request.form.get("tip_amount", type=int) or 0
    if tip < 0:
        tip = 0

    if wd.status in ("PENDING", "IN_PROGRESS"):
        game = db.session.get(Game, wd.game_id) if wd.game_id else None
        vendor = _vendor_from_game(game)
        acc_username = None
        if vendor and wd.game_id:
            acc_username = _get_login_username(wd.user_id, wd.game_id)
        if not acc_username:
            acc_username, vendor, _ = _find_account_username(wd.user_id, vendor_preference=vendor)

        if acc_username and vendor and (wd.amount or 0) > 0:
            try:
                if vendor == "juwa":
                    redeem_sync(acc_username, int(wd.amount or 0), f"Withdraw #{wd.id}")
                elif vendor == "gv" and gv_redeem:
                    _run_maybe_async(gv_redeem, acc_username, int(wd.amount or 0), f"Withdraw #{wd.id}")
                elif vendor == "milkyway":
                    mw_redeem(acc_username, int(wd.amount or 0), f"Withdraw #{wd.id}")
            except Exception:
                pass

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
    _safe_notify(
        wd.user_id,
        f"💸 {pname}, your withdrawal #{wd.id} for {wd.amount} via {method} has been paid. Please check your wallet."
    )
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
    _safe_notify(wd.user_id, f"⚠️ {pname}, your withdrawal #{wd.id} was rejected. Please contact support.")
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

# ------ Back-compat stub: Ready Accounts page (removed feature) ------
# Some templates still link to 'employeebp.ready_accounts_page'. Provide a safe stub.
@employee_bp.get("/ready-accounts", endpoint="ready_accounts_page")
@login_required
def ready_accounts_page_stub():
    flash("Ready Accounts feature is disabled in this build.", "info")
    return redirect(url_for("employeebp.requests_list"))

# Silent backend opener for a specific deposit (no redirect)
@employee_bp.route("/deposits/<int:deposit_id>/open-backend")
@login_required
def employee_open_backend(deposit_id):
    if SILENT_BACKEND_UI:
        return jsonify({"ok": False, "error": "Backend UI access disabled (silent mode)."}), 409
    dep = db.session.get(Deposit, deposit_id)  # Deposit is alias to DepositRequest
    if not dep or not getattr(dep, "backend_url", None):
        return "Backend link not ready yet", 409
    return redirect(dep.backend_url)