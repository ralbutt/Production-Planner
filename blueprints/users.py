from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session
)
from database import get_db
from blueprints.auth import manager_required, log_action
from bcrypt_compat import hashpw, gensalt

users_bp = Blueprint("users", __name__, url_prefix="/manager/users")


# ── list ───────────────────────────────────────────────────────────────────

@users_bp.route("/")
@manager_required
def list_users():
    db = get_db()
    users = db.execute("""
        SELECT u.*, e.name emp_name
        FROM users u
        LEFT JOIN employees e ON e.id = u.employee_id
        ORDER BY u.active DESC, u.role, u.username
    """).fetchall()
    employees_unlinked = db.execute("""
        SELECT e.id, e.name FROM employees e
        WHERE e.active=1
          AND NOT EXISTS (SELECT 1 FROM users u WHERE u.employee_id = e.id)
        ORDER BY e.name
    """).fetchall()
    db.close()
    return render_template("manager/users/list.html",
                           users=users,
                           employees_unlinked=employees_unlinked,
                           active_page="users")


# ── add user ───────────────────────────────────────────────────────────────

@users_bp.route("/add", methods=["POST"])
@manager_required
def add_user():
    db          = get_db()
    username    = request.form.get("username", "").strip()
    password    = request.form.get("password", "")
    role        = request.form.get("role", "operator")
    employee_id = request.form.get("employee_id") or None

    errors = []
    if not username:          errors.append("Username is required.")
    if len(password) < 8:    errors.append("Password must be at least 8 characters.")
    if role not in ("manager","operator"): errors.append("Invalid role.")

    existing = db.execute(
        "SELECT id FROM users WHERE username=? COLLATE NOCASE", (username,)
    ).fetchone()
    if existing:
        errors.append(f'Username "{username}" is already taken.')

    if errors:
        for e in errors:
            flash(e, "error")
        db.close()
        return redirect(url_for("users.list_users"))

    hashed = hashpw(password.encode(), gensalt()).decode()
    db.execute(
        "INSERT INTO users(username, password, role, employee_id) VALUES(?,?,?,?)",
        (username, hashed, role, employee_id)
    )
    db.commit()
    log_action(session["user_id"], "add_user", f"Created user {username} role={role}")
    db.close()
    flash(f'Account "{username}" created.', "success")
    return redirect(url_for("users.list_users"))


# ── reset password ─────────────────────────────────────────────────────────

@users_bp.route("/<int:user_id>/reset-password", methods=["POST"])
@manager_required
def reset_password(user_id):
    db       = get_db()
    password = request.form.get("password", "")
    user     = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()

    if not user:
        flash("User not found.", "error")
        db.close()
        return redirect(url_for("users.list_users"))

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        db.close()
        return redirect(url_for("users.list_users"))

    hashed = hashpw(password.encode(), gensalt()).decode()
    db.execute("UPDATE users SET password=? WHERE id=?", (hashed, user_id))
    db.commit()
    log_action(session["user_id"], "reset_password",
               f"Reset password for user {user['username']}")
    db.close()
    flash(f'Password reset for "{user["username"]}".', "success")
    return redirect(url_for("users.list_users"))


# ── toggle active ──────────────────────────────────────────────────────────

@users_bp.route("/<int:user_id>/toggle", methods=["POST"])
@manager_required
def toggle_user(user_id):
    if user_id == session["user_id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("users.list_users"))

    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user:
        new_state = 0 if user["active"] else 1
        db.execute("UPDATE users SET active=? WHERE id=?", (new_state, user_id))
        db.commit()
        action = "activated" if new_state else "deactivated"
        log_action(session["user_id"], f"user_{action}",
                   f"User {user['username']} {action}")
        flash(f'"{user["username"]}" {action}.', "success")
    db.close()
    return redirect(url_for("users.list_users"))
