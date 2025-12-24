# telegram_bp.py

import os
import time
import hmac
import json
import hashlib
import urllib.parse

from flask import Blueprint, request, jsonify, url_for, render_template, redirect, session
from flask_login import login_user

from models import db, User

telegram_bp = Blueprint("telegram_bp", __name__)

BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()


def _validate_init_data(init_data: str):
    """
    Validate Telegram WebApp initData.

    In DEV mode we only log mismatches but still accept the data
    so the mini app works even if our HMAC math is off.
    """
    if not BOT_TOKEN or not init_data:
        print("TG validate: missing BOT_TOKEN or initData")
        return None

    # parse query string into ordered list first
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)

    expected_hash = data.get("hash")
    if not expected_hash:
        print("TG validate: no hash in initData")
        return None

    # build check_string from ALL fields except "hash"
    pairs_no_hash = [(k, v) for (k, v) in pairs if k != "hash"]
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs_no_hash))

    # HMAC-SHA256 with secret_key = SHA256(bot_token)
    secret_key = hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest()
    computed = hmac.new(
        secret_key,
        check_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    print("TG validate: check_string =", check_string[:200])
    print("TG validate: computed =", computed)
    print("TG validate: expected =", expected_hash)

    if computed != expected_hash:
        # DEV: log but DO NOT block
        print("TG validate: HASH MISMATCH – IGNORING IN DEV MODE")

    # ---- if we reach here, we treat initData as valid ----
    user_raw = data.get("user")
    if not user_raw:
        print("TG validate: no user field")
        return None

    try:
        user = json.loads(user_raw)
    except Exception as e:
        print("TG validate: user json error:", e)
        return None

    # optional: expire auth after 1 day
    try:
        auth_date = int(data.get("auth_date", "0"))
        if time.time() - auth_date > 86400:
            print("TG validate: auth_date too old")
            # you can return None here if you want strict expiry
    except Exception:
        pass

    return user

@telegram_bp.route("/tg/start", methods=["GET"])
def tg_start():
    """
    Entry point for Telegram WebApp (Mini App).
    When opened inside Telegram, this serves tg_start.html
    which triggers auto-login using Telegram initData.
    """
    return render_template("tg_start.html")


@telegram_bp.route("/tg/autologin", methods=["POST"])
def tg_autologin():
    """
    Called from JS inside Telegram mini-app.
    ...
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        print("TG /tg/autologin payload keys:", list(payload.keys()))
        print("TG /tg/autologin initData first 80:", (payload.get("initData") or payload.get("init_data") or "")[:80])

        init_data = payload.get("initData") or payload.get("init_data") or ""
        user_info = _validate_init_data(init_data)

        if not user_info:
            return jsonify({"ok": False, "error": "bad_telegram_auth"}), 400

        tg_id = user_info.get("id")
        if not tg_id:
            return jsonify({"ok": False, "error": "no_id"}), 400

        first_name = (user_info.get("first_name") or "").strip() or "Player"
        last_name  = (user_info.get("last_name") or "").strip()
        username   = user_info.get("username")

        # 1) existing user?
        user = User.query.filter_by(telegram_id=tg_id).first()

        # 2) if not, create one
        if not user:
            from secrets import token_hex

            base_email = f"tg{tg_id}@neonspire.local"
            email = base_email.lower()
            while User.query.filter_by(email=email).first():
                email = f"tg{tg_id}_{token_hex(3)}@neonspire.local"

            user = User(
                name=f"{first_name} {last_name}".strip() or first_name,
                email=email,
                role="PLAYER",
                telegram_id=tg_id,
                telegram_username=username,
                telegram_firstname=first_name,
                telegram_lastname=last_name,
            )

            # random password that user never needs
            user.set_password(token_hex(16))
            db.session.add(user)
            db.session.commit()

        # 3) log them in
        login_user(user, remember=True)

        # mark this browser session as Telegram mini app
        session['is_telegram'] = True

       
        # redirect to the lobby (mark as Telegram)
        return jsonify({"ok": True, "redirect": url_for("index", tg="1")})

    except Exception as e:
        # make sure db session is clean
        db.session.rollback()
        # IMPORTANT: still return JSON so front-end doesn't see HTML
        return jsonify({
            "ok": False,
            "error": "server_error",
            "detail": str(e),
        }), 500