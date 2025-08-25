# app.py
import os
from flask import Flask, render_template, redirect, url_for
from dotenv import load_dotenv
from flask_login import LoginManager, current_user
from sqlalchemy import text, inspect as sqla_inspect

from models import (
    db, User, Notification, PlayerBalance, Game, GameAccount,
    DepositRequest, PaymentSettings
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
    from chat_bp import chat_bp  # create chat_bp later if you want live chat endpoints
except Exception:
    chat_bp = None

load_dotenv()


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-key")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///casino.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    # ------------------ tiny kv fallback (shared) ------------------
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
        """Return the first non-empty attribute if it exists on obj."""
        for n in names:
            if hasattr(obj, n):
                val = getattr(obj, n)
                if val not in (None, ""):
                    return val
        return default

    # alias sets for tolerant reads (match admin/player code paths)
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
        # unread notifications
        if current_user.is_authenticated:
            cnt = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        else:
            cnt = 0

        # one-row settings (if missing, other code uses kv fallback)
        settings = db.session.get(PaymentSettings, 1)

        # ensure wallet exists for signed-in users
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

    # ------------------ Home / Lobby (ONE PAGE) ------------------
    @app.route("/")
    @app.route("/lobby")
    def index():
        """
        Same lobby for guests and signed-in users.
        - News/promo banner + bonus % from PaymentSettings OR kv_store fallback
        - Today's Trending from saved id CSV (order preserved)
        """
        # active games (for catalog grid)
        games = Game.query.filter_by(is_active=True).order_by(
            Game.created_at.desc() if hasattr(Game, "created_at") else Game.id.desc()
        ).all()

        settings = db.session.get(PaymentSettings, 1)

        # -------- news / promo (aliases + kv fallback) --------
        promo_line1 = _first_attr(settings, *PROMO1_ALIASES, default=None) or _kv_first(*PROMO1_ALIASES, default="")
        promo_line2 = _first_attr(settings, *PROMO2_ALIASES, default=None) or _kv_first(*PROMO2_ALIASES, default="")
        bonus_percent = getattr(settings, "bonus_percent", None)
        if bonus_percent in (None, ""):
            bp = _kv_first("bonus_percent")
            bonus_percent = int(bp) if (bp and str(bp).isdigit()) else 0

        # -------- trending (aliases + kv fallback; preserve saved order) -------
        raw_csv = _first_attr(settings, *TREND_ALIASES, default=None)
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

        # -------- render (guest vs signed-in) --------
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

        # signed-in extras
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

    # ------------------ error pages ------------------
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("base.html", error="403 Forbidden", page_title="Forbidden"), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("base.html", error="404 Not Found", page_title="Not Found"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("base.html", error="500 Internal Server Error", page_title="Server Error"), 500

    # ------------------ first run bootstrap ------------------
    with app.app_context():
        db.create_all()
        _ensure_kv()  # make sure kv_store exists so admin/guest ticker works Day 1

        # --- one-time schema patch: add games.backend_url if missing ---
        try:
            insp = sqla_inspect(db.engine)
            game_cols = {c["name"] for c in insp.get_columns("games")}
            dialect = db.engine.dialect.name

            if "backend_url" not in game_cols:
                if dialect in ("postgresql", "postgres"):
                    db.session.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS backend_url VARCHAR(500)"))
                elif dialect in ("mysql", "mariadb"):
                    db.session.execute(text("ALTER TABLE games ADD COLUMN backend_url VARCHAR(500) NULL"))
                else:  # sqlite and others
                    db.session.execute(text("ALTER TABLE games ADD COLUMN backend_url VARCHAR(500)"))
                db.session.commit()
                print("✅ Added games.backend_url column")
        except Exception as e:
            db.session.rollback()
            print(f"⚠️ Skipped backend_url patch: {e}")

        seed_admin()

    return app


def seed_admin():
    """Create the .env-configured admin if missing."""
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


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=bool(int(os.getenv("FLASK_DEBUG", "1"))))