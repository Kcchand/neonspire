# chat_bp.py - UPDATED WITH TELEGRAM INTEGRATION
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, abort, current_app
from flask_login import current_user, login_required
from sqlalchemy import or_, desc

from models import db, User, DMThread, DMMessage, notify  # <-- add notify

chat_bp = Blueprint("chat_bp", __name__, url_prefix="/chat")


# ---------------- helpers ----------------

def _is_staff() -> bool:
    return current_user.is_authenticated and current_user.role in ("EMPLOYEE", "ADMIN")

def _visible_to_current_user(t: DMThread) -> bool:
    """Permission check for a given DM thread."""
    if not current_user.is_authenticated:
        return False
    if current_user.role == "PLAYER":
        return t.player_id == current_user.id
    return _is_staff()

def _display_name(u: User | None) -> str:
    """Pretty name for notifications."""
    if not u:
        return "User"
    return (u.name or u.email or f"User #{u.id}").strip()

def _get_or_create_player_thread(player_id: int, claim_emp_id: int | None = None) -> DMThread:
    """Ensure exactly one OPEN thread per player. Optionally claim it for an employee."""
    t = (DMThread.query
         .filter_by(player_id=player_id, status="OPEN")
         .order_by(desc(DMThread.id))
         .first())
    if not t:
        t = DMThread(
            player_id=player_id,
            employee_id=claim_emp_id,
            status="OPEN",
            last_msg_at=datetime.utcnow(),
        )
        db.session.add(t)
        db.session.commit()
    elif claim_emp_id and not t.employee_id:
        t.employee_id = claim_emp_id
        db.session.commit()
    return t

def _notify_chat_message(thread: DMThread, sender: User, body: str) -> None:
    """
    Create a Notification for the opposite side of the DM.
    - PLAYER -> notify assigned EMPLOYEE (if any)
    - EMPLOYEE/ADMIN -> notify PLAYER
    """
    preview = (body or "").strip().replace("\n", " ")
    if len(preview) > 90:
        preview = preview[:90] + "…"
    sender_name = _display_name(sender)
    message = f"💬 New chat message from {sender_name}: 「{preview}」"

    if sender.role == "PLAYER":
        if thread.employee_id:          # don't spam all staff if unclaimed
            notify(thread.employee_id, message)
    else:
        notify(thread.player_id, message)

def _forward_to_telegram(thread: DMThread, message_text: str, sender_name: str) -> None:
    """
    Forward website messages to Telegram.
    This function should be implemented in telegram_bot.py
    """
    try:
        # Import here to avoid circular imports
        from telegram_bot import forward_to_telegram
        forward_to_telegram(thread.id, message_text, sender_name, thread.employee_id)
    except ImportError:
        print("⚠️  Telegram bot module not available")
    except Exception as e:
        print(f"⚠️  Failed to forward to Telegram: {e}")


# ---------------- entry ----------------

@chat_bp.get("/")
@login_required
def index():
    if current_user.role == "PLAYER":
        return redirect(url_for("chat_bp.my_chat"))
    if _is_staff():
        return redirect(url_for("chat_bp.inbox"))
    abort(403)


# ---------------- player UI ----------------

@chat_bp.get("/me")
@login_required
def my_chat():
    """Player’s private support chat page."""
    if current_user.role != "PLAYER":
        return redirect(url_for("chat_bp.inbox"))
    t = _get_or_create_player_thread(current_user.id, None)
    return render_template("player_chat.html", thread=t)

@chat_bp.get("/my-thread-id")
@login_required
def my_thread_id():
    """Return the player’s thread id (used by floating _dm_widget.html)."""
    if current_user.role != "PLAYER":
        return jsonify({"ok": False, "error": "Players only"}), 403
    t = _get_or_create_player_thread(current_user.id, None)
    return jsonify({"ok": True, "thread_id": t.id}), 200


# ---------------- staff UI ----------------

