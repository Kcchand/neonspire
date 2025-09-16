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

# ---------- Optional: External account mapping (GameVault linkage) ----------
try:
    from models import ExternalAccount  # fields: user_id, vendor="gamevault", vendor_user_id, vendor_username
except Exception:  # pragma: no cover
    ExternalAccount = None  # graceful degrade if not present

# Optional helper to upsert ExternalAccount in one call
try:
    from models import get_or_create_external_account  # type: ignore
except Exception:  # pragma: no cover
    get_or_create_external_account = None

# ---------- GameVault automation (HTTP client adapter) ----------
# Capture real import errors so the UI can show them instead of "not configured".
GV_IMPORT_ERROR = None
try:
    from gamevault_automation import (
        CreditInput,
        credit_recharge,
        credit_redeem,
        user_list,
        create_user,
        GV_USERNAME_PREFIX,
        GV_USERNAME_SUFFIX,
    )
except Exception as e:  # pragma: no cover
    CreditInput = None
    credit_recharge = None
    credit_redeem = None
    user_list = None
    create_user = None
    GV_USERNAME_PREFIX = ""
    GV_USERNAME_SUFFIX = ""
    GV_IMPORT_ERROR = e

# -------------------- Ready Accounts model (alias to your models.ReadyAccount) --------------------
try:
    from models import ReadyAccount as ReadyAccountPool  # type: ignore
except Exception:  # pragma: no cover
    ReadyAccountPool = None  # sentinel so features auto-disable gracefully

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


# -------------------- ExternalAccount helpers --------------------
def _get_gv_external_account(user_id: int):
    """
    Returns (vendor_user_id:str|None, vendor_username:str|None) for GameVault mapping,
    or (None, None) if not found / not supported.
    """
    if not ExternalAccount:
        return None, None
    ext = ExternalAccount.query.filter_by(user_id=user_id, vendor="gamevault").first()
    if not ext:
        return None, None
    return (getattr(ext, "vendor_user_id", None) or None,
            (getattr(ext, "vendor_username", None) or "").strip() or None)

# Resolve GV numeric user_id from username when missing — via user_list()
def _gv_lookup_user_id_by_username(username: str | None) -> str | None:
    if not (username and user_list):
        return None
    try:
        # up to 5 pages (250 users). Adjust as needed for your panel size.
        for page in range(1, 6):
            body = user_list(page=page, page_size=50)
            items = (body or {}).get("data", {}).get("list", []) if isinstance(body, dict) else []
            for it in items:
                if (it.get("login_name") or "").strip().lower() == username.strip().lower():
                    return str(it.get("user_id"))
    except Exception:
        pass
    return None

def _resolve_gv_user_id_if_needed(vendor_user_id: str | None, username: str | None) -> str | None:
    if vendor_user_id:
        return vendor_user_id
    return _gv_lookup_user_id_by_username(username)

def _ensure_gv_external_account(user_id: int) -> tuple[str | None, str | None]:
    """
    Ensure there is an ExternalAccount for this user with a valid GameVault (vendor_user_id, vendor_username).
    Will create the GV user on-demand if missing. Returns (vendor_user_id, vendor_username).
    If ExternalAccount model or GV client is unavailable, returns (None, None).
    """
    if not ExternalAccount or not create_user:
        return None, None

    # existing?
    ea = ExternalAccount.query.filter_by(user_id=user_id, vendor="gamevault").first()
    gv_username = (ea.vendor_username.strip() if (ea and ea.vendor_username) else "").strip() if ea else ""
    gv_user_id = (ea.vendor_user_id.strip() if (ea and ea.vendor_user_id) else "").strip() if ea else ""

    # derive desired username if not set
    if not gv_username:
        gv_username = f"{(GV_USERNAME_PREFIX or '')}{user_id}{(GV_USERNAME_SUFFIX or '')}"

    # if we don't have GV numeric id, try to find or create
    if not gv_user_id:
        # 1) lookup by username
        gv_user_id = _gv_lookup_user_id_by_username(gv_username)

        # 2) create if still missing
        if not gv_user_id:
            try:
                create_user(gv_username)  # uses GV_DEFAULT_PASSWORD inside client
            except Exception:
                # even if create_user fails due to 'user exists', lookup will still find it below
                pass
            gv_user_id = _gv_lookup_user_id_by_username(gv_username)

    # If we now have user_id, persist mapping
    if gv_user_id:
        if get_or_create_external_account:
            rec = get_or_create_external_account(
                user_id=user_id,
                vendor="gamevault",
                vendor_user_id=gv_user_id,
                vendor_username=gv_username,
            )
            db.session.add(rec)
        else:
            if not ea:
                ea = ExternalAccount(
                    user_id=user_id,
                    vendor="gamevault",
                    vendor_user_id=gv_user_id,
                    vendor_username=gv_username,
                    created_at=datetime.utcnow(),
                )
                db.session.add(ea)
            else:
                if not ea.vendor_user_id:
                    ea.vendor_user_id = gv_user_id
                if not ea.vendor_username:
                    ea.vendor_username = gv_username
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return gv_user_id, gv_username

    # could not resolve/create
    return None, gv_username or None


