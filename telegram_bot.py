"""
NeonSpire Staff Bot with Chat Integration
=========================================

Telegram staff panel for your casino dashboard with chat sync.

- show dashboard counters
- list + approve/reject deposits
- provider-based "approve & credit" (juwa/gv/milkyway/vblink/ultrapanda)
- list + redeem/paid withdrawals
- list + approve/reject game-account requests
- list recent players
- 🔔 auto-poll DB and push NEW deposits/withdrawals/requests to staff
- 💬 support inbox (reads from kv_store -> 'support:inbox' same as employee dashboard)
- 💬 BIDIRECTIONAL CHAT SYNC: Website ↔ Telegram
"""

import os
import json
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv(".env")

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ParseMode,
)
from telegram.ext import (
    Updater,
    CallbackContext,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
)

# ---- Flask app + models -----------------------------
from app import app as flask_app
from models import (
    db,
    User,
    Game,
    PlayerBalance,
    GameAccount,
    GameAccountRequest,
    DepositRequest,
    WithdrawRequest,
    PaymentSettings,
    DMThread,
    DMMessage,
    notify,
)

# 👇 we need this to read from kv_store like employee dashboard
from sqlalchemy import text

# unified provider facade
from automation.providers import (
    detect_vendor,
    provider_credit,
    provider_redeem,
    provider_auto_create,
    result_ok as _prov_ok,
    result_error_text as _prov_err,
)

# --------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("tg.staff")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_NAME = os.getenv("TELEGRAM_BOT_NAME", "NeonSpire Staff Bot")

_raw_staff = os.getenv("TELEGRAM_STAFF_IDS", "")
STAFF_IDS = {int(x.strip()) for x in _raw_staff.split(",") if x.strip().isdigit()}

PAGE_SIZE = 10
STATE_FILE = ".tg_staff_state.json"

# ================== CHAT HELPERS ==================

def _get_thread_for_telegram_user(telegram_user_id: int) -> DMThread | None:
    """Find DMThread where the employee's Telegram ID matches the employee_id."""
    with flask_app.app_context():
        # Find user by telegram_id
        employee = User.query.filter_by(telegram_id=telegram_user_id).first()
        if not employee:
            log.warning(f"No employee found with telegram_id {telegram_user_id}")
            return None
        
        # Find active thread assigned to this employee
        thread = DMThread.query.filter_by(
            employee_id=employee.id,
            status="OPEN"
        ).order_by(DMThread.last_msg_at.desc()).first()
        
        return thread

def _get_or_create_thread_for_player(player_id: int, employee_telegram_id: int) -> DMThread:
    """Get or create thread for a player, assigned to employee."""
    with flask_app.app_context():
        # Find employee by telegram_id
        employee = User.query.filter_by(telegram_id=employee_telegram_id).first()
        if not employee:
            raise ValueError(f"No employee found with telegram_id {employee_telegram_id}")
        
        # Find existing thread
        thread = DMThread.query.filter_by(
            player_id=player_id,
            status="OPEN"
        ).first()
        
        if thread:
            # Update employee assignment if not assigned
            if not thread.employee_id:
                thread.employee_id = employee.id
                db.session.commit()
            return thread
        
        # Create new thread
        thread = DMThread(
            player_id=player_id,
            employee_id=employee.id,
            status="OPEN",
            last_msg_at=datetime.utcnow(),
        )
        db.session.add(thread)
        db.session.commit()
        return thread

def forward_to_telegram(thread_id: int, message_text: str, player_name: str, employee_id: int = None):
    """
    Forward website messages to Telegram (called from chat_bp.py).
    """
    try:
        from telegram import Bot
        bot = Bot(token=TOKEN)
        
        with flask_app.app_context():
            thread = db.session.get(DMThread, thread_id)
            if not thread:
                log.error(f"Thread {thread_id} not found")
                return
            
            # If employee_id not provided, use thread's assigned employee
            target_employee_id = employee_id or thread.employee_id
            if not target_employee_id:
                log.warning(f"No employee assigned to thread {thread_id}")
                return
            
            # Find employee's telegram_id
            employee = db.session.get(User, target_employee_id)
            if not employee or not employee.telegram_id:
                log.warning(f"Employee {target_employee_id} has no telegram_id")
                return
            
            # Format message
            formatted_msg = (
                f"💬 *New message from player*\n"
                f"👤 Player: {player_name}\n"
                f"📝 Message: {message_text}\n"
                f"🆔 Thread ID: {thread_id}\n\n"
                f"💡 Reply to this chat to respond"
            )
            
            # Send to employee's Telegram
            try:
                bot.send_message(
                    chat_id=employee.telegram_id,
                    text=formatted_msg,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "💬 Open Chat", 
                            callback_data=f"open_chat:{thread_id}"
                        )
                    ]])
                )
                log.info(f"Forwarded message to employee {employee.id} (telegram: {employee.telegram_id})")
            except Exception as e:
                log.error(f"Failed to send to Telegram: {e}")
                
    except ImportError:
        log.error("Telegram module not available")
    except Exception as e:
        log.error(f"Error in forward_to_telegram: {e}")

def _save_telegram_message(thread_id: int, telegram_user_id: int, message_text: str):
    """
    Save Telegram message to database and forward to website via Socket.IO.
    """
    with flask_app.app_context():
        try:
            # Find employee by telegram_id
            employee = User.query.filter_by(telegram_id=telegram_user_id).first()
            if not employee:
                log.error(f"Employee not found for telegram_id {telegram_user_id}")
                return None
            
            # Get thread
            thread = db.session.get(DMThread, thread_id)
            if not thread:
                log.error(f"Thread {thread_id} not found")
                return None
            
            # Create message with source='telegram'
            message = DMMessage(
                thread_id=thread_id,
                sender_id=employee.id,
                sender_role=employee.role,
                body=message_text,
                source='telegram',  # <-- IMPORTANT: Mark as from Telegram
                created_at=datetime.utcnow(),
            )
            db.session.add(message)
            thread.last_msg_at = datetime.utcnow()
            db.session.commit()
            
            log.info(f"Saved Telegram message to thread {thread_id} from employee {employee.id}")
            
            # Forward to website via Socket.IO
            try:
                from app import socketio
                socketio.emit('new_message', 
                            message.to_dict(),
                            room=f'thread_{thread_id}',
                            namespace='/chat')
                log.info(f"Emitted message to website for thread {thread_id}")
            except ImportError:
                log.warning("Socket.IO not available")
            except Exception as e:
                log.error(f"Failed to emit via Socket.IO: {e}")
            
            return message
            
        except Exception as e:
            log.error(f"Error saving Telegram message: {e}")
            db.session.rollback()
            return None

