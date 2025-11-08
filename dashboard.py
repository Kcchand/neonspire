from flask import Blueprint, redirect, url_for
from flask_login import login_required, current_user
from werkzeug.exceptions import abort

dash_bp = Blueprint("dash", __name__)

# Use non-conflicting paths to jump to each dashboard

@dash_bp.route("/go/admin")
@login_required
def admin():
    if current_user.role == "ADMIN":
        return redirect(url_for("adminbp.admin_home"))
    abort(403)

@dash_bp.route("/go/employee")
@login_required
def employee():
    if current_user.role in ("EMPLOYEE", "ADMIN"):
        return redirect(url_for("employeebp.employee_home"))
    abort(403)

@dash_bp.route("/go/player")
@login_required
def player():
    if current_user.role in ("PLAYER", "ADMIN"):
        return redirect(url_for("playerbp.player_home"))
    abort(403)