def _run_maybe_async(func, *args, **kwargs):
    """
    Call `func` whether it's sync or async.
    """
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(*args, **kwargs))
    res = func(*args, **kwargs)
    if asyncio.iscoroutine(res):
        return asyncio.run(res)
    return res


# -------------------- GameVault call shims (aligned to working client) --------------------
def _call_credit(request_id: str, vendor_user_id: str | None, username: str | None,
                 amount: int, note: str = "") -> dict:
    """
    Use credit_recharge(CreditInput) from gamevault_automation.
    """
    if not (CreditInput and credit_recharge):
        raise RuntimeError("GameVault credit client not available")

    if not vendor_user_id:
        vendor_user_id = _resolve_gv_user_id_if_needed(None, username)

    if not vendor_user_id:
        raise RuntimeError("Missing GameVault user_id (mapping/lookup failed)")

    inp = CreditInput(
        user_id=str(vendor_user_id),
        amount=int(amount),
        memo=note or request_id,
        op_type="recharge",
    )
    return _run_maybe_async(credit_recharge, inp) or {}


def _call_redeem(request_id: str, vendor_user_id: str | None, username: str | None,
                 amount: int, note: str = "") -> dict:
    """
    Use credit_redeem(user_id, amount) from gamevault_automation.
    """
    if not credit_redeem:
        raise RuntimeError("GameVault redeem client not available")

    if not vendor_user_id:
        vendor_user_id = _resolve_gv_user_id_if_needed(None, username)

    if not vendor_user_id:
        raise RuntimeError("Missing GameVault user_id (mapping/lookup failed)")

    return _run_maybe_async(credit_redeem, str(vendor_user_id), int(amount), note or request_id) or {}


# -------------------- Ready Accounts: helpers --------------------
def _pool_counts_by_game():
    """
    Returns {game_id: count} of *available* ready accounts if ReadyAccountPool exists,
    else empty dict.
    """
    if not ReadyAccountPool:
        return {}
    q = db.session.query(ReadyAccountPool.game_id, func.count(ReadyAccountPool.id))
    if hasattr(ReadyAccountPool, "is_claimed"):
        q = q.filter(ReadyAccountPool.is_claimed == False)  # only unclaimed
    rows = q.group_by(ReadyAccountPool.game_id).all()
    return {gid: int(cnt or 0) for gid, cnt in rows}

