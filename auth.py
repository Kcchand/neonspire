# auth.py
import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, render_template_string,
    flash, session
)
from flask_login import login_user, logout_user, current_user, login_required

from models import db, User, EmailToken, PasswordResetToken

auth_bp = Blueprint("auth", __name__)

# =========================================================
# Helpers
# =========================================================

def flash_once(message: str, category: str = "message") -> None:
    """
    Deduplicates flashes within the same response cycle.
    If a (category, message) pair already exists in session['_flashes'],
    don't add it again. This prevents the double banner you saw.
    """
    try:
        current = session.get("_flashes", [])
        if (category, message) not in current:
            flash(message, category)
    except Exception:
        # If anything odd with session, fall back to normal flash.
        flash(message, category)


def _is_safe_next(next_url: str) -> bool:
    """
    Allow only relative paths within this app, e.g. '/admin', '/player'.
    No schema/host, no //, no external redirects.
    """
    if not next_url:
        return False
    if "://" in next_url or next_url.startswith("//"):
        return False
    # Disallow weird characters; allow /, ?, =, -, _, . and alphanumerics
    return bool(re.fullmatch(r"[\/\w\-\.\?\=\&%#]*", next_url))


def _role_home(role: str) -> str:
    """Default landing per role."""
    if role == "ADMIN":
        return url_for("adminbp.admin_home")
    if role == "EMPLOYEE":
        return url_for("employeebp.employee_home")
    return url_for("index")


def _smtp_send(to_email: str, subject: str, html: str) -> bool:
    """
    Send email using environment-configured SMTP.

    Expected env (example):
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, SMTP_TLS (1/0)

    Returns True if the message was handed to SMTP successfully.
    In dev (no SMTP), it prints the email to console and returns False,
    but we never expose links in flash messages.
    """
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pw   = os.getenv("SMTP_PASS")
    from_addr = os.getenv("SMTP_FROM", user or "no-reply@localhost")
    use_tls = os.getenv("SMTP_TLS", "1") not in ("0", "false", "False")

    if not host or not from_addr:
        # Dev-mode fallback: print to server logs only
        print("\n[DEV-MAIL] Would send email:")
        print(f"To: {to_email}\nSubject: {subject}\nBody (HTML):\n{html}\n")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_email
    msg.set_content("This message requires an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")

    try:
        server = smtplib.SMTP(host, port)
        if use_tls:
            server.starttls()
        if user and pw:
            server.login(user, pw)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        # Log and fall back silently (UI will still say 'email sent')
        print(f"[SMTP ERROR] {e}")
        print("\n[DEV-MAIL Fallback] Would send email:")
        print(f"To: {to_email}\nSubject: {subject}\nBody (HTML):\n{html}\n")
        return False


def _send_verification_email(user: User) -> bool:
    """Email a verify link. Never surface the URL in the UI."""
    token = EmailToken.issue(user.id)
    verify_url = url_for("auth.verify_email", token=token.token, _external=True)
    html = f"""
      <p>Hi {user.name or 'there'},</p>
      <p>Confirm your email for NeonSpire Casino by clicking the link below:</p>
      <p><a href="{verify_url}">Verify my email</a></p>
      <p>If you didn’t create this account, you can ignore this message.</p>
    """
    ok = _smtp_send(user.email, "Verify your email", html)
    return ok


def _send_reset_email(user: User, token: PasswordResetToken) -> bool:
    """Email a password-reset link. Never surface the URL in the UI."""
    reset_url = url_for("auth.reset_get", token=token.token, _external=True)
    html = f"""
      <p>Hi {user.name or 'there'},</p>
      <p>Use the link below to reset your password. This link expires soon.</p>
      <p><a href="{reset_url}">Reset my password</a></p>
      <p>If you didn’t request this, you can ignore this message.</p>
    """
    ok = _smtp_send(user.email, "Password reset", html)
    return ok


# =========================================================
# Auth routes
# =========================================================

@auth_bp.get("/login")
def login_get():
    if current_user.is_authenticated:
        return redirect(_role_home(current_user.role or "PLAYER"))
    return render_template("login.html", page_title="Sign In")