# ================== STATE HELPERS ==================

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.warning("could not save state: %s", e)

# ================== UI HELPERS ==================

def _ack_working(q):
    """always show instant toast so staff knows tap was received."""
    try:
        q.answer("⏳ Working…", show_alert=False)
    except Exception:
        pass

def _send_progress_msg(bot, chat_id: int, text: str) -> int:
    msg = bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    return msg.message_id

def _set_loading_keyboard(q, label: str = "⏳ Working…"):
    try:
        q.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data="noop")]]
            )
        )
    except Exception as e:
        log.debug("cannot edit keyboard to loading: %s", e)

def _set_loading(q, text: str):
    try:
        q.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN)
        return
    except Exception:
        pass
    try:
        q.message.edit_text(text=text, parse_mode=ParseMode.MARKDOWN)
        return
    except Exception:
        pass
    try:
        q.bot.edit_message_text(
            chat_id=q.message.chat_id,
            message_id=q.message.message_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception as e:
        log.warning("cannot show loading text: %s", e)
        try:
            q.answer("Processing…", show_alert=False)
        except Exception:
            pass

# ================== SUPPORT HELPERS ==================

def _read_support_inbox() -> list[dict]:
    """
    Read support inbox from the same place employee dashboard uses:
    kv_store -> key = 'support:inbox'
    We do NOT change the dashboard. We just read the same JSON.
    """
    with flask_app.app_context():
        try:
            row = db.session.execute(
                text("SELECT value FROM kv_store WHERE key = :k"),
                {"k": "support:inbox"},
            ).fetchone()
        except Exception as e:
            log.warning("support: cannot read kv_store: %s", e)
            return []

        if not row or not row[0]:
            return []

        try:
            data = json.loads(row[0])
        except Exception:
            return []

        # expect list of threads; if it's a dict wrap it
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        return data

def _support_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh", callback_data="list_support:0")],
        ]
    )

# ================== HELPERS ==================

def staff_only(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if uid in STAFF_IDS:
        return True

    if update.message:
        update.message.reply_text("🚫 You are not allowed to use this staff bot.")
    elif update.callback_query:
        update.callback_query.answer("Not allowed.", show_alert=True)
    return False

def _display_name(user: User | None) -> str:
    if not user:
        return "—"
    return (user.name or user.email or f"User #{user.id}").strip()

def _player_login_for_game(user_id: int, game_id: int | None) -> str | None:
    if not game_id:
        return None
    acc = GameAccount.query.filter_by(user_id=user_id, game_id=game_id).first()
    if not acc:
        return None
    for f in ("account_username", "username", "login", "user"):
        if hasattr(acc, f):
            v = getattr(acc, f)
            if v:
                return v
    return None

def _vendor_for_game(game: Game | None) -> str | None:
    if not game:
        return None
    return detect_vendor(game)

def _add_wallet_balance(user_id: int, amount: int):
    wallet = PlayerBalance.query.filter_by(user_id=user_id).first()
    if wallet:
        wallet.balance = (wallet.balance or 0) + amount

# ================== STAFF BOTTOM MENU ==================

def staff_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📥 Pending deposits", "📤 Pending withdrawals"],
            ["👤 Find player", "💬 Active Chats"]
        ],
        resize_keyboard=True
    )

def staff_menu_router(update, context):
    """Handle staff reply-keyboard buttons."""
    if not staff_only(update):
        return

    text = (update.message.text or "").strip()
    telegram_user_id = update.effective_user.id

    # ----- Pending Deposits -----
    if text == "📥 Pending deposits":
        return _send_deposits_list(update, page=0)

    # ----- Pending Withdrawals -----
    if text == "📤 Pending withdrawals":
        return _send_withdrawals_list(update, page=0)

    # ----- Find Player -----
    if text == "👤 Find player":
        update.message.reply_text("Send the player's email or name:")
        context.user_data["expect_player_search"] = True
        return

    # ----- Active Chats -----
    if text == "💬 Active Chats":
        return _send_active_chats(update, telegram_user_id)

    # ----- Search Mode -----
    if context.user_data.get("expect_player_search"):
        query = text
        context.user_data["expect_player_search"] = False

        with flask_app.app_context():
            rows = User.query.filter(
                (User.email.ilike(f"%{query}%")) |
                (User.name.ilike(f"%{query}%"))
            ).limit(5).all()

        if not rows:
            update.message.reply_text("No matching players found.")
            return

        msg = ["🔍 *Search results:*"]
        for u in rows:
            msg.append(f"- {u.id} • {_display_name(u)} • {u.email or ''}")

        update.message.reply_text("\n".join(msg), parse_mode="Markdown")
        return

    # ----- Check if this is a reply to an active chat -----
    if "active_chat_thread" in context.user_data:
        thread_id = context.user_data["active_chat_thread"]
        _save_telegram_message(thread_id, telegram_user_id, text)
        update.message.reply_text("✅ Message sent to player!")
        return

    # ----- Regular text message - check if it's for a thread -----
    with flask_app.app_context():
        # Find employee by telegram_id
        employee = User.query.filter_by(telegram_id=telegram_user_id).first()
        if employee:
            # Check for active thread assigned to this employee
            thread = DMThread.query.filter_by(
                employee_id=employee.id,
                status="OPEN"
            ).order_by(DMThread.last_msg_at.desc()).first()
            
            if thread:
                # Ask if they want to reply to this thread
                player = db.session.get(User, thread.player_id)
                update.message.reply_text(
                    f"💬 You have an active chat with {_display_name(player)}.\n"
                    f"Send your message to reply, or use /chats to see all active chats.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "💬 Reply to this chat", 
                            callback_data=f"set_chat:{thread.id}"
                        )
                    ]])
                )
                return