def _autofulfill_from_pool(req: GameAccountRequest) -> bool:
    """
    If a ready account exists for req.game_id, claim it and approve the request,
    creating/updating the player's GameAccount. Returns True if fulfilled.
    Safe no-op if ReadyAccountPool is not defined.
    """
    if not ReadyAccountPool or not req or not req.game_id or not req.user_id:
        return False

    # already approved? nothing to do
    if (req.status or "").upper() in ("APPROVED", "PROVIDED"):
        return False

    # pick oldest available ready account for this game
    q = ReadyAccountPool.query.filter_by(game_id=req.game_id)
    if hasattr(ReadyAccountPool, "is_claimed"):
        q = q.filter(ReadyAccountPool.is_claimed == False)
    pool_row = q.order_by(ReadyAccountPool.created_at.asc()).first()
    if not pool_row:
        return False

    username = _first_attr(pool_row, "username", "login", "account_username", default="")
    password = _first_attr(pool_row, "password", "passcode", "account_password", default="")
    note     = _first_attr(pool_row, "note", "extra", "remark", default="")

    if not username or not password:
        # invalid pool row; do not consume
        return False

    # ensure or create GameAccount for this player+game
    acc = GameAccount.query.filter_by(user_id=req.user_id, game_id=req.game_id).first()
    if not acc:
        acc = GameAccount(user_id=req.user_id, game_id=req.game_id)
        if hasattr(acc, "created_at") and not getattr(acc, "created_at", None):
            acc.created_at = datetime.utcnow()
        db.session.add(acc)

    # link request if supported
    if hasattr(acc, "request_id"):
        acc.request_id = req.id

    # write credentials
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

    # stamp issuer to current employee
    if hasattr(acc, "issued_by_id") and current_user and current_user.is_authenticated:
        acc.issued_by_id = current_user.id
    if hasattr(acc, "issued_at"):
        acc.issued_at = datetime.utcnow()

    # close request
    req.status = "APPROVED"
    if hasattr(req, "approved_by_id") and current_user and current_user.is_authenticated:
        req.approved_by_id = current_user.id
    if hasattr(req, "handled_by") and current_user and current_user.is_authenticated:
        req.handled_by = current_user.id
    if hasattr(req, "approved_at"):
        req.approved_at = datetime.utcnow()
    if hasattr(req, "updated_at"):
        req.updated_at = datetime.utcnow()

    # consume/mark pool row
    try:
        if hasattr(pool_row, "is_claimed"):
            pool_row.is_claimed = True
            if hasattr(pool_row, "claimed_by"):
                pool_row.claimed_by = req.user_id
            if hasattr(pool_row, "claimed_at"):
                pool_row.claimed_at = datetime.utcnow()
        else:
            db.session.delete(pool_row)
    except Exception:
        pass

    db.session.commit()

    # notify player
    player = db.session.get(User, req.user_id)
    game   = db.session.get(Game, req.game_id)
    pname  = _display_name(player) if player else f"User #{req.user_id}"
    gname  = game.name if game else "your game"
    notify(req.user_id, f"🔐 {pname}, credentials added for {gname}. Check My Logins.")

    return True


# Try to autofulfill a batch of open requests (optionally by game)
def _try_autofulfill_open_requests(game_id: int | None = None):
    if not ReadyAccountPool:
        return  # feature disabled gracefully

    q = GameAccountRequest.query.filter(
        GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"])
    )
    if game_id:
        q = q.filter(GameAccountRequest.game_id == game_id)

    for req in q.order_by(GameAccountRequest.created_at.asc()).all():
        _autofulfill_from_pool(req)


