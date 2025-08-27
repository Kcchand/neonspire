# app.py
import os
from flask import Flask, render_template
from dotenv import load_dotenv
from flask_login import LoginManager, current_user
from sqlalchemy import text, inspect as sqla_inspect
from flask_socketio import SocketIO, emit
from flask_mail import Mail

# models (import every model that needs a table so create_all() sees them)
from models import (
    db,
    User,
    Notification,
    PlayerBalance,
    Game,
    GameAccount,
    DepositRequest,
    PaymentSettings,
    EmailToken,
    PasswordResetToken,
    WithdrawRequest,
    GameAccountRequest,
    ChatMessage,
    DMThread,
    DMMessage,
    ReferralCode,  # NEW: ensures referral_codes table is created
)

# blueprints
from auth import auth_bp
from dashboard import dash_bp
from admin_bp import admin_bp
from employee_bp import employee_bp
from notifications import notify_bp
from player_bp import player_bp  # player features live on /player

# (optional) live chat blueprint – only registers if present
try:
    from chat_bp import chat_bp
except Exception:
    chat_bp = None

load_dotenv()

# ------------------------------------------------------------------
# Socket.IO
# ------------------------------------------------------------------
ASYNC_MODE = os.getenv("ASYNC_MODE", "threading").strip().lower()
socketio = SocketIO(async_mode=ASYNC_MODE, cors_allowed_origins="*")