@chat_bp.get("/inbox")
@login_required
def inbox():
    """Staff inbox: CLEAR list showing player name/email + last message preview."""
    if not _is_staff():
        abort(403)

    term = (request.args.get("q") or "").strip()
    q = DMThread.query.filter_by(status="OPEN")

    if term:
        # search player by name/email/id
        matches = (User.query
                   .filter(or_(
                       User.name.ilike(f"%{term}%"),
                       User.email.ilike(f"%{term}%"),
                       User.id.cast(db.String).ilike(f"%{term}%"),
                   ))
                   .with_entities(User.id)
                   .all())
        ids = [uid for (uid,) in matches]
        if ids:
            q = q.filter(DMThread.player_id.in_(ids))
        else:
            # nothing matches
            q = q.filter(DMThread.id == 0)

    threads = (q.order_by(desc(DMThread.last_msg_at), desc(DMThread.id))
                 .limit(300)
                 .all())

    # Build a simple view-model for the template (no heavy logic there)
    threads_view = []
    for t in threads:
        player = db.session.get(User, t.player_id)
        last = (DMMessage.query
                .filter_by(thread_id=t.id)
                .order_by(DMMessage.id.desc())
                .first())
        threads_view.append({
            "thread_id": t.id,
            "status": t.status,
            "player_id": t.player_id,
            "player_name": (player.name if player and player.name else f"Player #{t.player_id}"),
            "player_email": (player.email if player else ""),
            "assigned_staff_id": t.employee_id,
            "last_msg_at": (t.last_msg_at or t.created_at),
            "last_msg_snippet": (last.body[:80] + ("…" if last and len(last.body) > 80 else "")) if last else "(no messages yet)",
        })

    return render_template("employee_chat.html", threads_view=threads_view, q=term)


@chat_bp.post("/open")
@login_required
def open_with_player():
    """Staff: open/claim chat with a specific player id then jump to room."""
    if not _is_staff():
        abort(403)
    player_id = request.form.get("player_id", type=int)
    if not player_id:
        abort(400)
    t = _get_or_create_player_thread(player_id, current_user.id)
    return redirect(url_for("chat_bp.room", thread_id=t.id))


@chat_bp.get("/room/<int:thread_id>")
@login_required
def room(thread_id: int):
    """Room page for staff or the owning player."""
    t = db.session.get(DMThread, thread_id)
    if not t or not _visible_to_current_user(t):
        abort(403)
    player = db.session.get(User, t.player_id)
    employee = db.session.get(User, t.employee_id) if t.employee_id else None
    return render_template("dm_room.html", thread=t, player=player, employee=employee)


@chat_bp.post("/thread/<int:thread_id>/claim")
@login_required
def claim_thread(thread_id: int):
    """Staff can claim an unassigned OPEN thread."""
    if not _is_staff():
        abort(403)
    t = db.session.get(DMThread, thread_id)
    if not t or t.status != "OPEN":
        abort(404)
    if not t.employee_id:
        t.employee_id = current_user.id
        db.session.commit()
    return redirect(url_for("chat_bp.room", thread_id=t.id))


@chat_bp.post("/thread/<int:thread_id>/close")
@login_required
def close_thread(thread_id: int):
    """Staff can close a thread."""
    if not _is_staff():
        abort(403)
    t = db.session.get(DMThread, thread_id)
    if not t:
        abort(404)
    t.status = "CLOSED"
    db.session.commit()
    return redirect(url_for("chat_bp.inbox"))


# ---------------- staff can initiate chat with players ----------------

@chat_bp.post("/start-with-player")
@login_required
def start_with_player():
    """Staff: proactively start a chat with a player."""
    if not _is_staff():
        abort(403)
    
    player_id = request.form.get("player_id", type=int)
    if not player_id:
        abort(400)
    
    # Get or create thread
    t = _get_or_create_player_thread(player_id, current_user.id)
    
    # Create initial greeting message
    initial_message = "Hello! How may I help you today? 😊"
    
    m = DMMessage(
        thread_id=t.id,
        sender_id=current_user.id,
        sender_role=current_user.role,
        body=initial_message,
        source='website',  # From website
        created_at=datetime.utcnow(),
    )
    db.session.add(m)
    t.last_msg_at = datetime.utcnow()
    db.session.commit()
    
    # Notify player
    _notify_chat_message(t, current_user, initial_message)
    
    # Forward to Telegram (if staff wants to continue there)
    try:
        from telegram_bot import forward_to_telegram
        forward_to_telegram(t.id, initial_message, current_user.name, t.employee_id)
    except:
        pass  # Telegram not available
    
    return redirect(url_for("chat_bp.room", thread_id=t.id))


