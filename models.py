from datetime import datetime, timedelta
import secrets
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import event
from sqlalchemy.orm import object_session

db = SQLAlchemy()

# ========================= Users =========================
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    # profile
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    mobile = db.Column(db.String(24), nullable=True, index=True)  # E.164

    # auth
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="PLAYER")  # PLAYER | EMPLOYEE | ADMIN

    # email verification
    email_verified = db.Column(db.Boolean, default=False, index=True)
    email_verified_at = db.Column(db.DateTime, nullable=True)

    # one-time UX flags
    promo_seen = db.Column(db.Boolean, default=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # telegram (optional, for mini-app + staff dashboard)
    telegram_id        = db.Column(db.BigInteger, unique=True, nullable=True, index=True)
    telegram_username  = db.Column(db.String(64), nullable=True, index=True)
    telegram_firstname = db.Column(db.String(120), nullable=True)
    telegram_lastname  = db.Column(db.String(120), nullable=True)
    
    # ===== NEW: Bonus tracking fields =====
    deposit_count = db.Column(db.Integer, default=0)  # Track total deposits
    signup_bonus_claimed = db.Column(db.Boolean, default=False)  # Sign-up bonus claimed?
    signup_bonus_amount = db.Column(db.Float, default=0.0)  # Amount of sign-up bonus
    signup_bonus_claimed_at = db.Column(db.DateTime, nullable=True)  # When claimed
    regular_bonus_last_claimed = db.Column(db.DateTime, nullable=True)  # Last regular bonus
    available_bonus = db.Column(db.Float, default=0.0)  # Total bonus available
    bonus_eligible = db.Column(db.Boolean, default=True, index=True)  # <-- ADD THIS LINE

    # ===== END NEW FIELDS =====

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email} {self.role} verified={self.email_verified}>"


# ========================= One-time tokens =========================
class EmailToken(db.Model):
    __tablename__ = "email_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    purpose = db.Column(db.String(16), default="verify", index=True)
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

    id = db.Column(db.Integer, primary_key=True)

    # deposit methods
    crypto_wallet_text = db.Column(db.String(255), default="USDT-TRC20: YOUR_ADDRESS")
    crypto_qr_url = db.Column(db.String(500), default="")
    chime_handle = db.Column(db.String(255), default="@your-chime")
    chime_qr_url = db.Column(db.String(500), default="")

    # direct pay links
    crypto_pay_url = db.Column(db.String(500), default="")
    chime_pay_url  = db.Column(db.String(500), default="")

    # Cash App
    cashapp_handle  = db.Column(db.String(255), default="$yourcashtag")
    cashapp_qr_url  = db.Column(db.String(500), default="")
    cashapp_pay_url = db.Column(db.String(500), default="")

     # 🔹 NEW – withdraw payout info (shown on player withdraw page)
    withdraw_crypto_address = db.Column(db.String(255), default="")
    withdraw_crypto_qr_url  = db.Column(db.String(500), default="")
    withdraw_chime_handle   = db.Column(db.String(255), default="")
    withdraw_chime_qr_url   = db.Column(db.String(500), default="")

    # promos / limits
    bonus_percent = db.Column(db.Integer, default=0)
    min_redeem = db.Column(db.Integer, default=0)
    max_redeem = db.Column(db.Integer, default=0)

    # social/contact
    whatsapp_url = db.Column(db.String(500), default="")
    telegram_url = db.Column(db.String(500), default="")
    facebook_url = db.Column(db.String(500), default="")
    instagram_url = db.Column(db.String(500), default="")

    # promos
    promo_bonus_line    = db.Column(db.String(300), default="🔥 Today only: +10% bonus on YOLO!")
    promo_referral_line = db.Column(db.String(300), default="🤝 Refer a friend, get +5% bonus!")
    promo_service_line  = db.Column(db.String(300), default="🕐 We're 24/7 and always here to help.")
    promo_trust_line    = db.Column(db.String(300), default="✅ 100% legit & secure.")
    promo_text          = db.Column(db.String(200), default="")

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========================= NEW: Bonus Settings =========================
class BonusSettings(db.Model):
    __tablename__ = "bonus_settings"
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Sign-Up Bonus (First-Time Deposit)
    signup_active = db.Column(db.Boolean, default=True)
    signup_percentage = db.Column(db.Integer, default=60)  # <-- CHANGE from 100 to 60
    signup_max_amount = db.Column(db.Float, default=100.0)
    signup_min_deposit = db.Column(db.Float, default=30.0)  # <-- CHANGE from 20.0 to 30.0
    signup_wagering = db.Column(db.Integer, default=30)
    
    # Regular Bonus
    regular_active = db.Column(db.Boolean, default=True)
    regular_percentage = db.Column(db.Integer, default=20)  # <-- CHANGE from 50 to 20
    regular_max_amount = db.Column(db.Float, default=50.0)
    regular_min_deposit = db.Column(db.Float, default=25.0)  # <-- CHANGE from 10.0 to 25.0
    regular_wagering = db.Column(db.Integer, default=25)
    
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<BonusSetting signup:{self.signup_percentage}% reg:{self.regular_percentage}%>"