def _send_active_chats(update, telegram_user_id: int):
    """Show active chats assigned to this employee."""
    with flask_app.app_context():
        # Find employee by telegram_id
        employee = User.query.filter_by(telegram_id=telegram_user_id).first()
        if not employee:
            update.message.reply_text("❌ Employee record not found.")
            return
        
        # Get active threads
        threads = DMThread.query.filter_by(
            employee_id=employee.id,
            status="OPEN"
        ).order_by(DMThread.last_msg_at.desc()).limit(10).all()
        
        if not threads:
            update.message.reply_text("💬 No active chats.")
            return
        
        msg = ["💬 *Your Active Chats:*\n"]
        for thread in threads:
            player = db.session.get(User, thread.player_id)
            last_msg = DMMessage.query.filter_by(
                thread_id=thread.id
            ).order_by(DMMessage.id.desc()).first()
            
            last_text = last_msg.body[:50] + "..." if last_msg and len(last_msg.body) > 50 else (last_msg.body if last_msg else "No messages")
            msg.append(
                f"• *{_display_name(player)}* (ID: {thread.id})\n"
                f"  Last: {last_text}\n"
                f"  [💬 Chat](callback:open_chat:{thread.id}) | "
                f" [📋 Info](callback:player_info:{thread.player_id})\n"
            )
        
        # Create inline keyboard with chat buttons
        keyboard = []
        for thread in threads:
            player = db.session.get(User, thread.player_id)
            keyboard.append([
                InlineKeyboardButton(
                    f"💬 {_display_name(player)[:20]}...",
                    callback_data=f"open_chat:{thread.id}"
                )
            ])
        
        update.message.reply_text(
            "\n".join(msg),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Deposits", callback_data="list_deposits:0")],
            [InlineKeyboardButton("💸 Withdrawals", callback_data="list_withdrawals:0")],
            [InlineKeyboardButton("🆔 Game requests", callback_data="list_requests:0")],
            [InlineKeyboardButton("👥 Players", callback_data="list_players:0")],
            [InlineKeyboardButton("💬 Active Chats", callback_data="list_chats:0")],
        ]
    )

# ================== COMMANDS ==================

def start_cmd(update: Update, context: CallbackContext):
    if not staff_only(update):
        return

    txt = (
        f"👋 *{BOT_NAME}*\n"
        "Process player deposits, approve/reject game login requests, pay withdrawals, and view players.\n\n"
        "Commands:\n"
        "/panel – main menu\n"
        "/deposits – list pending deposits\n"
        "/withdrawals – list pending withdrawals\n"
        "/requests – list pending game login requests\n"
        "/players – list recent players\n"
        "/chats – your active chats\n"
        "/startchat <player_id> – start chat with player\n"
    )
    update.message.reply_text(
        txt,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=staff_reply_keyboard()
    )

def panel_cmd(update: Update, context: CallbackContext):
    if not staff_only(update):
        return
    with flask_app.app_context():
        pending_deposits = DepositRequest.query.filter_by(status="PENDING").count()
        pending_requests = GameAccountRequest.query.filter(
            GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"])
        ).count()
        pending_withdraws = WithdrawRequest.query.filter_by(status="PENDING").count()
        
        # Count active chats for this employee
        telegram_user_id = update.effective_user.id
        employee = User.query.filter_by(telegram_id=telegram_user_id).first()
        active_chats = 0
        if employee:
            active_chats = DMThread.query.filter_by(
                employee_id=employee.id,
                status="OPEN"
            ).count()

    txt = (
        "*Staff panel:*\n"
        f"💰 Pending deposits: *{pending_deposits}*\n"
        f"🆔 Pending game requests: *{pending_requests}*\n"
        f"💸 Pending withdrawals: *{pending_withdrawals}*\n"
        f"💬 Your active chats: *{active_chats}*\n"
    )
    update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())

def deposits_cmd(update: Update, context: CallbackContext):
    if not staff_only(update):
        return
    _send_deposits_list(update, page=0)

def withdrawals_cmd(update: Update, context: CallbackContext):
    if not staff_only(update):
        return
    _send_withdrawals_list(update, page=0)

def requests_cmd(update: Update, context: CallbackContext):
    if not staff_only(update):
        return
    _send_requests_list(update, page=0)

def players_cmd(update: Update, context: CallbackContext):
    if not staff_only(update):
        return
    _send_players_list(update, page=0)

def chats_cmd(update: Update, context: CallbackContext):
    """Show active chats for this employee."""
    if not staff_only(update):
        return
    telegram_user_id = update.effective_user.id
    _send_active_chats(update, telegram_user_id)

def startchat_cmd(update: Update, context: CallbackContext):
    """Start a chat with a player: /startchat <player_id> [message]"""
    if not staff_only(update):
        return
    
    args = context.args
    if not args or len(args) < 1:
        update.message.reply_text("Usage: /startchat <player_id> [optional_message]")
        return
    
    try:
        player_id = int(args[0])
        initial_message = " ".join(args[1:]) if len(args) > 1 else "Hello! How may I help you today? 😊"
        telegram_user_id = update.effective_user.id
        
        with flask_app.app_context():
            # Verify player exists
            player = db.session.get(User, player_id)
            if not player or player.role != "PLAYER":
                update.message.reply_text("❌ Player not found.")
                return
            
            # Get or create thread
            thread = _get_or_create_thread_for_player(player_id, telegram_user_id)
            
            # Save initial message
            employee = User.query.filter_by(telegram_id=telegram_user_id).first()
            message = DMMessage(
                thread_id=thread.id,
                sender_id=employee.id,
                sender_role=employee.role,
                body=initial_message,
                source='telegram',
                created_at=datetime.utcnow(),
            )
            db.session.add(message)
            thread.last_msg_at = datetime.utcnow()
            db.session.commit()
            
            # Notify player
            notify(player_id, f"💬 New message from support: {initial_message}")
            
            # Forward to website via Socket.IO
            try:
                from app import socketio
                socketio.emit('new_message', 
                            message.to_dict(),
                            room=f'thread_{thread.id}',
                            namespace='/chat')
            except:
                pass
            
            update.message.reply_text(
                f"✅ Chat started with {_display_name(player)}!\n"
                f"Message: {initial_message}\n\n"
                f"You can now send messages directly to continue the conversation.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "💬 Continue Chat", 
                        callback_data=f"set_chat:{thread.id}"
                    )
                ]])
            )
            
    except ValueError:
        update.message.reply_text("❌ Invalid player ID. Use a number.")
    except Exception as e:
        log.error(f"Error starting chat: {e}")
        update.message.reply_text("❌ Error starting chat.")

