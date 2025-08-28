# models.py
from datetime import datetime, timedelta
import secrets
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# ========================= Users =========================
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    # profile
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    mobile = db.Column(db.String(24), nullable=True, index=True)  # E.164 (e.g. +15551234567)

    # auth
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="PLAYER")  # PLAYER | EMPLOYEE | ADMIN

    # email verification
    email_verified = db.Column(db.Boolean, default=False, index=True)
    email_verified_at = db.Column(db.DateTime, nullable=True)

    # one-time UX flags
    promo_seen = db.Column(db.Boolean, default=False, index=True)  # show promos once after first login

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # helpers
    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email} {self.role} verified={self.email_verified}>"


# ========================= One-time tokens =========================
class EmailToken(db.Model):
    """
    Email verification token. When the user clicks the link we:
      - look up by token
      - mark the owning user's email_verified True
      - delete the token (or let it expire)
    """
    __tablename__ = "email_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    purpose = db.Column(db.String(16), default="verify", index=True)  # "verify"
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)

    @staticmethod
    def issue(user_id: int, ttl_minutes: int = 60 * 24) -> "EmailToken":
        t = EmailToken(
            user_id=user_id,
            token=secrets.token_urlsafe(32),
            purpose="verify",
            expires_at=datetime.utcnow() + timedelta(minutes=ttl_minutes),
        )
        db.session.add(t)
        db.session.commit()
        return t


class PasswordResetToken(db.Model):
    """
    Password reset token. Flow:
      - issue for user_id
      - email link: /reset?token=...
      - on submit, set new password + delete/mark used tokens
    """
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def issue(user_id: int, ttl_minutes: int = 30) -> "PasswordResetToken":
        t = PasswordResetToken(
            user_id=user_id,
            token=secrets.token_urlsafe(32),
            expires_at=datetime.utcnow() + timedelta(minutes=ttl_minutes),
        )
        db.session.add(t)
        db.session.commit()
        return t


# ========================= Settings =========================
class PaymentSettings(db.Model):
    __tablename__ = "payment_settings"

    id = db.Column(db.Integer, primary_key=True)  # single row (id=1)

    # deposit methods
    crypto_wallet_text = db.Column(db.String(255), default="USDT-TRC20: YOUR_ADDRESS")
    crypto_qr_url = db.Column(db.String(500), default="")
    chime_handle = db.Column(db.String(255), default="@your-chime")
    chime_qr_url = db.Column(db.String(500), default="")

    # promo math / withdrawal limits
    bonus_percent = db.Column(db.Integer, default=0)
    min_redeem = db.Column(db.Integer, default=0)
    max_redeem = db.Column(db.Integer, default=0)

    # social/contact links used by the left rail
    whatsapp_url = db.Column(db.String(500), default="")
    telegram_url = db.Column(db.String(500), default="")
    facebook_url = db.Column(db.String(500), default="")
    instagram_url = db.Column(db.String(500), default="")

    # one-time login promos (admin editable)
    promo_bonus_line    = db.Column(db.String(300), default="🔥 Today only: +10% bonus on YOLO!")
    promo_referral_line = db.Column(db.String(300), default="🤝 Refer a friend, get +5% bonus!")
    promo_service_line  = db.Column(db.String(300), default="🕐 We’re 24/7 and always here to help.")
    promo_trust_line    = db.Column(db.String(300), default="✅ 100% legit & secure.")

    # legacy helper; safe to ignore elsewhere
    promo_text = db.Column(db.String(200), default="")

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========================= Games =========================
class Game(db.Model):
    __tablename__ = "games"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(300), default="")
    icon_url = db.Column(db.String(500), default="")
    download_url = db.Column(db.String(500), nullable=False)
    backend_url = db.Column(db.String(500), nullable=True)  # merchant/staff URL (optional)
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========================= Wallet =========================
class PlayerBalance(db.Model):
    __tablename__ = "player_balances"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False, index=True)
    balance = db.Column(db.Integer, default=0)  # store in smallest unit
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========================= Deposits =========================
class DepositRequest(db.Model):
    __tablename__ = "deposit_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=True, index=True)
    amount = db.Column(db.Integer, nullable=False)
    method = db.Column(db.String(20), nullable=False)  # 'CRYPTO' | 'CHIME'
    proof_url = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), default="PENDING", index=True)  # PENDING | RECEIVED | LOADED | REJECTED
    note = db.Column(db.String(300), default="")
    received_at = db.Column(db.DateTime, nullable=True)
    loaded_at = db.Column(db.DateTime, nullable=True)
    loaded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)  # employee/admin id
    credited_amount = db.Column(db.Integer, default=0)  # amount + bonus actually credited
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========================= Withdrawals =========================
class WithdrawRequest(db.Model):
    __tablename__ = "withdraw_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=True, index=True)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="PENDING", index=True)  # PENDING | APPROVED | REJECTED | PAID
    method = db.Column(db.String(20), default="MANUAL")
    note = db.Column(db.String(300), default="")
    acted_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    acted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========================= Game Access Requests =========================
