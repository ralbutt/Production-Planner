from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, g
)
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from bcrypt_compat import hashpw, checkpw, gensalt  # real bcrypt on prod, shim in dev
from database import get_db

auth_bp = Blueprint("auth", __name__)


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def manager_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        if session.get("role") != "manager":
            flash("Manager access required.", "error")
            return redirect(url_for("operator.dashboard"))
        return f(*args, **kwargs)
    return decorated


def log_action(user_id, action, detail=None):
    db = get_db()
    db.execute(
        "INSERT INTO audit_log(user_id, action, detail) VALUES(?,?,?)",
        (user_id, action, detail)
    )
    db.commit()
    db.close()


# ------------------------------------------------------------------ #
#  First-run setup                                                     #
# ------------------------------------------------------------------ #

@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    db.close()
    if user_count > 0:
        return redirect(url_for("auth.login"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if not username:
            error = "Username is required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            hashed = hashpw(password.encode(), gensalt()).decode()
            db = get_db()
            db.execute(
                "INSERT INTO users(username, password, role) VALUES(?,?,?)",
                (username, hashed, "manager")
            )
            db.commit()
            db.close()
            flash("Manager account created. Please log in.", "success")
            return redirect(url_for("auth.login"))

    return render_template("setup.html", error=error)


# ------------------------------------------------------------------ #
#  Login / Logout                                                      #
# ------------------------------------------------------------------ #

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # Redirect if already logged in
    if "user_id" in session:
        return redirect(url_for("manager.dashboard") if session["role"] == "manager"
                        else url_for("operator.dashboard"))

    # First-run guard
    db = get_db()
    user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    db.close()
    if user_count == 0:
        return redirect(url_for("auth.setup"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1",
            (username,)
        ).fetchone()
        db.close()

        if user and checkpw(password.encode(), user["password"].encode()):
            session.permanent = True
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["role"]     = user["role"]
            log_action(user["id"], "login", f"Logged in from {request.remote_addr}")
            if user["role"] == "manager":
                return redirect(url_for("manager.dashboard"))
            else:
                return redirect(url_for("operator.dashboard"))
        else:
            error = "Incorrect username or password."

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    user_id = session.get("user_id")
    if user_id:
        log_action(user_id, "logout")
    session.clear()
    return redirect(url_for("auth.login"))