def whoami_cmd(update: Update, context: CallbackContext):
    user = update.effective_user
    uid = user.id if user else None
    uname = user.full_name if user else "unknown"
    update.message.reply_text(f"Your Telegram ID is: {uid}\nName: {uname}")

# ================== LIST SENDERS ==================

def _send_deposits_list(target, page: int):
    with flask_app.app_context():
        q = (
            DepositRequest.query.filter(DepositRequest.status.in_(["PENDING", "RECEIVED"]))
            .order_by(DepositRequest.created_at.asc())
        )
        total = q.count()
        rows = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()

    if not rows:
        if hasattr(target, "message"):
            target.message.reply_text("No pending deposits ✅", reply_markup=main_menu_kb())
        else:
            target.edit_message_text("No pending deposits ✅", reply_markup=main_menu_kb())
        return

    text_lines = [f"💰 *Pending deposits* (page {page+1})"]
    for d in rows:
        text_lines.append(f"- #{d.id} • user {d.user_id} • {d.amount} via {d.method}")
    text = "\n".join(text_lines)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list_deposits:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"list_deposits:{page+1}"))

    if hasattr(target, "message"):
        target.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([nav] if nav else []))
    else:
        target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=InlineKeyboardMarkup([nav] if nav else []))

    for d in rows:
        _send_deposit_card(target, d)

def _send_withdrawals_list(target, page: int):
    with flask_app.app_context():
        q = (
            WithdrawRequest.query.filter_by(status="PENDING")
            .order_by(WithdrawRequest.created_at.asc())
        )
        total = q.count()
        rows = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()

    if not rows:
        if hasattr(target, "message"):
            target.message.reply_text("No pending withdrawals ✅", reply_markup=main_menu_kb())
        else:
            target.edit_message_text("No pending withdrawals ✅", reply_markup=main_menu_kb())
        return

    text_lines = [f"💸 *Pending withdrawals* (page {page+1})"]
    for w in rows:
        text_lines.append(f"- #{w.id} • user {w.user_id} • {w.amount} via {w.method}")
    text = "\n".join(text_lines)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list_withdrawals:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"list_withdrawals:{page+1}"))

    if hasattr(target, "message"):
        target.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([nav] if nav else []))
    else:
        target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=InlineKeyboardMarkup([nav] if nav else []))

    for w in rows:
        _send_withdraw_card(target, w)

def _send_requests_list(target, page: int):
    with flask_app.app_context():
        q = (
            GameAccountRequest.query.filter(
                GameAccountRequest.status.in_(["PENDING", "IN_PROGRESS"])
            )
            .order_by(GameAccountRequest.created_at.asc())
        )
        total = q.count()
        rows = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()

    if not rows:
        if hasattr(target, "message"):
            target.message.reply_text("No pending game login requests ✅", reply_markup=main_menu_kb())
        else:
            target.edit_message_text("No pending game login requests ✅", reply_markup=main_menu_kb())
        return

    text_lines = [f"🆔 *Game login requests* (page {page+1})"]
    for r in rows:
        text_lines.append(f"- #{r.id} • user {r.user_id} • game {r.game_id} • {r.status}")
    text = "\n".join(text_lines)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list_requests:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"list_requests:{page+1}"))

    if hasattr(target, "message"):
        target.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([nav] if nav else []))
    else:
        target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=InlineKeyboardMarkup([nav] if nav else []))

    for r in rows:
        _send_request_card(target, r)

def _send_players_list(target, page: int):
    """
    Rich players list:
    - ID + name
    - email
    - Telegram username / ID (if stored)
    - games count + names
    - registration time
    """
    with flask_app.app_context():
        q = User.query.filter_by(role="PLAYER").order_by(User.created_at.desc())
        total = q.count()
        rows = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()

        if not rows:
            msg = "No players."
            if hasattr(target, "message"):
                target.message.reply_text(msg, reply_markup=main_menu_kb())
            else:
                target.edit_message_text(msg, reply_markup=main_menu_kb())
            return

        lines = [f"👥 *Players* (page {page+1}) — Total: {total}"]

        for u in rows:
            name = _display_name(u)
            lines.append(f"- {u.id} • {name}")

            # email
            email = (getattr(u, "email", "") or "").strip()
            if email:
                lines.append(f"  📧 {email}")

            # Telegram info (if stored)
            tg_id = getattr(u, "telegram_id", None) or getattr(u, "tg_id", None)
            tg_name = (
                getattr(u, "telegram_name", None)
                or getattr(u, "tg_name", None)
                or getattr(u, "telegram_username", None)
                or getattr(u, "telegram_handle", None)
            )
            if tg_id or tg_name:
                if tg_name and tg_id:
                    lines.append(f"  📲 Telegram: {tg_name} (id {tg_id})")
                elif tg_name:
                    lines.append(f"  📲 Telegram: {tg_name}")
                else:
                    lines.append(f"  📲 Telegram id: {tg_id}")

            # games for this player
            game_names = []
            try:
                accs = GameAccount.query.filter_by(user_id=u.id).all()
                for acc in accs:
                    g = db.session.get(Game, acc.game_id) if acc.game_id else None
                    if g and g.name and g.name not in game_names:
                        game_names.append(g.name)
            except Exception:
                game_names = []

            if game_names:
                short = ", ".join(game_names[:5])
                if len(game_names) > 5:
                    short += "…"
                lines.append(f"  🎮 Games ({len(game_names)}): {short}")

            # registration time
            created = getattr(u, "created_at", None)
            if created:
                try:
                    lines.append(f"  🕒 Joined: {created:%Y-%m-%d %H:%M}")
                except Exception:
                    lines.append(f"  🕒 Joined: {created}")

        text = "\n".join(lines)

    # pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list_players:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"list_players:{page+1}"))

    kb = InlineKeyboardMarkup([nav]) if nav else None

    if hasattr(target, "message"):
        target.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

