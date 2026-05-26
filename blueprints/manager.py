from flask import Blueprint, render_template, session
from blueprints.auth import manager_required
from database import get_db

manager_bp = Blueprint("manager", __name__, url_prefix="/manager")


@manager_bp.route("/")
@manager_bp.route("/dashboard")
@manager_required
def dashboard():
    db = get_db()
    employee_count = db.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    active_count   = db.execute("SELECT COUNT(*) FROM employees WHERE active=1").fetchone()[0]
    user_count     = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    manager_count  = db.execute("SELECT COUNT(*) FROM users WHERE role='manager' AND active=1").fetchone()[0]
    operator_count = db.execute("SELECT COUNT(*) FROM users WHERE role='operator' AND active=1").fetchone()[0]
    db.close()
    return render_template("manager/dashboard.html",
                           username=session["username"],
                           employee_count=employee_count,
                           active_count=active_count,
                           user_count=user_count,
                           manager_count=manager_count,
                           operator_count=operator_count,
                           active_page="dashboard")