# -------------------- READY ACCOUNTS PAGE + CRUD --------------------
@employee_bp.get("/ready-accounts", endpoint="ready_accounts_page")
@login_required
def ready_accounts_page():
    games = Game.query.order_by(Game.name.asc()).all()
    buckets = []

    if ReadyAccountPool:
        for g in games:
            rows_q = ReadyAccountPool.query.filter_by(game_id=g.id)
            if hasattr(ReadyAccountPool, "is_claimed"):
                rows_q = rows_q.filter(ReadyAccountPool.is_claimed == False)
            rows = rows_q.order_by(ReadyAccountPool.created_at.asc()).all()
            norm = []
            for r in rows:
                norm.append({
                    "id": getattr(r, "id", None),
                    "username": _first_attr(r, "username", "login", "account_username", default=""),
                    "password": _first_attr(r, "password", "passcode", "account_password", default=""),
                    "note":     _first_attr(r, "note", "extra", "remark", default=""),
                    "created_at": getattr(r, "created_at", None),
                })
            buckets.append({"game": g, "rows": norm, "count": len(norm)})
    else:
        buckets = []

    ctx = {
        "page_title": "Ready Accounts",
        "buckets": buckets,
        "pool_available": bool(ReadyAccountPool),
    }

    if _template_exists("employee_ready_accounts.html"):
        return render_template("employee_ready_accounts.html", **ctx)

    # Fallback inline template (view-only)
    return render_template_string("""
    {% extends "base.html" %}
    {% block content %}
    <div class="shell" style="margin-top:14px">
      <div class="panel" style="display:flex;align-items:center;justify-content:space-between">
        <div class="h3">Ready Accounts</div>
        <a class="btn" href="{{ url_for('employeebp.employee_home') }}">← Back</a>
      </div>
      {% if not pool_available %}
        <div class="panel"><div class="muted">
          Ready account pool is not configured. Define a <code>ReadyAccount</code> model to enable this page.
        </div></div>
      {% endif %}
      {% for b in buckets %}
        <div class="panel" style="margin-top:12px">
          <div class="h4" style="display:flex;align-items:center;justify-content:space-between">
            <div>{{ b.game.name }}</div>
            <div class="muted">Available: <strong>{{ b.count }}</strong></div>
          </div>
          {% if b.count %}
            <div class="table">
              <div class="t-head">
                <div>Username</div><div>Password</div><div>Note</div><div>Added</div>
              </div>
              {% for r in b.rows %}
                <div class="t-row">
                  <div class="mono">{{ r.username or '—' }}</div>
                  <div class="mono">{{ r.password or '—' }}</div>
                  <div>{{ r.note or '—' }}</div>
                  <div class="mono">{% if r.created_at %}{{ r.created_at.strftime('%Y-%m-%d %H:%M') }}{% else %}—{% endif %}</div>
                </div>
              {% endfor %}
            </div>
          {% else %}
            <div class="empty">No ready accounts saved for this game.</div>
          {% endif %}
        </div>
      {% endfor %}
    </div>
    {% endblock %}
    """, **ctx)


@employee_bp.post("/ready-accounts/add", endpoint="ready_accounts_add")
@login_required
def ready_accounts_add():
    if not ReadyAccountPool:
        flash("Ready accounts feature is not configured.", "error")
        return redirect(url_for("employeebp.ready_accounts_page"))

    game_id = request.form.get("game_id", type=int)
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    note     = (request.form.get("note") or "").strip()

    if not game_id or not username or not password:
        flash("Game, username and password are required.", "error")
        return redirect(url_for("employeebp.ready_accounts_page"))

    row = ReadyAccountPool(
        game_id=game_id,
        username=username,
        password=password,
        note=note,
    )
    if hasattr(row, "is_claimed"):
        row.is_claimed = False
    if hasattr(row, "added_by_id"):
        row.added_by_id = current_user.id
    if hasattr(row, "created_at") and not getattr(row, "created_at", None):
        row.created_at = datetime.utcnow()

    db.session.add(row)
    db.session.commit()

    _try_autofulfill_open_requests(game_id=game_id)

    flash("Ready account added.", "success")
    return redirect(url_for("employeebp.ready_accounts_page"))