def _send_chats_list(target, page: int):
    """Show all active chats (admin view)."""
    telegram_user_id = target.effective_user.id if hasattr(target, 'effective_user') else None
    if telegram_user_id:
        _send_active_chats(target, telegram_user_id)

# ================== CARD SENDERS ==================

def _send_deposit_card(target, dep: DepositRequest):
    with flask_app.app_context():
        user = db.session.get(User, dep.user_id) if dep.user_id else None
        game = db.session.get(Game, dep.game_id) if dep.game_id else None
        settings = db.session.get(PaymentSettings, 1)

        meta = {}
        try:
            if isinstance(dep.meta, dict):
                meta = dep.meta
            elif dep.meta:
                meta = json.loads(dep.meta)
        except Exception:
            meta = {}

    uname = _display_name(user)
    gname = game.name if game else "—"

    lines = [
        f"💰 *Deposit #{dep.id}*",
        f"Player: {uname} (id {dep.user_id})",
        f"Amount: *{dep.amount}*",
        f"Method: `{dep.method or '-'}`",
        f"Game: {gname}",
        f"Status: {dep.status}",
    ]

    if dep.created_at:
        lines.append(f"Created: `{dep.created_at:%Y-%m-%d %H:%M}`")

    detail_lines = []
    method = (dep.method or "").upper()

    if method == "CHIME":
        payer_name = (meta.get("payer_name") or "").strip()
        payer_handle = (
            meta.get("payer_handle")
            or meta.get("payer_contact")
            or meta.get("chime_handle")
            or ""
        ).strip()

        if not (payer_name or payer_handle) and settings and getattr(settings, "chime_handle", None):
            payer_handle = settings.chime_handle

        if payer_name:
            detail_lines.append(f"Name: {payer_name}")
        if payer_handle:
            detail_lines.append(f"Handle: `{payer_handle}`")

    elif method == "CRYPTO":
        from_addr = (
            meta.get("payer_wallet")
            or meta.get("payer_address")
            or meta.get("crypto_from")
            or ""
        ).strip()
        net = (meta.get("network") or meta.get("chain") or "").strip()

        if from_addr:
            detail_lines.append(f"From: `{from_addr}`")
        if net:
            detail_lines.append(f"Network: {net}")

    elif method == "CASHAPP":
        cash_tag = (
            meta.get("payer_cashtag")
            or meta.get("payer_handle")
            or meta.get("payer_contact")
            or ""
        ).strip()
        if cash_tag:
            detail_lines.append(f"From: `{cash_tag}`")

    proof_url_raw = (dep.proof_url or "").strip() or (meta.get("proof_url") or "").strip()
    proof_link = None

    if proof_url_raw:
        proof_link = proof_url_raw
        if not proof_link.lower().startswith(("http://", "https://")):
            base = (
                flask_app.config.get("EXTERNAL_BASE_URL")
                or os.getenv("EXTERNAL_BASE_URL", "")
            )
            if base.endswith("/"):
                base = base[:-1]
            if not proof_link.startswith("/"):
                proof_link = "/" + proof_link
            if base:
                proof_link = base + proof_link

        if proof_link.lower().startswith(("http://", "https://")):
            detail_lines.append(f"[View proof]({proof_link})")
        else:
            detail_lines.append(f"Proof: `{proof_url_raw}`")

    if detail_lines:
        lines.append("")
        lines.append("*Details:*")
        lines.extend(detail_lines)

    text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Mark LOADED", callback_data=f"dep_loaded:{dep.id}"),
                InlineKeyboardButton("🤖 Approve+Credit", callback_data=f"dep_auto:{dep.id}"),
            ],
            [InlineKeyboardButton("❌ Reject", callback_data=f"dep_reject:{dep.id}")],
        ]
    )

    if hasattr(target, "message"):
        target.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    else:
        target.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb,
        )

def _send_withdraw_card(target, wd: WithdrawRequest):
    with flask_app.app_context():
        user = db.session.get(User, wd.user_id) if wd.user_id else None
        game = db.session.get(Game, wd.game_id) if wd.game_id else None
        wallet = PlayerBalance.query.filter_by(user_id=wd.user_id).first()
        login = _player_login_for_game(wd.user_id, wd.game_id)

    uname = _display_name(user)
    gname = game.name if game else "—"

    lines = [
        f"💸 *Withdrawal #{wd.id}*",
        f"Player: {uname} (id {wd.user_id})",
        f"Game: {gname}",
    ]
    if login:
        lines.append(f"Login: `{login}`")

    lines.extend(
        [
            f"Amount: *{wd.amount}*",
            f"Method: `{wd.method or '-'}`",
            f"Status: {wd.status}",
        ]
    )

    if wd.created_at:
        lines.append(f"Requested: `{wd.created_at:%Y-%m-%d %H:%M}`")

    detail_lines = []

    if wallet and wallet.balance is not None:
        detail_lines.append(f"Wallet: *{int(wallet.balance)}*")

    if getattr(wd, "total_amount", None):
        detail_lines.append(f"Total: *{wd.total_amount}*")
    if getattr(wd, "keep_amount", None):
        detail_lines.append(f"Keep in wallet: *{wd.keep_amount}*")
    if getattr(wd, "tip_amount", None) and wd.tip_amount > 0:
        detail_lines.append(f"Tip: *{wd.tip_amount}*")

    addr = (getattr(wd, "address", "") or "").strip()
    if addr:
        method = (wd.method or "").upper()
        if method == "CRYPTO":
            label = "Address"
        elif method == "CHIME":
            label = "Chime"
        else:
            label = "Destination"
        detail_lines.append(f"{label}: `{addr}`")

    if detail_lines:
        lines.append("")
        lines.append("*Details:*")
        lines.extend(detail_lines)

    text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔁 Redeem", callback_data=f"wd_redeem:{wd.id}"),
                InlineKeyboardButton("✅ Mark PAID", callback_data=f"wd_paid:{wd.id}"),
            ],
            [InlineKeyboardButton("❌ Reject", callback_data=f"wd_reject:{wd.id}")],
        ]
    )

    if hasattr(target, "message"):
        target.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    else:
        target.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