# ========================= NEW: Bonus Record =========================
class BonusRecord(db.Model):
    __tablename__ = "bonus_records"
    
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bonus_type = db.Column(db.String(20))  # 'signup' or 'regular'
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='active')  # 'active', 'claimed', 'expired'
    wagering_completed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    player = db.relationship("User", backref=db.backref("bonus_history", lazy=True))
    
    def __repr__(self):
        return f"<BonusRecord {self.id} {self.bonus_type}: ${self.amount}>"


# ========================= Games =========================
class Game(db.Model):
    __tablename__ = "games"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(300), default="")
    icon_url = db.Column(db.String(500), default="")
    download_url = db.Column(db.String(500), nullable=False)
    backend_url = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========================= Wallet =========================
class PlayerBalance(db.Model):
    __tablename__ = "player_balances"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False, index=True)
    balance = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========================= Deposits =========================

class DepositRequest(db.Model):
    __tablename__ = "deposit_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=True, index=True)
    # CHANGE FROM Integer TO Float
    amount = db.Column(db.Float, nullable=False)  # <-- CHANGE THIS LINE
    method = db.Column(db.String(20), nullable=False, default="CRYPTO", index=True)  # CRYPTO|CHIME|CASHAPP
    proof_url = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), default="PENDING", index=True)  # PENDING|RECEIVED|LOADED|REJECTED
    note = db.Column(db.String(300), default="")
    received_at = db.Column(db.DateTime, nullable=True)
    loaded_at = db.Column(db.DateTime, nullable=True)
    loaded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # CHANGE FROM Integer TO Float
    credited_amount = db.Column(db.Float, default=0.0)  # <-- CHANGE THIS LINE

    # ===== NEW: Bonus fields =====
    bonus_percentage = db.Column(db.Float, default=0.0)  # Bonus percentage applied
    bonus_amount = db.Column(db.Float, default=0.0)      # Bonus amount added
    total_credited = db.Column(db.Float, default=0.0)    # amount + bonus
    
    # ===== END NEW FIELDS =====

    # SafePay / automation
    provider = db.Column(db.String(32), default="safepay")
    provider_order_id = db.Column(db.String(128))
    pay_url = db.Column(db.Text)
    backend_url = db.Column(db.Text)
    meta = db.Column(db.JSON, default=dict)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========================= Withdrawals =========================
class WithdrawRequest(db.Model):
    __tablename__ = "withdraw_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=True, index=True)
    amount = db.Column(db.Integer, nullable=False)

    # 👉 optional breakdown (you're setting these in player_bp.py)
    total_amount = db.Column(db.Integer, nullable=True)
    keep_amount  = db.Column(db.Integer, nullable=True)
    tip_amount   = db.Column(db.Integer, nullable=True)

    status = db.Column(db.String(20), default="PENDING", index=True)  # PENDING | APPROVED | REJECTED | PAID
    method = db.Column(db.String(20), default="MANUAL")

    # 👉 this is what employee dashboard template uses: w.address
    address = db.Column(db.String(255), nullable=True)

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

    # PENDING | IN_PROGRESS | PROVIDED | APPROVED | REJECTED
    status = db.Column(db.String(20), default="PENDING", index=True)
    retry_count = db.Column(db.Integer, default=0, nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # who is handling / who approved
    handled_by     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    approved_at    = db.Column(db.DateTime, nullable=True)
      # ⭐ ADD THIS LINE ⭐
    last_error = db.Column(db.Text, nullable=True)


# ========================= Issued Game Accounts =========================
class GameAccount(db.Model):
    __tablename__ = "game_accounts"

    id = db.Column(db.Integer, primary_key=True)

    # request_id nullable for legacy rows
    request_id = db.Column(db.Integer, db.ForeignKey("game_account_requests.id"), nullable=True, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False, index=True)

    account_username = db.Column(db.String(120), nullable=False)
    account_password = db.Column(db.String(120), nullable=False)
    extra = db.Column(db.String(300), default="")

    issued_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    issued_at    = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========================= Ready Accounts Pool =========================
class ReadyAccount(db.Model):
    """
    Employees/Admins can stock ready-to-use credentials per game.
    When a player requests access, the system can instantly claim one.
    """
    __tablename__ = "ready_accounts"

    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False, index=True)

    username = db.Column(db.String(120), nullable=False)
    password = db.Column(db.String(120), nullable=False)
    note     = db.Column(db.String(300), default="")

    is_claimed = db.Column(db.Boolean, default=False, index=True)
    claimed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)  # player_id
    claimed_at = db.Column(db.DateTime, nullable=True)

    added_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)  # stocking employee/admin
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.UniqueConstraint("game_id", "username", name="uq_ready_accounts_game_username"),
    )