# ---------------- JSON: poll & send ----------------

@chat_bp.get("/thread/<int:thread_id>/messages")
@login_required
def fetch_messages(thread_id: int):
    """Incremental polling. after_id=0 returns latest 100, otherwise messages > after_id."""
    t = db.session.get(DMThread, thread_id)
    if not t or not _visible_to_current_user(t):
        return jsonify({"ok": False, "error": "Not allowed"}), 403

    after_id = request.args.get("after_id", type=int) or 0
    q = DMMessage.query.filter_by(thread_id=thread_id)

    if after_id > 0:
        q = q.filter(DMMessage.id > after_id).order_by(DMMessage.id.asc()).limit(200)
        items = [m.to_dict() for m in q.all()]
    else:
        q = q.order_by(DMMessage.id.desc()).limit(100)
        items = list(reversed([m.to_dict() for m in q.all()]))

    resp = jsonify({"ok": True, "items": items})
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200


@chat_bp.post("/thread/<int:thread_id>/send")
@login_required
def send_message(thread_id: int):
    """Send a message into the thread."""
    t = db.session.get(DMThread, thread_id)
    if not t or not _visible_to_current_user(t):
        return jsonify({"ok": False, "error": "Not allowed"}), 403
    if t.status != "OPEN":
        return jsonify({"ok": False, "error": "Thread is closed"}), 400

    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    if len(body) > 2000:
        body = body[:2000]

    # Auto-claim if staff sends into an unclaimed thread
    if _is_staff() and not t.employee_id:
        t.employee_id = current_user.id

    # Create message with source tracking
    m = DMMessage(
        thread_id=t.id,
        sender_id=current_user.id,
        sender_role=current_user.role,
        body=body,
        source='website',  # <-- ADDED: Track that this came from website
        created_at=datetime.utcnow(),
    )
    db.session.add(m)
    t.last_msg_at = datetime.utcnow()
    db.session.commit()

    # ---- NEW: notify the other side (never breaks chat if it fails) ----
    try:
        _notify_chat_message(t, current_user, body)
    except Exception:
        db.session.rollback()
    
    # ---- NEW: Forward to Telegram if player sent message ----
    if current_user.role == "PLAYER":
        try:
            _forward_to_telegram(t, body, current_user.name)
        except Exception as e:
            print(f"⚠️  Failed to forward to Telegram: {e}")

    return jsonify({"ok": True, "item": m.to_dict()}), 200


# ---------------- broadcast to all players (staff only) ----------------

@chat_bp.post("/broadcast")
@login_required
def broadcast_to_all():
    """Staff: send a message to all players with open threads."""
    if not _is_staff():
        abort(403)
    
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    
    # Get all open threads
    open_threads = DMThread.query.filter_by(status="OPEN").all()
    sender_name = _display_name(current_user)
    
    success_count = 0
    for thread in open_threads:
        try:
            # Create broadcast message
            m = DMMessage(
                thread_id=thread.id,
                sender_id=current_user.id,
                sender_role=current_user.role,
                body=f"📢 Broadcast: {message}",
                source='website',
                created_at=datetime.utcnow(),
            )
            db.session.add(m)
            thread.last_msg_at = datetime.utcnow()
            
            # Notify player
            notify(thread.player_id, f"📢 Broadcast from {sender_name}: {message}")
            
            success_count += 1
        except:
            continue
    
    db.session.commit()
    
    return jsonify({
        "ok": True, 
        "message": f"Broadcast sent to {success_count} players",
        "count": success_count
    }), 200