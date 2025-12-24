# queued_requests.py

from flask import Blueprint, jsonify
from models import db, GameAccountRequest
import os
import redis

# Local Redis client just for progress lookups (no circular import)
REDIS_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(REDIS_URL)


def _progress_key(req_id: int) -> str:
    return f"req_progress:{req_id}"

queue_bp = Blueprint("queue_bp", __name__)

@queue_bp.get("/player/request/<int:req_id>/status.json")
def player_request_status_json(req_id):
    req = db.session.get(GameAccountRequest, req_id)
    if not req:
        return jsonify({"ok": False, "error": "Request not found"}), 404

    # status from DB
    status = getattr(req, "status", "UNKNOWN")

    # progress text from Redis
    raw = redis_client.get(_progress_key(req_id))
    progress = raw.decode("utf-8") if raw else "Your ID request is in the queue…"

    return jsonify({
        "ok": True,
        "status": status,
        "progress": progress,
    })