@employee_bp.post("/ready-accounts/<int:row_id>/delete", endpoint="ready_accounts_delete")
@login_required
def ready_accounts_delete(row_id: int):
    if not ReadyAccountPool:
        flash("Ready accounts feature is not configured.", "error")
        return redirect(url_for("employeebp.ready_accounts_page"))

    row = db.session.get(ReadyAccountPool, row_id)
    if not row:
        flash("Row not found.", "error")
        return redirect(url_for("employeebp.ready_accounts_page"))

    if hasattr(row, "is_claimed") and row.is_claimed:
        flash("This ready account was already claimed.", "error")
        return redirect(url_for("employeebp.ready_accounts_page"))

    db.session.delete(row)
    db.session.commit()
    flash("Ready account deleted.", "success")
    return redirect(url_for("employeebp.ready_accounts_page"))


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
    """
    Legacy 'Loaded' button (manual mark). Your automated button lives at:
    POST /employee/deposits/<id>/approve  (see approve_and_credit_deposit)
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
    pname  = _display_name(player) if player else f"User #{dep.user_id}"
    notify(dep.user_id, f"⚠️ {pname}, your deposit #{dep.id} was rejected. Please contact support.")
    flash("Deposit marked as REJECTED.", "success")
    return redirect(url_for("employeebp.deposits_list"))


# -------------------- AUTOMATION: Approve & Credit (GameVault) --------------------
@employee_bp.post("/deposits/<int:deposit_id>/approve", endpoint="approve_and_credit_deposit")
@login_required
def approve_and_credit_deposit(deposit_id: int):
    """
    Approve a pending/received deposit and credit the exact amount
    in the external GameVault panel via automation.
    """
    # Lazy retry import if app started before env/module was ready
    global CreditInput, credit_recharge
    if not (CreditInput and credit_recharge):
        try:
            from gamevault_automation import CreditInput as _CI, credit_recharge as _cr
            CreditInput, credit_recharge = _CI, _cr
        except Exception as e:
            err = GV_IMPORT_ERROR or e
            return jsonify({"ok": False, "error": f"GameVault automation not configured: {err}"}), 500

    dep = db.session.get(DepositRequest, deposit_id)
    if not dep:
        return jsonify({"ok": False, "error": "Deposit not found"}), 404
    if dep.status not in ("PENDING", "RECEIVED"):
        return jsonify({"ok": False, "error": f"Invalid status {dep.status}"}), 409
    if not ExternalAccount:
        return jsonify({"ok": False, "error": "ExternalAccount model not available"}), 500

    # Ensure mapping exists (auto-provision GV user if needed)
    vendor_user_id, gv_username = _get_gv_external_account(dep.user_id)
    ensured_id, ensured_username = _ensure_gv_external_account(dep.user_id)
    vendor_user_id = vendor_user_id or ensured_id
    gv_username = gv_username or ensured_username
    if not gv_username or not vendor_user_id:
        return jsonify({"ok": False, "error": "GameVault account not mapped and auto-provision failed."}), 422

    # Ensure GV numeric user_id is present (safe re-lookup)
    vendor_user_id = _resolve_gv_user_id_if_needed(vendor_user_id, gv_username)

    try:
        result = _call_credit(
            request_id=f"dep{dep.id}",
            vendor_user_id=vendor_user_id,
            username=gv_username,
            amount=int(dep.amount or 0),
            note=f"Deposit #{dep.id}",
        )

        # Friendly handling for expired GV session (client already retries once)
        if isinstance(result, dict) and (result.get("unauthorized") or result.get("needs_login")):
            return jsonify({
                "ok": False,
                "error": "⚠️ GameVault session expired. Run: python gv_login_capture.py --force"
            }), 401

        ok = False
        if isinstance(result, dict):
            ok = bool(result.get("ok", False) or result.get("code", 1) in (0, 200))

        if not ok:
            return jsonify({"ok": False, "error": f"GameVault credit failed: {result}"}), 500

        # Mark as loaded locally & credit wallet
        dep.status = "LOADED"
        dep.loaded_at = datetime.utcnow()
        if hasattr(dep, "loaded_by"):
            dep.loaded_by = current_user.id

        wallet = PlayerBalance.query.filter_by(user_id=dep.user_id).first()
        if wallet:
            wallet.balance = (wallet.balance or 0) + (dep.amount or 0)

        db.session.commit()

        player = db.session.get(User, dep.user_id)
        pname  = _display_name(player) if player else f"User #{dep.user_id}"
        notify(dep.user_id, f"✅ {pname}, your deposit #{dep.id} of {dep.amount} has been credited.")
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"Credit failed: {e}"}), 500


# -------------------- GAME ACCESS REQUESTS --------------------
@employee_bp.get("/requests")
@login_required
def requests_list():
    _try_autofulfill_open_requests(game_id=None)

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

    pool_counts = _pool_counts_by_game()

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
        pool_counts=pool_counts,
        pool_counts_for_selected=None,
    )


@employee_bp.get("/requests/game/<int:game_id>")
@login_required
def requests_list_by_game(game_id: int):
    _try_autofulfill_open_requests(game_id=game_id)

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

    pool_counts = _pool_counts_by_game()
    selected_count = pool_counts.get(game_id, 0) if pool_counts else 0

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
        pool_counts=pool_counts,
        pool_counts_for_selected=selected_count,
    )


@employee_bp.post("/requests/<int:req_id>/provide")
@login_required
def requests_provide(req_id: int):
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    action   = (request.form.get("action") or "approve").lower().strip()
    if action == "auto":
        if _autofulfill_from_pool(req):
            flash("Request auto-fulfilled from ready accounts.", "success")
            if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
                return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
            return redirect(url_for("employeebp.requests_list"))
        else:
            flash("No ready account available for this game.", "error")

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
    notify(req.user_id, f"🤖 {gname}: your login was issued. Open My Logins to view it, {pname}.")

    if request.referrer and f"/requests/game/{req.game_id}" in request.referrer:
        return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
    return redirect(url_for("employeebp.requests_list"))


# --------- NEW: reject game access request (fixes BuildError) ----------
@employee_bp.post("/requests/<int:req_id>/reject", endpoint="requests_reject")
@login_required
def requests_reject(req_id: int):
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        flash("Request not found.", "error")
        return redirect(url_for("employeebp.requests_list"))

    # Only reject if not already terminal/approved
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

    # Notify player
    player = db.session.get(User, req.user_id) if req.user_id else None
    game   = db.session.get(Game, req.game_id) if req.game_id else None
    pname  = (player.name or player.email or f"User #{player.id}").strip() if player else f"User #{req.user_id}"
    gname  = game.name if game else "your game"
    notify(req.user_id, f"⚠️ {pname}, your access request for {gname} was rejected. Please contact support if needed.")

    flash("Request rejected.", "success")
    if request.referrer and req.game_id and f"/requests/game/{req.game_id}" in request.referrer:
        return redirect(url_for("employeebp.requests_list_by_game", game_id=req.game_id))
    return redirect(url_for("employeebp.requests_list"))
# ---------------------------------------------------------------------


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


@employee_bp.post("/withdrawals/<int:wd_id>/redeem", endpoint="withdrawals_redeem")
@login_required
def withdrawals_redeem(wd_id: int):
    """
    Employee clicks REDEEM first: call GameVault (type=2) ONLY.
    If successful, set status=APPROVED so staff can finish with Mark Paid.
    """
    # Lazy retry import if needed
    global credit_redeem
    if not credit_redeem:
        try:
            from gamevault_automation import credit_redeem as _rw
            credit_redeem = _rw
        except Exception as e:
            err = GV_IMPORT_ERROR or e
            flash(f"GameVault not configured: {err}", "error")
            return redirect(url_for("employeebp.withdrawals_list"))

    wd = db.session.get(WithdrawRequest, wd_id)
    if not wd:
        flash("Withdrawal not found.", "error")
        return redirect(url_for("employeebp.withdrawals_list"))
    if wd.status not in ("PENDING", "IN_PROGRESS"):
        flash(f"Cannot redeem withdrawal in status {wd.status}.", "error")
        return redirect(url_for("employeebp.withdrawals_list"))

    # Ensure mapping exists (auto-provision GV user if needed)
    vendor_user_id, gv_username = _get_gv_external_account(wd.user_id)
    ensured_id, ensured_username = _ensure_gv_external_account(wd.user_id)
    vendor_user_id = vendor_user_id or ensured_id
    gv_username = gv_username or ensured_username
    if not gv_username or not vendor_user_id:
        flash("Redeem not possible: GameVault account not mapped and auto-provision failed.", "error")
        return redirect(url_for("employeebp.withdrawals_list"))

    # Ensure user_id present
    vendor_user_id = _resolve_gv_user_id_if_needed(vendor_user_id, gv_username)

    try:
        result = _call_redeem(
            request_id=f"wd{wd.id}",
            vendor_user_id=vendor_user_id,
            username=gv_username,
            amount=int(wd.amount or 0),
            note=f"Withdraw #{wd.id}",
        )
        if isinstance(result, dict) and (result.get("unauthorized") or result.get("needs_login")):
            flash("⚠️ GameVault session expired. Run: python gv_login_capture.py --force", "error")
            return redirect(url_for("employeebp.withdrawals_list"))

        ok = False
        if isinstance(result, dict):
            ok = bool(result.get("ok", False) or result.get("code", 1) in (0, 200))
        if not ok:
            raise RuntimeError(result)

        wd.status = "APPROVED"
        if hasattr(wd, "acted_by"):
            wd.acted_by = current_user.id
        if hasattr(wd, "acted_at"):
            wd.acted_at = datetime.utcnow()
        db.session.commit()

        player = db.session.get(User, wd.user_id)
        pname  = _display_name(player) if player else f"User #{wd.user_id}"
        notify(wd.user_id, f"🔓 {pname}, your withdrawal #{wd.id} has been redeemed and is awaiting final payout.")
        try:
            notify(current_user.id, f"GV redeem OK for withdraw #{wd.id} (user {pname}). Now Mark Paid when sent.")
        except Exception:
            pass

        flash("GameVault redeem successful. You can now Mark Paid.", "success")
        return redirect(url_for("employeebp.withdrawals_list"))
    except Exception as e:
        db.session.rollback()
        flash(f"GameVault redeem failed: {e}", "error")
        return redirect(url_for("employeebp.withdrawals_list"))


@employee_bp.post("/withdrawals/<int:wd_id>/paid")
@login_required
def withdrawals_paid(wd_id: int):
    """
    Mark withdrawal as PAID. As a fallback, if still PENDING and GV mapping is present,
    we attempt a redeem first; otherwise we just finalize the local payout.
    """
    wd = db.session.get(WithdrawRequest, wd_id)
    if not wd:
        flash("Withdrawal not found.", "error")
        return redirect(url_for("employeebp.withdrawals_list"))

    tip = request.form.get("tip_amount", type=int) or 0
    if tip < 0:
        tip = 0

    # Optional fallback redeem (if not previously APPROVED)
    if wd.status in ("PENDING", "IN_PROGRESS"):
        # Ensure mapping exists (auto-provision GV user if needed)
        vendor_user_id, gv_username = _get_gv_external_account(wd.user_id)
        ensured_id, ensured_username = _ensure_gv_external_account(wd.user_id)
        vendor_user_id = vendor_user_id or ensured_id
        gv_username = gv_username or ensured_username

        if credit_redeem and gv_username and (wd.amount or 0) > 0 and vendor_user_id:
            vendor_user_id = _resolve_gv_user_id_if_needed(vendor_user_id, gv_username)
            try:
                result = _call_redeem(
                    request_id=f"wd{wd.id}",
                    vendor_user_id=vendor_user_id,
                    username=gv_username,
                    amount=int(wd.amount or 0),
                    note=f"Withdraw #{wd.id}",
                )
                if isinstance(result, dict) and (result.get("unauthorized") or result.get("needs_login")):
                    flash("⚠️ GameVault session expired. Run: python gv_login_capture.py --force", "error")
                    return redirect(url_for("employeebp.withdrawals_list"))

                ok = False
                if isinstance(result, dict):
                    ok = bool(result.get("ok", False) or result.get("code", 1) in (0, 200))
                if not ok:
                    flash(f"GameVault redeem failed: {result}", "error")
                    return redirect(url_for("employeebp.withdrawals_list"))
            except Exception as e:
                flash(f"GameVault redeem failed: {e}", "error")
                return redirect(url_for("employeebp.withdrawals_list"))

    # Local bookkeeping
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


# Backend opener for a specific deposit
@employee_bp.route('/deposits/<int:deposit_id>/open-backend')
@login_required
def employee_open_backend(deposit_id):
    dep = db.session.get(Deposit, deposit_id)  # Deposit is alias to DepositRequest
    if not dep or not getattr(dep, "backend_url", None):
        return "Backend link not ready yet", 409
    return redirect(dep.backend_url)