@auth_bp.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    remember = bool(request.form.get("remember"))

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash_once("Invalid email or password.", "error")
        return redirect(url_for("auth.login_get"))

    # No email verification check – players can log in immediately
    login_user(user, remember=remember)

    # role-aware redirect
    next_url = request.args.get("next") or request.form.get("next") or ""
    if _is_safe_next(next_url):
        if user.role != "ADMIN" and next_url.startswith("/admin"):
            return redirect(_role_home(user.role))
        if user.role not in ("ADMIN", "EMPLOYEE") and next_url.startswith("/employee"):
            return redirect(_role_home(user.role))
        return redirect(next_url)

    return redirect(_role_home(user.role))


@auth_bp.get("/register")
def register_get():
    if current_user.is_authenticated:
        return redirect(_role_home(current_user.role or "PLAYER"))
    return render_template("register.html", page_title="Create Account")


@auth_bp.post("/register")
def register_post():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    mobile = (request.form.get("mobile") or "").strip()
    password = request.form.get("password") or ""
    confirm  = request.form.get("confirm_password") or ""

    # Basic validations
    if not name or not email or not password or not confirm:
        flash_once("All fields are required.", "error")
        return redirect(url_for("auth.register_get"))

    if password != confirm:
        flash_once("Passwords do not match.", "error")
        return redirect(url_for("auth.register_get"))

    if User.query.filter_by(email=email).first():
        flash_once("Email already registered.", "error")
        return redirect(url_for("auth.register_get"))

    # Optional mobile format check (E.164-ish)
    if mobile and not re.fullmatch(r"^\+?[0-9]{7,15}$", mobile):
        flash_once("Enter a valid mobile number (use country code, e.g. +15551234567).", "error")
        return redirect(url_for("auth.register_get"))

    # Create PLAYER (unverified)
        # Create PLAYER (mark as verified immediately – no email flow)
    u = User(
        name=name,
        email=email,
        mobile=mobile,
        role="PLAYER",
        email_verified=True,
        email_verified_at=datetime.utcnow()
    )
    u.set_password(password)
    db.session.add(u)
    db.session.commit()

    # Auto login after registration
    login_user(u)

    # Redirect player to Lobby page immediately after first-time registration
    return redirect(_role_home(u.role or "PLAYER"))


@auth_bp.get("/verify-email")
def verify_email():
    token = (request.args.get("token") or "").strip()
    if not token:
        flash_once("Missing token.", "error")
        return redirect(url_for("auth.login_get"))

    rec = EmailToken.query.filter_by(token=token, purpose="verify").first()
    if not rec:
        flash_once("Invalid or expired token.", "error")
        return redirect(url_for("auth.login_get"))

    if rec.expires_at and rec.expires_at < datetime.utcnow():
        uid = rec.user_id
        db.session.delete(rec)
        db.session.commit()
        # silently re-issue + notify
        u = db.session.get(User, uid)
        if u:
            _send_verification_email(u)
        flash_once("Verification link expired. We’ve sent a new one to your email.", "error")
        return redirect(url_for("auth.login_get"))

    # Mark verified
    u = db.session.get(User, rec.user_id)
    if not u:
        flash_once("Account not found.", "error")
        return redirect(url_for("auth.login_get"))

    u.email_verified = True
    u.email_verified_at = datetime.utcnow()
    db.session.delete(rec)
    db.session.commit()

    flash_once("Email verified! You can now sign in.", "success")
    return redirect(url_for("auth.login_get"))