# ------------------------------------------------------------------
# Mail (exported so other modules can `from app import mail`)
# ------------------------------------------------------------------
mail = Mail()


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-key")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///casino.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Mail config
    app.config.update(
        MAIL_SERVER=os.getenv("MAIL_SERVER", "localhost"),
        MAIL_PORT=int(os.getenv("MAIL_PORT", "25")),
        MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
        MAIL_USE_TLS=os.getenv("MAIL_USE_TLS", "false").lower() == "true",
        MAIL_USE_SSL=os.getenv("MAIL_USE_SSL", "false").lower() == "true",
        MAIL_DEFAULT_SENDER=os.getenv("MAIL_DEFAULT_SENDER", "no-reply@neonspire.local"),
    )

    db.init_app(app)
    socketio.init_app(app)
    mail.init_app(app)

    # ------------------ tiny kv fallback (shared) ------------------
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
            else:  # sqlite / other
                db.session.execute(text("""
                    CREATE TABLE IF NOT EXISTS kv_store (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """))
            db.session.commit()
        except Exception:
            db.session.rollback()

    def _kv_get(k: str):
        _ensure_kv()
        try:
            row = db.session.execute(text("SELECT value FROM kv_store WHERE key=:k"), {"k": k}).fetchone()
            return row[0] if row else None
        except Exception:
            db.session.rollback()
            return None

    def _kv_first(*keys, default=None):
        for k in keys:
            v = _kv_get(k)
            if v not in (None, ""):
                return v
        return default

    def _first_attr(obj, *names, default=None):
        for n in names:
            if hasattr(obj, n):
                val = getattr(obj, n)
                if val not in (None, ""):
                    return val
        return default

    PROMO1_ALIASES = ("promo_line1", "news_line1", "ticker_line1", "headline1", "news1")
    PROMO2_ALIASES = ("promo_line2", "news_line2", "ticker_line2", "headline2", "news2")
    TREND_ALIASES  = ("trending_game_ids", "trending_ids", "trending_csv", "trending")

    # ------------------ login manager ------------------
    login_manager = LoginManager()
    login_manager.login_view = "auth.login_get"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # ------------------ globals for templates ------------------
    @app.context_processor
    def inject_globals():
        if current_user.is_authenticated:
            cnt = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        else:
            cnt = 0

        settings = db.session.get(PaymentSettings, 1)

        wallet = None
        if current_user.is_authenticated:
            wallet = PlayerBalance.query.filter_by(user_id=current_user.id).first()
            if not wallet:
                wallet = PlayerBalance(user_id=current_user.id, balance=0)
                db.session.add(wallet)
                db.session.commit()

        return dict(unread_count=cnt, settings=settings, wallet=wallet)

    # ------------------ blueprints ------------------
    app.register_blueprint(auth_bp)
    app.register_blueprint(dash_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(employee_bp)
    app.register_blueprint(notify_bp)
    app.register_blueprint(player_bp)
    if chat_bp:
        app.register_blueprint(chat_bp)

    # ------------------ health ------------------
    @app.get("/health")
    def health():
        return {"ok": True, "service": "web", "env": os.getenv("NODE_ENV", "production")}

    # ------------------ Home / Lobby ------------------
    @app.route("/")
    @app.route("/lobby")
    def index():
        games = Game.query.filter_by(is_active=True).order_by(
            Game.created_at.desc() if hasattr(Game, "created_at") else Game.id.desc()
        ).all()

        settings = db.session.get(PaymentSettings, 1)

        promo_line1 = _first_attr(settings, *PROMO1_ALIASES) or _kv_first(*PROMO1_ALIASES, default="")
        promo_line2 = _first_attr(settings, *PROMO2_ALIASES) or _kv_first(*PROMO2_ALIASES, default="")
        bonus_percent = getattr(settings, "bonus_percent", None)
        if bonus_percent in (None, ""):
            bp = _kv_first("bonus_percent")
            bonus_percent = int(bp) if (bp and str(bp).isdigit()) else 0

        raw_csv = _first_attr(settings, *TREND_ALIASES)
        if raw_csv in (None, ""):
            raw_csv = _kv_first(*TREND_ALIASES, default="") or ""
        trending_ids = []
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

        if not current_user.is_authenticated:
            return render_template(
                "index.html",
                is_player_home=False,
                games=games,
                trending_games=trending_games,
                promo_line1=promo_line1,
                promo_line2=promo_line2,
                bonus_percent=bonus_percent,
                settings=settings,
                page_title="NeonSpire Casino",
            )

        wallet = PlayerBalance.query.filter_by(user_id=current_user.id).first()
        if not wallet:
            wallet = PlayerBalance(user_id=current_user.id, balance=0)
            db.session.add(wallet)
            db.session.commit()

        my_accounts = GameAccount.query.filter_by(user_id=current_user.id).all()
        accounts_by_game = {}
        for a in my_accounts:
            accounts_by_game.setdefault(a.game_id, []).append(a)

        recent_credits = (
            DepositRequest.query.filter_by(user_id=current_user.id, status="LOADED")
            .order_by(DepositRequest.loaded_at.desc()).limit(5).all()
        )
        notes = (
            Notification.query.filter_by(user_id=current_user.id)
            .order_by(Notification.created_at.desc()).limit(10).all()
        )

        return render_template(
            "index.html",
            is_player_home=True,
            wallet=wallet,
            games=games,
            trending_games=trending_games,
            accounts_by_game=accounts_by_game,
            settings=settings,
            recent_credits=recent_credits,
            notifications=notes,
            promo_line1=promo_line1,
            promo_line2=promo_line2,
            bonus_percent=bonus_percent,
            page_title="NeonSpire Casino",
        )

    # ------------------ Socket.IO sample ------------------
    @socketio.on("connect")
    def _on_connect():
        emit("welcome", {"msg": f"Connected (async={ASYNC_MODE})"})

    # ------------------ first run bootstrap & safe patches ------------------
    with app.app_context():
        # Create tables for all imported models
        db.create_all()
        _ensure_kv()

        insp = sqla_inspect(db.engine)
        dialect = db.engine.dialect.name

        def _has_col(table: str, col: str) -> bool:
            try:
                return col in {c["name"] for c in insp.get_columns(table)}
            except Exception:
                return False

        def _add_col(stmt_sqlite: str, stmt_pg: str = None, stmt_mysql: str = None):
            try:
                if dialect in ("postgresql", "postgres"):
                    db.session.execute(text(stmt_pg or stmt_sqlite))
                elif dialect in ("mysql", "mariadb"):
                    db.session.execute(text(stmt_mysql or stmt_sqlite))
                else:  # sqlite
                    db.session.execute(text(stmt_sqlite))
                db.session.commit()
            except Exception:
                db.session.rollback()

        # --- games.backend_url (legacy patch) ---
        if not _has_col("games", "backend_url"):
            _add_col(
                "ALTER TABLE games ADD COLUMN backend_url VARCHAR(500)",
                "ALTER TABLE games ADD COLUMN IF NOT EXISTS backend_url VARCHAR(500)",
                "ALTER TABLE games ADD COLUMN backend_url VARCHAR(500) NULL",
            )

        # --- users table new fields (mobile, email_verified, email_verified_at, promo_seen) ---
        if not _has_col("users", "mobile"):
            _add_col("ALTER TABLE users ADD COLUMN mobile VARCHAR(24)")
        if not _has_col("users", "email_verified"):
            _add_col("ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 0")
        if not _has_col("users", "email_verified_at"):
            _add_col("ALTER TABLE users ADD COLUMN email_verified_at DATETIME")
        if not _has_col("users", "promo_seen"):
            _add_col("ALTER TABLE users ADD COLUMN promo_seen BOOLEAN DEFAULT 0")

        # --- payment_settings social links & promo lines ---
        ps_cols = {c["name"] for c in insp.get_columns("payment_settings")}
        needed_ps = {
            "whatsapp_url": "ALTER TABLE payment_settings ADD COLUMN whatsapp_url VARCHAR(500)",
            "telegram_url": "ALTER TABLE payment_settings ADD COLUMN telegram_url VARCHAR(500)",
            "facebook_url": "ALTER TABLE payment_settings ADD COLUMN facebook_url VARCHAR(500)",
            "instagram_url": "ALTER TABLE payment_settings ADD COLUMN instagram_url VARCHAR(500)",
            "promo_bonus_line": "ALTER TABLE payment_settings ADD COLUMN promo_bonus_line VARCHAR(300)",
            "promo_referral_line": "ALTER TABLE payment_settings ADD COLUMN promo_referral_line VARCHAR(300)",
            "promo_service_line": "ALTER TABLE payment_settings ADD COLUMN promo_service_line VARCHAR(300)",
            "promo_trust_line": "ALTER TABLE payment_settings ADD COLUMN promo_trust_line VARCHAR(300)",
        }
        for col, stmt in needed_ps.items():
            if col not in ps_cols:
                _add_col(stmt)

        # Ensure a row exists in payment_settings with id=1
        ps = db.session.get(PaymentSettings, 1)
        if not ps:
            ps = PaymentSettings(
                id=1,
                bonus_percent=0,
                min_redeem=0,
                max_redeem=0,
                whatsapp_url="",
                telegram_url="",
                facebook_url="",
                instagram_url="",
            )
            db.session.add(ps)
            db.session.commit()

        seed_admin()

    return app


def seed_admin():
    email = os.getenv("ADMIN_EMAIL")
    password = os.getenv("ADMIN_PASSWORD")
    if not email or not password:
        return
    if not User.query.filter_by(email=email.lower()).first():
        admin = User(name="Admin", email=email.lower(), role="ADMIN")
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        print("✅ Admin user created from .env")


# module-level app for gunicorn (app:app)
app = create_app()

if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5000, debug=bool(int(os.getenv("FLASK_DEBUG", "1"))))