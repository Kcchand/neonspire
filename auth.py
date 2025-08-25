# auth.py
import re
from urllib.parse import urlparse

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, current_user, login_required

from models import db, User

auth_bp = Blueprint("auth", __name__)

# ---------- helpers ----------

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
    """
    Default landing per role.
    ADMIN    -> admin console
    EMPLOYEE -> employee desk
    PLAYER   -> home (player lobby lives on '/')
    """
    if role == "ADMIN":
        return url_for("adminbp.admin_home")
    if role == "EMPLOYEE":
        return url_for("employeebp.employee_home")
    # PLAYER or anything else
    return url_for("index")


# ---------- routes ----------

@auth_bp.get("/login")
def login_get():
    if current_user.is_authenticated:
        # already logged in → go to your role home
        return redirect(_role_home(current_user.role or "PLAYER"))
    return render_template("login.html", page_title="Sign In")


@auth_bp.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    remember = bool(request.form.get("remember"))

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash("Invalid email or password.", "error")
        return redirect(url_for("auth.login_get"))

    login_user(user, remember=remember)

    # role-aware redirect
    next_url = request.args.get("next") or request.form.get("next") or ""
    if _is_safe_next(next_url):
        # Optional: keep next only if it's consistent with role
        # e.g., block non-admins from being redirected into /admin
        if user.role != "ADMIN" and next_url.startswith("/admin"):
            return redirect(_role_home(user.role))
        if user.role not in ("ADMIN", "EMPLOYEE") and (next_url.startswith("/employee")):
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
    password = request.form.get("password") or ""

    if not name or not email or not password:
        flash("All fields are required.", "error")
        return redirect(url_for("auth.register_get"))

    if User.query.filter_by(email=email).first():
        flash("Email already registered.", "error")
        return redirect(url_for("auth.register_get"))

    # Create PLAYER by default
    u = User(name=name, email=email, role="PLAYER")
    u.set_password(password)
    db.session.add(u)
    db.session.commit()

    login_user(u)
    flash("Welcome! Your account is ready.", "success")
    return redirect(_role_home("PLAYER"))


@auth_bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("index"))