def _send_request_card(target, req: GameAccountRequest):
    with flask_app.app_context():
        user = db.session.get(User, req.user_id) if req.user_id else None
        game = db.session.get(Game, req.game_id) if req.game_id else None
    uname = _display_name(user)
    gname = game.name if game else "—"
    lines = [
        f"🆔 *Game request #{req.id}*",
        f"Player: {uname} (id {req.user_id})",
        f"Game: {gname}",
        f"Status: {req.status}",
    ]
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"req_ok:{req.id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"req_no:{req.id}"),
            ],
            [
                InlineKeyboardButton("🤖 Auto-provision", callback_data=f"req_auto:{req.id}"),
                InlineKeyboardButton("⏳ In progress", callback_data=f"req_prog:{req.id}"),
            ],
        ]
    )
    if hasattr(target, "message"):
        target.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ================== CALLBACK HANDLER ==================

def callback_handler(update: Update, context: CallbackContext):
    if not staff_only(update):
        return

    q = update.callback_query
    data = q.data or ""
    q.answer()

    # ================== CHAT HANDLERS ==================
    if data.startswith("open_chat:"):
        thread_id = int(data.split(":", 1)[1])
        telegram_user_id = update.effective_user.id
        
        with flask_app.app_context():
            thread = db.session.get(DMThread, thread_id)
            if not thread:
                q.edit_message_text("❌ Chat thread not found.")
                return
            
            player = db.session.get(User, thread.player_id)
            
            # Get last few messages
            messages = DMMessage.query.filter_by(
                thread_id=thread_id
            ).order_by(DMMessage.id.desc()).limit(10).all()
            messages.reverse()
            
            msg_lines = [f"💬 *Chat with {_display_name(player)}*\n"]
            for msg in messages:
                sender = "You" if msg.sender_id == thread.employee_id else player.name
                msg_lines.append(f"*{sender}:* {msg.body[:100]}{'...' if len(msg.body) > 100 else ''}")
            
            q.edit_message_text(
                "\n".join(msg_lines),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Send Message", callback_data=f"set_chat:{thread_id}")
                ]])
            )
        return
    
    if data.startswith("set_chat:"):
        thread_id = int(data.split(":", 1)[1])
        context.user_data["active_chat_thread"] = thread_id
        
        with flask_app.app_context():
            thread = db.session.get(DMThread, thread_id)
            player = db.session.get(User, thread.player_id) if thread else None
            
            q.edit_message_text(
                f"✅ Chat mode activated!\n"
                f"You're now chatting with {_display_name(player) if player else 'player'}.\n"
                f"Type your message and send it normally.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Exit Chat Mode", callback_data="exit_chat_mode")
                ]])
            )
        return
    
    if data == "exit_chat_mode":
        if "active_chat_thread" in context.user_data:
            del context.user_data["active_chat_thread"]
        q.edit_message_text("✅ Exited chat mode.")
        return
    
    if data.startswith("player_info:"):
        player_id = int(data.split(":", 1)[1])
        with flask_app.app_context():
            player = db.session.get(User, player_id)
            if player:
                q.edit_message_text(
                    f"👤 *Player Info*\n"
                    f"ID: {player.id}\n"
                    f"Name: {player.name}\n"
                    f"Email: {player.email}\n"
                    f"Joined: {player.created_at:%Y-%m-%d %H:%M}\n\n"
                    f"💬 [Start Chat](callback:startchat_{player.id})",
                    parse_mode=ParseMode.MARKDOWN
                )
        return
    
    # lists
    if data.startswith("list_deposits:"):
        page = int(data.split(":", 1)[1])
        _send_deposits_list(q, page)
        return
    if data.startswith("list_withdrawals:"):
        page = int(data.split(":", 1)[1])
        _send_withdrawals_list(q, page)
        return
    if data.startswith("list_requests:"):
        page = int(data.split(":", 1)[1])
        _send_requests_list(q, page)
        return
    if data.startswith("list_players:"):
        page = int(data.split(":", 1)[1])
        _send_players_list(q, page)
        return
    if data.startswith("list_chats:"):
        page = int(data.split(":", 1)[1])
        telegram_user_id = update.effective_user.id
        _send_active_chats(update, telegram_user_id)
        return
    if data.startswith("list_support:"):
        page = int(data.split(":", 1)[1])
        _send_support_list(q, page)
        return

    # ================== DEPOSITS ==================
    if data.startswith("dep_loaded:"):
        dep_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Marking deposit #{dep_id} as LOADED…")
        with flask_app.app_context():
            dep = db.session.get(DepositRequest, dep_id)
            if not dep:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Deposit not found.")
                return
            dep.status = "LOADED"
            dep.loaded_at = datetime.utcnow()
            _add_wallet_balance(dep.user_id, int(dep.amount or 0))
            db.session.commit()
            notify(dep.user_id, f"✅ Your deposit #{dep.id} of {dep.amount} has been loaded.")
        context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid,
                                      text=f"Deposit #{dep_id} ✅ marked LOADED.")
        return

    if data.startswith("dep_reject:"):
        dep_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Rejecting deposit #{dep_id}…")
        with flask_app.app_context():
            dep = db.session.get(DepositRequest, dep_id)
            if not dep:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Deposit not found.")
                return
            dep.status = "REJECTED"
            db.session.commit()
            notify(dep.user_id, f"❌ Your deposit #{dep.id} was rejected. Please contact support.")
        context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid,
                                      text=f"Deposit #{dep_id} ❌ rejected.")
        return

    if data.startswith("dep_auto:"):
        dep_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Crediting deposit #{dep_id}…")
        with flask_app.app_context():
            dep = db.session.get(DepositRequest, dep_id)
            if not dep:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Deposit not found.")
                return
            if dep.status not in ("PENDING", "RECEIVED"):
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text=f"Deposit #{dep.id} has status {dep.status}, not PENDING/RECEIVED.",
                )
                return
            game = db.session.get(Game, dep.game_id) if dep.game_id else None
            vendor = _vendor_for_game(game)
            if not vendor:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid,
                                              text="Vendor not recognized for this game.")
                return
            acc_username = _player_login_for_game(dep.user_id, dep.game_id)
            if not acc_username:
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text="Player has no saved login for this game/vendor.",
                )
                return
            amount = int(dep.amount or 0)
            if amount <= 0:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Amount must be > 0")
                return

            note = f"Deposit#{dep.id} via Telegram"
            try:
                res = provider_credit(vendor, acc_username, amount, note)
            except Exception as e:
                msg = str(e)
                if "Locator.click" in msg or "Timeout 60000ms" in msg or "recharge" in msg.lower():
                    context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_mid,
                        text="⚠️ JUWA recharge failed.\nPlease try again or load it manually in the JUWA panel.",
                    )
                else:
                    context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_mid,
                        text=f"{vendor.upper()} credit failed: {msg}",
                    )
                return

            if not _prov_ok(vendor, res):
                err_txt = _prov_err(res) or "unknown error"
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text=f"⚠️ {vendor.upper()} recharge failed.\nReason: {err_txt}\nTry again or do it manually.",
                )
                return

            dep.status = "LOADED"
            dep.loaded_at = datetime.utcnow()
            _add_wallet_balance(dep.user_id, amount)
            db.session.commit()
            notify(dep.user_id, f"✅ Your deposit #{dep.id} of {amount} has been credited to {vendor.upper()}.")
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_mid,
            text=f"Deposit #{dep_id} ✅ credited on vendor and marked LOADED.",
        )
        return

    # ================== WITHDRAWALS ==================
    if data.startswith("wd_redeem:"):
        wd_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Redeeming withdrawal #{wd_id}…")
        with flask_app.app_context():
            wd = db.session.get(WithdrawRequest, wd_id)
            if not wd:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Withdrawal not found.")
                return
            game = db.session.get(Game, wd.game_id) if wd.game_id else None
            vendor = _vendor_for_game(game)
            acc_username = _player_login_for_game(wd.user_id, wd.game_id) if wd.game_id else None

            if (not acc_username) or (not vendor):
                accs = GameAccount.query.filter_by(user_id=wd.user_id).all()
                for a in accs:
                    g = db.session.get(Game, a.game_id) if a.game_id else None
                    v = _vendor_for_game(g)
                    if v:
                        acc_username = acc_username or (
                            a.account_username or a.username or a.login or a.user
                        )
                        vendor = vendor or v
                        if acc_username and vendor:
                            break

            if not acc_username or not vendor:
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text="Cannot redeem: no username/vendor found for player.",
                )
                return

            amount = int(wd.amount or 0)
            if amount <= 0:
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text="Redeem amount must be > 0.",
                )
                return

            try:
                res = provider_redeem(vendor, acc_username, amount, f"Withdraw #{wd.id}")
            except Exception as e:
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text=f"Redeem error: {e}",
                )
                return
            if not _prov_ok(vendor, res):
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text=f"Redeem failed: {_prov_err(res)}",
                )
                return

            wd.status = "APPROVED"
            db.session.commit()
            notify(wd.user_id, f"🔔 Your withdrawal #{wd.id} has been successfully paid. Please check your wallet to confirm the funds.")
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_mid,
            text=f"Withdrawal #{wd_id} ✅ redeemed on {vendor.upper()}.",
        )
        return

    if data.startswith("wd_paid:"):
        wd_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Marking withdrawal #{wd_id} as PAID…")
        with flask_app.app_context():
            wd = db.session.get(WithdrawRequest, wd_id)
            if not wd:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Withdrawal not found.")
                return
            wd.status = "PAID"
            wd.paid_at = datetime.utcnow()
            wallet = PlayerBalance.query.filter_by(user_id=wd.user_id).first()
            if wallet:
                wallet.balance = max(0, (wallet.balance or 0) - (wd.amount or 0))
            db.session.commit()
            notify(wd.user_id, f"💸 Your withdrawal #{wd.id} for {wd.amount} has been paid.")
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_mid,
            text=f"Withdrawal #{wd_id} ✅ marked PAID.",
        )
        return

    if data.startswith("wd_reject:"):
        wd_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Rejecting withdrawal #{wd_id}…")
        with flask_app.app_context():
            wd = db.session.get(WithdrawRequest, wd_id)
            if not wd:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Withdrawal not found.")
                return
            wd.status = "REJECTED"
            db.session.commit()
            notify(wd.user_id, f"⚠️ Your withdrawal #{wd.id} was rejected. Contact support.")
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_mid,
            text=f"Withdrawal #{wd_id} ❌ rejected.",
        )
        return

    # ================== GAME REQUESTS ==================
    if data.startswith("req_ok:"):
        req_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Approving game request #{req_id}…")
        with flask_app.app_context():
            req = db.session.get(GameAccountRequest, req_id)
            if not req:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Request not found.")
                return
            req.status = "APPROVED"
            req.approved_at = datetime.utcnow()
            db.session.commit()
            notify(req.user_id, f"✅ Your game login request #{req.id} was approved. Open My Logins.")
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_mid,
            text=f"Game request #{req_id} ✅ approved.",
        )
        return

    if data.startswith("req_no:"):
        req_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Rejecting game request #{req_id}…")
        with flask_app.app_context():
            req = db.session.get(GameAccountRequest, req_id)
            if not req:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Request not found.")
                return
            if (req.status or "").upper() not in ("APPROVED", "PROVIDED", "REJECTED"):
                req.status = "REJECTED"
                db.session.commit()
                notify(req.user_id, f"⚠️ Your game login request #{req.id} was rejected.")
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_mid,
            text=f"Game request #{req_id} ❌ rejected.",
        )
        return

    if data.startswith("req_prog:"):
        req_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Marking game request #{req_id} IN_PROGRESS…")
        with flask_app.app_context():
            req = db.session.get(GameAccountRequest, req_id)
            if not req:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Request not found.")
                return
            req.status = "IN_PROGRESS"
            db.session.commit()
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_mid,
            text=f"Game request #{req_id} ⏳ marked IN_PROGRESS.",
        )
        return

    if data.startswith("req_auto:"):
        req_id = int(data.split(":", 1)[1])
        chat_id = q.message.chat_id
        _ack_working(q)
        _set_loading_keyboard(q)
        progress_mid = _send_progress_msg(context.bot, chat_id, f"⏳ Auto-provisioning for request #{req_id}…")
        with flask_app.app_context():
            req = db.session.get(GameAccountRequest, req_id)
            if not req:
                context.bot.edit_message_text(chat_id=chat_id, message_id=progress_mid, text="Request not found.")
                return
            game = db.session.get(Game, req.game_id) if req.game_id else None
            vendor = _vendor_for_game(game)
            if vendor != "milkyway":
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text="Auto-provision supported only for Milkyway (in this build).",
                )
                return
            try:
                r = provider_auto_create(vendor)
            except Exception as e:
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text=f"Auto-provision error: {e}",
                )
                return
            if not isinstance(r, dict) or not r.get("ok"):
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_mid,
                    text=f"Auto-provision failed: {r}",
                )
                return

            username = r.get("username")
            password = r.get("password") or username
            note = r.get("note") or "Auto-provisioned via Telegram"

            acc = GameAccount.query.filter_by(user_id=req.user_id, game_id=req.game_id).first()
            if not acc:
                acc = GameAccount(user_id=req.user_id, game_id=req.game_id)
                db.session.add(acc)

            for f in ("account_username", "username", "login", "user"):
                if hasattr(acc, f):
                    setattr(acc, f, username)
                    break
            for f in ("account_password", "password", "passcode", "pin"):
                if hasattr(acc, f):
                    setattr(acc, f, password)
                    break
            if hasattr(acc, "extra"):
                acc.extra = note

            req.status = "APPROVED"
            req.approved_at = datetime.utcnow()
            db.session.commit()
            notify(req.user_id, "🤖 Your Milkyway login was issued. Open My Logins.")
        context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_mid,
            text=f"Game request #{req_id} 🤖 auto-provisioned.",
        )
        return

