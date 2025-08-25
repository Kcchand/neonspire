# models.py
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# ========================= Users =========================
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="PLAYER")  # PLAYER | EMPLOYEE | ADMIN
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

    def __repr__(self) -> str:  # helpful during debugging
        return f"<User {self.id} {self.email} {self.role}>"

# ========================= Settings =========================
class PaymentSettings(db.Model):
    __tablename__ = "payment_settings"

    id = db.Column(db.Integer, primary_key=True)  # single row (id=1)
    crypto_wallet_text = db.Column(db.String(255), default="USDT-TRC20: YOUR_ADDRESS")
    crypto_qr_url = db.Column(db.String(500), default="")
    chime_handle = db.Column(db.String(255), default="@your-chime")
    chime_qr_url = db.Column(db.String(500), default="")
    bonus_percent = db.Column(db.Integer, default=0)
    min_redeem = db.Column(db.Integer, default=0)
    max_redeem = db.Column(db.Integer, default=0)
    promo_text = db.Column(db.String(200), default="")  # legacy helper; safe to ignore elsewhere
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
    status = db.Column(db.String(20), default="PENDING", index=True)  # PENDING | IN_PROGRESS | PROVIDED | REJECTED
    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ========================= Issued Game Accounts =========================
class GameAccount(db.Model):
    __tablename__ = "game_accounts"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("game_account_requests.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False, index=True)
    account_username = db.Column(db.String(120), nullable=False)
    account_password = db.Column(db.String(120), nullable=False)
    extra = db.Column(db.String(300), default="")
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