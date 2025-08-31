# admin_bp.py
import os
import time
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import (
    Blueprint, render_template, render_template_string, request, redirect,
    url_for, flash, abort, current_app
)
from flask_login import login_required, current_user
from sqlalchemy import text, func, or_
from sqlalchemy import inspect as sqla_inspect  # SQLAlchemy 2.x safe table check

from models import (
    db,
    User,
    Game,
    DepositRequest,
    PaymentSettings,
    PlayerBalance,      # optional wallet credit on deposit load
    GameAccount,        # for IDs / reporting
    GameAccountRequest, # schema may exist with different statuses
    WithdrawRequest,
    ReferralCode,
    Notification,
    notify,
)

admin_bp = Blueprint("adminbp", __name__, url_prefix="/admin")

# -------------------- Guards --------------------

@admin_bp.before_request
def require_admin():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login_get", next=url_for("adminbp.admin_home")))
    if (current_user.role or "").upper() != "ADMIN":
        return abort(403)

# -------------------- Helpers -------------------

def _uploads_dir() -> str:
    """Ensure static/uploads exists and return the absolute path."""
    updir = os.path.join(current_app.static_folder, "uploads")
    os.makedirs(updir, exist_ok=True)
    return updir

def _save_image(file_storage, prefix: str) -> str | None:
    """
    Save an uploaded image into static/uploads and return a static URL path.
    Returns None if no file selected or invalid.
    """
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None
    filename = secure_filename(file_storage.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        flash("Only PNG/JPG/WEBP images are allowed.", "error")
        return None

    ts = int(time.time())
    new_name = f"{prefix}_{ts}{ext}"
    dest = os.path.join(_uploads_dir(), new_name)
    file_storage.save(dest)
    return url_for("static", filename=f"uploads/{new_name}")

# ---------- tiny key–value fallback (no migration needed) ----------

def _ensure_kv():
    """Create a simple key–value table if it doesn't exist."""
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

def _kv_set(k: str, v: str | None):
    _ensure_kv()
    v = v if v is not None else ""
    try:
        bind = db.session.get_bind()
        dialect = bind.dialect.name if bind else "sqlite"
        if dialect in ("postgresql", "postgres"):
            db.session.execute(
                text("INSERT INTO kv_store(key,value) VALUES(:k,:v) "
                     "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"),
                {"k": k, "v": v}
            )
        elif dialect in ("mysql", "mariadb"):
            db.session.execute(
                text("INSERT INTO kv_store(`key`,`value`) VALUES(:k,:v) "
                     "ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)"),
                {"k": k, "v": v}
            )
        else:  # sqlite
            db.session.execute(
                text("INSERT INTO kv_store(key,value) VALUES(:k,:v) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
                {"k": k, "v": v}
            )
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

# ---------- PaymentSettings (row #1) ----------

def _get_settings() -> PaymentSettings:
    s = db.session.get(PaymentSettings, 1)
    if not s:
        s = PaymentSettings(id=1, min_redeem=0, max_redeem=0)
        db.session.add(s)
        db.session.commit()
    return s

# Safe attribute set (only if column exists on model)
def _maybe_set(obj, attr: str, value):
    if hasattr(obj, attr):
        setattr(obj, attr, value)

# Resolve employee id from a GameAccount across schema variants
def _emp_id_from_account(acc: GameAccount):
    for col in ("issued_by_id", "issued_by", "created_by", "staff_id"):
        if hasattr(acc, col):
            val = getattr(acc, col, None)
            if val:
                return val
    return None

# Resolve "issued at" / created timestamp for display
def _when_from_account(acc: GameAccount):
    for col in ("issued_at", "created_at", "updated_at"):
        if hasattr(acc, col):
            val = getattr(acc, col, None)
            if val:
                return val
    return None

def _name(u: User | None) -> str:
    return (u.name or u.email or (f"User #{u.id}" if u and u.id else "—")).strip() if u else "—"

# -------------------- Admin Home --------------------

@admin_bp.get("/")
@login_required
def admin_home():
    s = _get_settings()

    players_count = User.query.filter_by(role="PLAYER").count()
    employees_count = User.query.filter_by(role="EMPLOYEE").count()
    games_count = Game.query.count()
    pending_deposits_count = DepositRequest.query.filter_by(status="PENDING").count()

    employees = (
        User.query.filter_by(role="EMPLOYEE")
        .order_by(User.created_at.desc() if hasattr(User, "created_at") else User.id.desc())
        .all()
    )

    # Games list (for trending checkboxes)
    order_col = Game.created_at if hasattr(Game, "created_at") else Game.id
    games = Game.query.order_by(order_col.desc()).all()

    # Pending deposits (moderation panel on dashboard)
    pending_deposits = (
        DepositRequest.query.filter_by(status="PENDING")
        .order_by(DepositRequest.created_at.desc())
        .limit(50)
        .all()
    )

    # Reports: Employee contributions
    loaded = DepositRequest.query.filter_by(status="LOADED").all()
    dep_by_emp = {}
    for d in loaded:
        emp_id = getattr(d, "loaded_by", None)  # your schema uses loaded_by
        if not emp_id:
            continue
        bucket = dep_by_emp.setdefault(emp_id, {"count": 0, "sum": 0})
        bucket["count"] += 1
        bucket["sum"] += int(d.amount or 0)

    employees_map = {e.id: e for e in employees}

    # IDs created per employee (SQLAlchemy 2.x safe table presence check)
    ids_by_emp = {}
    try:
        insp = sqla_inspect(db.engine)

        # Count from GameAccountRequest if present
        if "game_account_requests" in insp.get_table_names():
            q = GameAccountRequest.query.filter(
                GameAccountRequest.status.in_(["PROVIDED", "APPROVED"])
            )
            for r in q.all():
                emp_id = (
                    getattr(r, "approved_by_id", None)
                    or getattr(r, "approved_by", None)
                    or getattr(r, "handled_by", None)
                )
                if emp_id:
                    ids_by_emp[emp_id] = ids_by_emp.get(emp_id, 0) + 1

        # Also count directly from GameAccount across multiple possible columns
        if "game_accounts" in insp.get_table_names():
            for col_name in ("issued_by_id", "issued_by", "created_by", "staff_id"):
                if hasattr(GameAccount, col_name):
                    col = getattr(GameAccount, col_name)
                    for emp_id, cnt in (
                        db.session.query(col, func.count(GameAccount.id))
                        .filter(col.isnot(None))
                        .group_by(col)
                        .all()
                    ):
                        ids_by_emp[emp_id] = ids_by_emp.get(emp_id, 0) + int(cnt or 0)

    except Exception:
        db.session.rollback()

    # Players per game
    acc_counts = (
        db.session.query(GameAccount.game_id, func.count(GameAccount.id))
        .group_by(GameAccount.game_id)
        .all()
    )
    players_per_game = []
    game_map = {g.id: g for g in games}
    for gid, cnt in acc_counts:
        g = game_map.get(gid)
        if g:
            players_per_game.append({"game": g, "count": cnt})

    # Latest IDs issued (resolve staff name robustly)
    latest_ids = []
    rows = (
        db.session.query(GameAccount, Game.name.label("game_name"))
        .join(Game, Game.id == GameAccount.game_id)
        .order_by(GameAccount.id.desc())
        .limit(15)
        .all()
    )

    for acc, gname in rows:
        # player
        user = db.session.get(User, getattr(acc, "user_id", None) or 0)

        # figure out staff who issued (account columns -> linked request fallbacks)
        emp_id = _emp_id_from_account(acc)
        if not emp_id:
            req_id = getattr(acc, "request_id", None)
            if req_id:
                req = db.session.get(GameAccountRequest, req_id)
                if req:
                    emp_id = (
                        getattr(req, "approved_by_id", None)
                        or getattr(req, "approved_by", None)
                        or getattr(req, "handled_by", None)
                    )

        emp_name = _name(db.session.get(User, emp_id) if emp_id else None) if emp_id else "N/A"

        latest_ids.append({
            "player": _name(user),
            "game": gname,
            "employee": emp_name,
            "created_at": _when_from_account(acc),
        })

    # ---- Promo + Trending values (read from columns OR kv fallback) ----
    promo_line1_value = getattr(s, "promo_line1", None) or _kv_get("promo_line1") or ""
    promo_line2_value = getattr(s, "promo_line2", None) or _kv_get("promo_line2") or ""

    raw_csv = getattr(s, "trending_game_ids", None) or _kv_get("trending_game_ids") or ""
    selected_ids = set()
    for token in str(raw_csv).split(","):
        token = token.strip()
        if token.isdigit():
            selected_ids.add(int(token))

    return render_template(
        "dashboard_admin.html",
        page_title="Admin",
        # metrics
        players_count=players_count,
        employees_count=employees_count,
        games_count=games_count,
        pending_deposits_count=pending_deposits_count,
        # lists / forms
        employees=employees,
        games=games,
        pending_deposits=pending_deposits,
        # settings & normalized promo fields for the template
        settings=s,
        promo_line1_value=promo_line1_value,
        promo_line2_value=promo_line2_value,
        trending_selected_ids=selected_ids,
        # reports
        employees_map=employees_map,
        dep_by_emp=dep_by_emp,
        ids_by_emp=ids_by_emp,
        players_per_game=players_per_game,
        latest_ids=latest_ids,
    )

# -------------------- Settings (GET page) ---------------------

@admin_bp.get("/settings")
@login_required
def settings_get():
    s = _get_settings()

    promo_line1 = getattr(s, "promo_line1", None) or _kv_get("promo_line1") or ""
    promo_line2 = getattr(s, "promo_line2", None) or _kv_get("promo_line2") or ""
    trending_csv = getattr(s, "trending_game_ids", None) or _kv_get("trending_game_ids") or ""

    return render_template(
        "admin_settings.html",
        page_title="Settings",
        settings=s,
        promo_line1=promo_line1,
        promo_line2=promo_line2,
        trending_csv=trending_csv
    )

# -------------------- Settings: QR / limits / socials ---------------------

@admin_bp.post("/settings/crypto")
@login_required
def update_crypto_qr():
    s = _get_settings()
    s.crypto_wallet_text = (request.form.get("crypto_wallet_text") or "").strip()

    # NEW: save optional direct-pay URL (and mirror to kv for safety)
    crypto_pay_url = (request.form.get("crypto_pay_url") or "").strip()
    _maybe_set(s, "crypto_pay_url", crypto_pay_url)
    _kv_set("crypto_pay_url", crypto_pay_url)

    qr_file = request.files.get("crypto_qr")
    url = _save_image(qr_file, "crypto_qr")
    if url:
        s.crypto_qr_url = url
    db.session.commit()
    flash("Crypto settings saved.", "success")
    return redirect(url_for("adminbp.settings_get"))

@admin_bp.post("/settings/chime")
@login_required
def update_chime_qr():
    s = _get_settings()
    s.chime_handle = (request.form.get("chime_handle") or "").strip()

    # NEW: save optional direct-pay URL (and mirror to kv for safety)
    chime_pay_url = (request.form.get("chime_pay_url") or "").strip()
    _maybe_set(s, "chime_pay_url", chime_pay_url)
    _kv_set("chime_pay_url", chime_pay_url)

    qr_file = request.files.get("chime_qr")
    url = _save_image(qr_file, "chime_qr")
    if url:
        s.chime_qr_url = url
    db.session.commit()
    flash("Chime settings saved.", "success")
    return redirect(url_for("adminbp.settings_get"))

@admin_bp.post("/settings/limits")
@login_required
def update_limits():
    s = _get_settings()
    s.min_redeem = request.form.get("min_redeem", type=int) or 0
    s.max_redeem = request.form.get("max_redeem", type=int) or 0
    db.session.commit()
    flash("Withdrawal limits updated.", "success")
    return redirect(url_for("adminbp.settings_get"))

@admin_bp.post("/settings/socials")
@login_required
def update_socials():
    s = _get_settings()
    s.whatsapp_url  = (request.form.get("whatsapp_url") or "").strip()
    s.telegram_url  = (request.form.get("telegram_url") or "").strip()
    s.facebook_url  = (request.form.get("facebook_url") or "").strip()
    s.instagram_url = (request.form.get("instagram_url") or "").strip()
    db.session.commit()
    flash("Social/contact links updated.", "success")
    return redirect(url_for("adminbp.settings_get"))

# -------------------- Promotions / Trending -------------------------------

@admin_bp.post("/settings/news")
@login_required
def update_news():
    """
    Save promo line 1/2 and bonus percent.
    Writes to PaymentSettings if columns exist, and always mirrors to kv_store.
    """
    s = _get_settings()

    line1 = (request.form.get("promo_line1") or "").strip()
    line2 = (request.form.get("promo_line2") or "").strip()
    bonus = request.form.get("bonus_percent", type=int)

    _maybe_set(s, "promo_line1", line1)
    _maybe_set(s, "promo_line2", line2)
    if bonus is not None:
        _maybe_set(s, "bonus_percent", int(bonus))

    # Always mirror into kv_store
    _kv_set("promo_line1", line1)
    _kv_set("promo_line2", line2)
    if bonus is not None:
        _kv_set("bonus_percent", str(int(bonus)))

    db.session.commit()
    flash("Promotions updated.", "success")
    return redirect(url_for("adminbp.settings_get"))

@admin_bp.post("/settings/trending")
@login_required
def update_trending():
    """
    Save today's trending game ids (CSV) + optional note.
    Writes to PaymentSettings if columns exist, and mirrors to kv_store.
    """
    s = _get_settings()

    ids_list = request.form.getlist("trending_ids")
    if not ids_list:
        csv = (request.form.get("trending_game_ids") or "").strip()
        ids_list = [t.strip() for t in csv.split(",") if t.strip()]

    cleaned = []
    for t in ids_list:
        if str(t).isdigit():
            cleaned.append(str(int(t)))
    csv_value = ",".join(cleaned)

    note = (request.form.get("trending_note") or "").strip()

    _maybe_set(s, "trending_game_ids", csv_value)
    _maybe_set(s, "trending_note", note)

    _kv_set("trending_game_ids", csv_value)
    _kv_set("trending_note", note)

    db.session.commit()
    flash("Trending updated.", "success")
    return redirect(url_for("adminbp.settings_get"))

# -------------------- Employees (Create / List) ---------------------------

@admin_bp.route("/users", methods=["GET", "POST"])
@login_required
def admin_users():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not name or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("adminbp.admin_users"))

        if User.query.filter_by(email=email).first():
            flash("Email already exists.", "error")
            return redirect(url_for("adminbp.admin_users"))

        u = User(name=name, email=email, role="EMPLOYEE")
        if hasattr(u, "set_password"):
            u.set_password(password)
        else:
            setattr(u, "password", password)
        db.session.add(u)
        db.session.commit()

        flash("Employee created.", "success")
        return redirect(url_for("adminbp.admin_users"))

    employees = User.query.filter_by(role="EMPLOYEE").order_by(
        User.created_at.desc() if hasattr(User, "created_at") else User.id.desc()
    ).all()
    return render_template("admin_users.html", employees=employees, page_title="Employees")

# === Employee account actions ===

@admin_bp.post("/users/<int:user_id>/disable")
@login_required
def admin_disable_employee(user_id: int):
    u = db.session.get(User, user_id)
    if not u or (u.role or "").upper() == "ADMIN":
        flash("Invalid employee.", "error")
        return redirect(url_for("adminbp.admin_users"))

    did = False
    if hasattr(u, "is_active"):
        u.is_active = False; did = True
    if hasattr(u, "active"):
        u.active = False; did = True
    if not did:
        u.role = "DISABLED"
    db.session.commit()
    flash(f"{_name(u)} has been disabled.", "success")
    return redirect(url_for("adminbp.admin_users"))

@admin_bp.post("/users/<int:user_id>/enable")
@login_required
def admin_enable_employee(user_id: int):
    u = db.session.get(User, user_id)
    if not u:
        flash("User not found.", "error")
        return redirect(url_for("adminbp.admin_users"))

    did = False
    if hasattr(u, "is_active"):
        u.is_active = True; did = True
    if hasattr(u, "active"):
        u.active = True; did = True
    if (u.role or "").upper() in ("DISABLED", "") and not did:
        u.role = "EMPLOYEE"
    db.session.commit()
    flash(f"{_name(u)} has been enabled.", "success")
    return redirect(url_for("adminbp.admin_users"))

@admin_bp.post("/users/<int:user_id>/password")
@login_required
def admin_change_employee_password(user_id: int):
    u = db.session.get(User, user_id)
    if not u or (u.role or "").upper() == "ADMIN":
        flash("Invalid employee.", "error")
        return redirect(url_for("adminbp.admin_users"))
    new_pw = (request.form.get("new_password") or "").strip()
    if not new_pw:
        flash("New password is required.", "error")
        return redirect(url_for("adminbp.admin_users"))
    if hasattr(u, "set_password"):
        u.set_password(new_pw)
    else:
        setattr(u, "password", new_pw)
    db.session.commit()
    flash(f"Password updated for {_name(u)}.", "success")
    return redirect(url_for("adminbp.admin_users"))

# -------------------- Games Management (list/create/edit) -----------------

@admin_bp.get("/games")
@login_required
def games_list():
    games = Game.query.order_by(
        Game.created_at.desc() if hasattr(Game, "created_at") else Game.id.desc()
    ).all()
    return render_template("admin_games.html", page_title="Manage Games", games=games)

@admin_bp.post("/games/create")
@login_required
def create_game():
    """Create a game (supports either icon_url OR icon_file upload)."""
    name         = (request.form.get("name") or "").strip()
    description  = (request.form.get("description") or "").strip()
    download_url = (request.form.get("download_url") or "").strip()
    icon_url     = (request.form.get("icon_url") or "").strip()
    is_active    = bool(request.form.get("is_active"))
    backend_url  = (request.form.get("backend_url") or "").strip()  # optional

    if not name:
        flash("Game name is required.", "error")
        return redirect(url_for("adminbp.games_list"))

    icon_file = request.files.get("icon_file")
    uploaded_icon = _save_image(icon_file, "game_icon")
    if uploaded_icon:
        icon_url = uploaded_icon or icon_url

    g = Game(
        name=name,
        description=description or None,
        download_url=download_url or None,
        icon_url=icon_url or None,
        is_active=is_active,
    )

    if hasattr(g, "backend_url"):
        g.backend_url = backend_url or None

    if hasattr(g, "created_at") and not g.created_at:
        g.created_at = datetime.utcnow()

    db.session.add(g)
    db.session.commit()  # need g.id for kv mirror

    _kv_set(f"game:{g.id}:backend_url", backend_url or "")

    flash("Game created.", "success")
    return redirect(url_for("adminbp.games_list"))

@admin_bp.post("/games/<int:game_id>/edit")
@login_required
def edit_game_post(game_id: int):
    g = db.session.get(Game, game_id)
    if not g:
        flash("Game not found.", "error")
        return redirect(url_for("adminbp.games_list"))

    g.name         = (request.form.get("name") or g.name).strip()
    g.description  = (request.form.get("description") or "").strip() or None
    g.download_url = (request.form.get("download_url") or "").strip() or None

    backend_url = (request.form.get("backend_url") or "").strip() or None
    if hasattr(g, "backend_url"):
        g.backend_url = backend_url

    icon_file = request.files.get("icon_file")
    uploaded_icon = _save_image(icon_file, "game_icon")
    if uploaded_icon:
        g.icon_url = uploaded_icon
    else:
        url_from_text = (request.form.get("icon_url") or "").strip()
        if url_from_text:
            g.icon_url = url_from_text

    g.is_active = "is_active" in request.form

    db.session.commit()

    _kv_set(f"game:{g.id}:backend_url", backend_url or "")

    flash("Game updated.", "success")
    return redirect(url_for("adminbp.games_list"))

@admin_bp.get("/games/<int:game_id>/toggle")
@login_required
def toggle_game(game_id: int):
    g = db.session.get(Game, game_id)
    if not g:
        flash("Game not found.", "error")
        return redirect(url_for("adminbp.games_list"))
    g.is_active = not bool(g.is_active)
    db.session.commit()
    flash(f"{g.name} is now {'Active' if g.is_active else 'Inactive'}.", "success")
    return redirect(url_for("adminbp.games_list"))

@admin_bp.post("/games/<int:game_id>/delete")
@login_required
def delete_game(game_id: int):
    g = db.session.get(Game, game_id)
    if not g:
        flash("Game not found.", "error")
        return redirect(url_for("adminbp.games_list"))
    db.session.delete(g)
    db.session.commit()
    flash("Game deleted.", "success")
    return redirect(url_for("adminbp.games_list"))

# -------------------- Deposits Audit & Actions ----------------

@admin_bp.get("/deposits")
@login_required
def deposits_audit():
    pending = (
        DepositRequest.query.filter_by(status="PENDING")
        .order_by(DepositRequest.created_at.desc())
        .all()
    )
    recent = (
        DepositRequest.query.filter(DepositRequest.status.in_(["LOADED", "PAID"]))
        .order_by(DepositRequest.updated_at.desc())
        .limit(20)
        .all()
    )
    return render_template("admin_deposits.html", page_title="Deposits", pending=pending, recent=recent)

@admin_bp.post("/deposits/<int:dep_id>/<string:action>", endpoint="deposit_mark")
@login_required
def deposit_mark(dep_id: int, action: str):
    dep = db.session.get(DepositRequest, dep_id)
    if not dep:
        flash("Deposit not found.", "error")
        return redirect(url_for("adminbp.deposits_audit"))

    action = (action or "").lower().strip()
    if action not in ("loaded", "reject"):
        flash("Invalid action.", "error")
        return redirect(url_for("adminbp.deposits_audit"))

    if action == "loaded":
        dep.status = "LOADED"
        dep.loaded_at = datetime.utcnow()
        if hasattr(dep, "loaded_by"):
            dep.loaded_by = current_user.id

        if dep.amount and dep.user_id:
            wallet = PlayerBalance.query.filter_by(user_id=dep.user_id).first()
            if wallet:
                wallet.balance = (wallet.balance or 0) + int(dep.amount or 0)

        db.session.commit()

        if dep.user_id:
            notify(dep.user_id, f"✅ Your deposit #{dep.id} of {dep.amount} has been loaded.")
        flash("Deposit marked as LOADED.", "success")
    else:
        dep.status = "REJECTED"
        db.session.commit()
        if dep.user_id:
            notify(dep.user_id, f"⚠️ Your deposit #{dep.id} was rejected. Please contact support.")
        flash("Deposit marked as REJECTED.", "success")

    return redirect(url_for("adminbp.deposits_audit"))

# -------------------- Players list + Delete player -------------------

def _template_exists(name: str) -> bool:
    try:
        render_template(name)
        return True
    except Exception:
        return False

@admin_bp.get("/players", endpoint="admin_players")
@login_required
def admin_players():
    players = (
        User.query.filter_by(role="PLAYER")
        .order_by(User.created_at.desc() if hasattr(User, "created_at") else User.id.desc())
        .limit(200)
        .all()
    )
    if _template_exists("admin_players.html"):
        return render_template("admin_players.html", page_title="Players", players=players)
    # Minimal fallback page if template is missing
    return render_template_string("""
    {% extends "base.html" %}
    {% block content %}
    <div class="shell">
      <div class="panel between">
        <div class="h3">Players</div>
        <a class="btn" href="{{ url_for('adminbp.admin_home') }}">← Back</a>
      </div>
      <div class="panel">
        <table class="tbl">
          <thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Joined</th><th>Action</th></tr></thead>
          <tbody>
            {% for p in players %}
              <tr>
                <td class="mono">#{{ p.id }}</td>
                <td>{{ p.name or '—' }}</td>
                <td>{{ p.email }}</td>
                <td class="mono">{{ p.created_at or '—' }}</td>
                <td>
                  <form method="post" action="{{ url_for('adminbp.delete_player', player_id=p.id) }}" onsubmit="return confirm('Delete this player and all related data?');">
                    <button class="btn btn-ghost" type="submit">Delete</button>
                  </form>
                </td>
              </tr>
            {% else %}
              <tr><td colspan="5" class="muted">No players found.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    {% endblock %}
    """, players=players)

@admin_bp.post("/players/<int:player_id>/delete")
@login_required
def delete_player(player_id: int):
    """Hard delete a player and ALL their data (safe across schema variants)."""
    u = db.session.get(User, player_id)
    if not u or (u.role or "").upper() != "PLAYER":
        flash("Player not found.", "error")
        return redirect(url_for("adminbp.admin_players"))

    try:
        DepositRequest.query.filter_by(user_id=player_id).delete(synchronize_session=False)
        WithdrawRequest.query.filter_by(user_id=player_id).delete(synchronize_session=False)
        GameAccountRequest.query.filter_by(user_id=player_id).delete(synchronize_session=False)
        GameAccount.query.filter_by(user_id=player_id).delete(synchronize_session=False)
        PlayerBalance.query.filter_by(user_id=player_id).delete(synchronize_session=False)
        ReferralCode.query.filter_by(user_id=player_id).delete(synchronize_session=False)
        Notification.query.filter_by(user_id=player_id).delete(synchronize_session=False)

        db.session.delete(u)
        db.session.commit()
        flash("Player and all related data deleted.", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to delete player. See server logs.", "error")
    return redirect(url_for("adminbp.admin_players"))