# Back-compat alias for blueprints/routes expecting `ReadyAccountPool`
ReadyAccountPool = ReadyAccount


# ========================= External Accounts (NEW) =========================
class ExternalAccount(db.Model):
    """
    Maps our users to external vendor accounts (e.g., GameVault).
    Safe to use with automation jobs to find the right external ID/username.
    """
    __tablename__ = "external_accounts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # e.g., 'gamevault', 'apex', etc.
    vendor = db.Column(db.String(32), nullable=False, index=True)

    # Values as shown in the vendor's panel
    vendor_user_id = db.Column(db.String(64), nullable=True, index=True)     # numeric/string ID from vendor
    vendor_username = db.Column(db.String(120), nullable=True, index=True)   # username created at vendor

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.UniqueConstraint("vendor", "vendor_user_id", name="uq_vendor_and_vendor_user_id"),
    )


def get_or_create_external_account(user_id: int, vendor: str,
                                   vendor_user_id: str | None = None,
                                   vendor_username: str | None = None) -> ExternalAccount:
    """
    Convenience helper: fetch if exists; otherwise create a new mapping.
    Does NOT commit — caller should commit the session.
    """
    rec = (
        ExternalAccount.query
        .filter(ExternalAccount.user_id == user_id, ExternalAccount.vendor == vendor)
        .first()
    )
    if rec:
        if vendor_user_id and not rec.vendor_user_id:
            rec.vendor_user_id = vendor_user_id
        if vendor_username and not rec.vendor_username:
            rec.vendor_username = vendor_username
        return rec

    rec = ExternalAccount(
        user_id=user_id,
        vendor=vendor,
        vendor_user_id=vendor_user_id,
        vendor_username=vendor_username,
        created_at=datetime.utcnow(),
    )
    db.session.add(rec)
    return rec


# ========================= Notifications =========================
class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    message = db.Column(db.String(300), nullable=False)
    is_read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


def notify(user_id: int, message: str):
    """
    Create a notification unless an identical unread one already exists
    very recently (5 minutes). This prevents accidental duplicates.
    """
    try:
        recent = (
            Notification.query
            .filter(Notification.user_id == user_id)
            .filter(Notification.message == message)
            .filter(Notification.is_read == False)  # noqa: E712
            .order_by(Notification.created_at.desc())
            .first()
        )
        if recent and (datetime.utcnow() - (recent.created_at or datetime.utcnow())) < timedelta(minutes=5):
            return
    except Exception:
        db.session.rollback()

    n = Notification(user_id=user_id, message=message, is_read=False, created_at=datetime.utcnow())
    db.session.add(n)
    db.session.commit()


# ========================= Broadcast Chat (legacy/optional) =========================
class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(64), default="global", index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    user_role = db.Column(db.String(20))
    user_name = db.Column(db.String(120))
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


# ========================= Private DM Chat =========================
class DMThread(db.Model):
    __tablename__ = "dm_threads"

    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
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
    # NEW COLUMN: Track message origin
    source = db.Column(db.String(20), default='website')  # 'website' or 'telegram'
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "sender_id": self.sender_id,
            "sender_role": (self.sender_role or "").upper(),
            "body": self.body,
            # Add source to the dictionary output
            "source": self.source,  # <-- NEW LINE
            "created_at": self.created_at.isoformat() + "Z",
        }


# ========================= Referrals =========================
class ReferralCode(db.Model):
    __tablename__ = "referral_codes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False, index=True)
    code = db.Column(db.String(16), unique=True, nullable=False, index=True)  # e.g., AB1234
    clicks = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


def _generate_ref_code_base(name: str) -> str:
    letters = "".join([c for c in (name or "") if c.isalpha()]).upper()
    base = (letters[:2] or "PL")
    if len(base) < 2:
        base = (base + "PL")[:2]
    return base


def get_or_create_referral_for_user(user_id: int) -> ReferralCode:
    rec = ReferralCode.query.filter_by(user_id=user_id).first()
    if rec:
        return rec

    user = db.session.get(User, user_id)
    base = _generate_ref_code_base(user.name if user else "")

    while True:
        suffix = f"{secrets.randbelow(10000):04d}"
        code = f"{base}{suffix}"
        if not ReferralCode.query.filter_by(code=code).first():
            break

    rec = ReferralCode(user_id=user_id, code=code)
    db.session.add(rec)
    db.session.commit()
    return rec