@auth_bp.route("/resend-verification", methods=["GET", "POST"])
def resend_verification():
    if current_user.is_authenticated:
        user = current_user
    else:
        email = (request.form.get("email") or request.args.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first()

    # Do not reveal whether a user exists
    if not user:
        flash_once("If the account exists, a verification email will be sent.", "success")
        return redirect(url_for("auth.login_get"))

    if user.email_verified:
        flash_once("This email is already verified. Please sign in.", "success")
        return redirect(url_for("auth.login_get"))

    _send_verification_email(user)
    flash_once("Verification email sent. Please check your inbox.", "success")
    return redirect(url_for("auth.login_get"))


@auth_bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash_once("Signed out.", "success")
    return redirect(url_for("index"))


# =========================================================
# Forgot / Reset password
# =========================================================

@auth_bp.get("/forgot")
def forgot_get():
    # If you have templates/forgot.html, it will be used; otherwise inline fallback renders.
    return render_template("forgot.html") if _template_exists("forgot.html") else render_template_string("""
    {% extends "base.html" %}
    {% block hero %}<div class="hero"><div class="hero__title">Forgot password</div></div>{% endblock %}
    {% block content %}
      <div class="shell" style="max-width:520px">
        <div class="panel">
          <form method="post" action="{{ url_for('auth.forgot_post') }}">
            <label>Email</label>
            <input class="input" name="email" type="email" placeholder="you@example.com" required>
            <div class="form-actions"><button class="btn btn-primary" type="submit">Send reset link</button></div>
          </form>
        </div>
      </div>
    {% endblock %}""")


@auth_bp.post("/forgot")
def forgot_post():
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash_once("Enter your account email.", "error")
        return redirect(url_for("auth.forgot_get"))

    user = User.query.filter_by(email=email).first()
    if not user:
        # Do not reveal account existence
        flash_once("If the account exists, a reset link will be sent.", "success")
        return redirect(url_for("auth.login_get"))

    token = PasswordResetToken.issue(user.id)
    _send_reset_email(user, token)
    flash_once("Password reset link sent. Please check your email.", "success")
    return redirect(url_for("auth.login_get"))


@auth_bp.get("/reset")
def reset_get():
    token = (request.args.get("token") or "").strip()
    if not token:
        flash_once("Missing token.", "error")
        return redirect(url_for("auth.login_get"))

    return render_template("reset.html", token=token) if _template_exists("reset.html") else render_template_string("""
    {% extends "base.html" %}
    {% block hero %}<div class="hero"><div class="hero__title">Reset password</div></div>{% endblock %}
    {% block content %}
      <div class="shell" style="max-width:520px">
        <div class="panel">
          <form method="post" action="{{ url_for('auth.reset_post') }}">
            <input type="hidden" name="token" value="{{ token }}">
            <label>New Password</label>
            <input class="input" name="password" type="password" required>
            <label>Confirm Password</label>
            <input class="input" name="confirm_password" type="password" required>
            <div class="form-actions"><button class="btn btn-primary" type="submit">Update password</button></div>
          </form>
        </div>
      </div>
    {% endblock %}""", token=token)


@auth_bp.post("/reset")
def reset_post():
    token_val = (request.form.get("token") or "").strip()
    pw = request.form.get("password") or ""
    cpw = request.form.get("confirm_password") or ""

    if not token_val or not pw or not cpw:
        flash_once("All fields are required.", "error")
        return redirect(url_for("auth.login_get"))

    if pw != cpw:
        flash_once("Passwords do not match.", "error")
        return redirect(url_for("auth.reset_get", token=token_val))

    rec = PasswordResetToken.query.filter_by(token=token_val, used_at=None).first()
    if not rec:
        flash_once("Invalid or expired token.", "error")
        return redirect(url_for("auth.login_get"))

    if rec.expires_at and rec.expires_at < datetime.utcnow():
        db.session.delete(rec)
        db.session.commit()
        flash_once("Reset link expired. Please request a new one.", "error")
        return redirect(url_for("auth.forgot_get"))

    user = db.session.get(User, rec.user_id)
    if not user:
        flash_once("Account not found.", "error")
        return redirect(url_for("auth.login_get"))

    user.set_password(pw)
    rec.used_at = datetime.utcnow()
    db.session.commit()

    flash_once("Password updated. You can now sign in.", "success")
    return redirect(url_for("auth.login_get"))


# =========================================================
# Utility
# =========================================================
def _template_exists(name: str) -> bool:
    """
    Returns True if a Jinja template file exists.
    Lets us gracefully fall back to simple inline forms
    when the project hasn't created the optional templates yet.
    """
    try:
        render_template(name)
        return True
    except Exception:
        return False