class GameAccountRequest(db.Model):
    __tablename__ = "game_account_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False, index=True)

    # PENDING | IN_PROGRESS | PROVIDED | APPROVED | REJECTED (we allow APPROVED for dashboards)
    status = db.Column(db.String(20), default="PENDING", index=True)

    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # NEW: who is handling / who approved (needed for admin dashboard)
    handled_by     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    approved_at    = db.Column(db.DateTime, nullable=True)


# ========================= Issued Game Accounts =========================
class GameAccount(db.Model):
    __tablename__ = "game_accounts"

    id = db.Column(db.Integer, primary_key=True)

    # make request_id nullable=True for legacy rows; views handle presence if available
    request_id = db.Column(db.Integer, db.ForeignKey("game_account_requests.id"), nullable=True, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False, index=True)

    account_username = db.Column(db.String(120), nullable=False)
    account_password = db.Column(db.String(120), nullable=False)
    extra = db.Column(db.String(300), default="")

    # who issued this login (so admin dashboard can show "Issued By")
    issued_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    issued_at    = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========================= Notifications =========================
class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    message = db.Column(db.String(300), nullable=False)
    is_read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


def notify(user_id: int, message: str):
    n = Notification(user_id=user_id, message=message, is_read=False)
    db.session.add(n)
    db.session.commit()


# ========================= Broadcast Chat (legacy/optional) =========================
class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(64), default="global", index=True)   # "global", "game-12", etc.
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    user_role = db.Column(db.String(20))                             # PLAYER / EMPLOYEE / ADMIN / GUEST
    user_name = db.Column(db.String(120))                            # snapshot for quick rendering
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "room": self.room,
            "user_id": self.user_id,
            "user_role": self.user_role,
            "user_name": self.user_name,
            "message": self.message,
            "created_at": self.created_at.isoformat() + "Z",
        }


# ========================= Private DM Chat (player ↔ employee) =========================
class DMThread(db.Model):
    __tablename__ = "dm_threads"

    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)  # assigned staff
    status = db.Column(db.String(16), default="OPEN", index=True)  # OPEN | CLOSED
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_msg_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self) -> str:
        return f"<DMThread {self.id} player={self.player_id} emp={self.employee_id} {self.status}>"


class DMMessage(db.Model):
    __tablename__ = "dm_messages"

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("dm_threads.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    sender_role = db.Column(db.String(20))  # PLAYER | EMPLOYEE | ADMIN
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "sender_id": self.sender_id,
            "sender_role": (self.sender_role or "").upper(),
            "body": self.body,
            "created_at": self.created_at.isoformat() + "Z",
        }


# ========================= Referrals (NEW) =========================
class ReferralCode(db.Model):
    __tablename__ = "referral_codes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False, index=True)
    code = db.Column(db.String(16), unique=True, nullable=False, index=True)  # e.g., AB1234
    clicks = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


def _generate_ref_code_base(name: str) -> str:
    """
    Take first two alphabetic characters from the name (uppercased).
    Fallback to 'PL' if not available or too short.
    """
    letters = "".join([c for c in (name or "") if c.isalpha()]).upper()
    base = (letters[:2] or "PL")
    if len(base) < 2:
        base = (base + "PL")[:2]
    return base


def get_or_create_referral_for_user(user_id: int) -> ReferralCode:
    """
    Ensure a user has a unique 6-char referral code:
      - 2 letters from name (uppercased)
      - 4 digit random number
    Returns the ReferralCode row.
    """
    rec = ReferralCode.query.filter_by(user_id=user_id).first()
    if rec:
        return rec

    user = db.session.get(User, user_id)
    base = _generate_ref_code_base(user.name if user else "")

    # ensure uniqueness
    while True:
        suffix = f"{secrets.randbelow(10000):04d}"
        code = f"{base}{suffix}"
        if not ReferralCode.query.filter_by(code=code).first():
            break

    rec = ReferralCode(user_id=user_id, code=code)
    db.session.add(rec)
    db.session.commit()
    return rec