# =============== AUTO-FULFILL GAME REQUESTS ON INSERT ===============
@event.listens_for(GameAccountRequest, "after_insert")
def _autofulfill_game_request_on_insert(mapper, connection, req: GameAccountRequest):
    try:
        sess = object_session(req)
        if sess is None or not req or not req.game_id or not req.user_id:
            return

        q = (
            sess.query(ReadyAccount)
            .filter(ReadyAccount.game_id == req.game_id)
            .order_by(ReadyAccount.created_at.asc())
        )
        if hasattr(ReadyAccount, "is_claimed"):
            q = q.filter(ReadyAccount.is_claimed == False)  # noqa: E712

        ra = q.first()
        if not ra:
            return

        acc = (
            sess.query(GameAccount)
            .filter(GameAccount.user_id == req.user_id, GameAccount.game_id == req.game_id)
            .first()
        )
        if not acc:
            acc = GameAccount(
                user_id=req.user_id,
                game_id=req.game_id,
                created_at=datetime.utcnow(),
            )
            sess.add(acc)

        acc.account_username = getattr(ra, "username", "")
        acc.account_password = getattr(ra, "password", "")
        acc.extra = (getattr(ra, "note", "") or "")

        if hasattr(acc, "request_id"):
            acc.request_id = req.id
        if hasattr(acc, "issued_at"):
            acc.issued_at = datetime.utcnow()

        req.status = "APPROVED"
        if hasattr(req, "approved_at"):
            req.approved_at = datetime.utcnow()

        if hasattr(ra, "is_claimed"):
            ra.is_claimed = True
            if hasattr(ra, "claimed_by"):
                ra.claimed_by = req.user_id
            if hasattr(ra, "claimed_at"):
                ra.claimed_at = datetime.utcnow()
        else:
            sess.delete(ra)

        game = sess.get(Game, req.game_id)
        game_name = game.name if game else f"Game #{req.game_id}"
        sess.add(Notification(
            user_id=req.user_id,
            message=f"✅ Your login for {game_name} is ready. Check My Logins.",
            is_read=False,
            created_at=datetime.utcnow(),
        ))
    except Exception:
        # never block the original insert
        pass


# ========================= NEW: Bonus Helper Functions =========================
def apply_bonus_to_deposit(player, deposit, bonus_type, bonus_settings):
    """
    Apply bonus to a deposit and update player records
    Returns: (bonus_amount, total_credited)
    """
    if bonus_type == 'signup':
        if player.signup_bonus_claimed:
            raise ValueError("Sign-up bonus already claimed")
        
        # ADD MINIMUM DEPOSIT CHECK
        if deposit.amount < bonus_settings.signup_min_deposit:
            raise ValueError(f"Minimum deposit for sign-up bonus is ${bonus_settings.signup_min_deposit}")
        
        percentage = bonus_settings.signup_percentage
        max_amount = bonus_settings.signup_max_amount
        
        # Calculate bonus
        bonus_amount = (deposit.amount * percentage) / 100
        bonus_amount = min(bonus_amount, max_amount)
        
        # Update player
        player.signup_bonus_claimed = True
        player.signup_bonus_amount = bonus_amount
        player.signup_bonus_claimed_at = datetime.utcnow()
        
    else:  # regular bonus
        # ADD MINIMUM DEPOSIT CHECK
        if deposit.amount < bonus_settings.regular_min_deposit:
            raise ValueError(f"Minimum deposit for regular bonus is ${bonus_settings.regular_min_deposit}")
        
        percentage = bonus_settings.regular_percentage
        max_amount = bonus_settings.regular_max_amount
        
        # Calculate bonus
        bonus_amount = (deposit.amount * percentage) / 100
        bonus_amount = min(bonus_amount, max_amount)
        
        player.regular_bonus_last_claimed = datetime.utcnow()
    
    # Update deposit record
    deposit.bonus_percentage = percentage
    deposit.bonus_amount = bonus_amount
    deposit.total_credited = deposit.amount + bonus_amount
    
    # Update player bonus balance
    player.available_bonus += bonus_amount
    player.deposit_count += 1
    
    # Create bonus record
    bonus_record = BonusRecord(
        player_id=player.id,
        bonus_type=bonus_type,
        amount=bonus_amount,
        status='active'
    )
    db.session.add(bonus_record)
    
    # 🔥 CRITICAL: COMMIT THE CHANGES TO DATABASE
    db.session.commit()
    
    return bonus_amount, deposit.total_credited


def get_player_next_bonus(player):
    """
    Determine which bonus the player is eligible for next
    Returns: 'signup' or 'regular'
    """
    if not player.signup_bonus_claimed:
        return 'signup'
    return 'regular'


# ========================= Back-compat alias =========================
# Some blueprints may still import `Deposit` from models.
Deposit = DepositRequest