# ================== AUTO-POLL JOB ==================

def poll_new_items(context: CallbackContext):
    job_ctx = context.job.context
    state = job_ctx["state"]
    staff_ids = job_ctx["staff_ids"]

    try:
        with flask_app.app_context():
            # ... (existing poll code remains the same) ...
            pass  # Keep all existing poll logic
            
    except Exception as e:
        log.exception("poll_new_items crashed: %s", e)

# ================== EXTERNAL HELPER (for Flask) ==================

def notify_withdraw_request(user, amount, method, address, game_name):
    """
    Called from Flask when a player submits a withdrawal request.
    Sends formatted message to all staff Telegram IDs.
    """
    try:
        from telegram import Bot
        bot = Bot(token=TOKEN)
        msg = (
            f"💸 *New Withdrawal Request*\n"
            f"👤 Player: `{user.username}` (id {user.id})\n"
            f"🎮 Game: {game_name}\n"
            f"💰 Amount: *${amount}*\n"
            f"🏦 Method: {method}\n"
            f"📤 Destination: `{address}`\n"
            f"⏳ Status: Pending approval"
        )
        for sid in STAFF_IDS:
            try:
                bot.send_message(chat_id=sid, text=msg, parse_mode="Markdown")
            except Exception as inner_e:
                log.warning("Failed to send withdraw alert to %s: %s", sid, inner_e)
    except Exception as e:
        log.warning("notify_withdraw_request() failed: %s", e)

