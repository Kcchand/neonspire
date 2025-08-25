# notifications.py
from flask import Blueprint, render_template, redirect, url_for, abort
from flask_login import login_required, current_user
from models import db, Notification

notify_bp = Blueprint("notifybp", __name__, url_prefix="/notifications")

@notify_bp.before_request
def _guard():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login_get"))

@notify_bp.get("/")
@login_required
def list_notifications():
    unread = (Notification.query
              .filter_by(user_id=current_user.id, is_read=False)
              .order_by(Notification.created_at.desc())
              .all())
    history = (Notification.query
               .filter_by(user_id=current_user.id, is_read=True)
               .order_by(Notification.created_at.desc())
               .limit(50)
               .all())
    return render_template("notifications.html",
                           page_title="Notifications",
                           unread=unread, history=history)

@notify_bp.post("/read/<int:note_id>")
@login_required
def mark_read(note_id: int):
    n = db.session.get(Notification, note_id)
    if not n or n.user_id != current_user.id:
        return abort(404)
    n.is_read = True
    db.session.commit()
    return redirect(url_for("notifybp.list_notifications"))

@notify_bp.post("/read-all")
@login_required
def mark_all_read():
    (Notification.query
     .filter_by(user_id=current_user.id, is_read=False)
     .update({"is_read": True}))
    db.session.commit()
    return redirect(url_for("notifybp.list_notifications"))