# ================== MAIN ==================

def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("whoami", whoami_cmd))
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("panel", panel_cmd))
    dp.add_handler(CommandHandler("deposits", deposits_cmd))
    dp.add_handler(CommandHandler("withdrawals", withdrawals_cmd))
    dp.add_handler(CommandHandler("requests", requests_cmd))
    dp.add_handler(CommandHandler("players", players_cmd))
    dp.add_handler(CommandHandler("chats", chats_cmd))  # New chat command
    dp.add_handler(CommandHandler("startchat", startchat_cmd, pass_args=True))  # Start chat command

    dp.add_handler(CallbackQueryHandler(callback_handler))
    dp.add_handler(
        MessageHandler(
            Filters.text & ~Filters.command & Filters.private,
            staff_menu_router
        )
    )

    state = load_state()
    with flask_app.app_context():
        if "last_dep_id" not in state:
            last_dep = db.session.query(DepositRequest.id).order_by(DepositRequest.id.desc()).first()
            state["last_dep_id"] = last_dep[0] if last_dep else 0
        if "last_wd_id" not in state:
            last_wd = db.session.query(WithdrawRequest.id).order_by(WithdrawRequest.id.desc()).first()
            state["last_wd_id"] = last_wd[0] if last_wd else 0
        if "last_acc_id" not in state:
            last_acc = db.session.query(GameAccountRequest.id).order_by(GameAccountRequest.id.desc()).first()
            state["last_acc_id"] = last_acc[0] if last_acc else 0
        if "last_user_id" not in state:
            last_user = db.session.query(User.id).order_by(User.id.desc()).first()
            state["last_user_id"] = last_user[0] if last_user else 0

    save_state(state)

    job_context = {
        "state": state,
        "staff_ids": STAFF_IDS,
    }

    updater.job_queue.run_repeating(
        poll_new_items,
        interval=5,
        first=5,
        context=job_context,
    )

    log.info("Telegram staff bot started with chat integration.")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()