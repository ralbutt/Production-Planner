from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from database import get_db
from blueprints.auth import manager_required, log_action
import datetime

availability_bp = Blueprint("availability", __name__, url_prefix="/manager/availability")


def _current_year():
    return datetime.date.today().year


@availability_bp.route("/")
@manager_required
def index():
    db   = get_db()
    year = request.args.get("year", _current_year(), type=int)

    bank_holidays = db.execute(
        "SELECT * FROM bank_holidays WHERE date LIKE ? ORDER BY date",
        (f"{year}%",)
    ).fetchall()

    employees = db.execute("""
        SELECT e.id, e.name, e.team, t.name grade
        FROM employees e JOIN tiers t ON t.id=e.tier_id
        WHERE e.active=1 ORDER BY e.team, e.name
    """).fetchall()

    # Weekend working entries for this year
    weekend_working = db.execute("""
        SELECT ea.*, e.name emp_name
        FROM employee_availability ea
        JOIN employees e ON e.id=ea.employee_id
        WHERE ea.day_type='weekend_working' AND ea.date LIKE ?
        ORDER BY ea.date, e.name
    """, (f"{year}%",)).fetchall()

    # Absences for this year
    absences = db.execute("""
        SELECT ea.*, e.name emp_name
        FROM employee_availability ea
        JOIN employees e ON e.id=ea.employee_id
        WHERE (ea.day_type='absence' OR ea.day_type IS NULL) AND ea.date LIKE ?
        ORDER BY ea.date, e.name
    """, (f"{year}%",)).fetchall()

    db.close()
    return render_template("manager/availability/index.html",
        bank_holidays=bank_holidays,
        employees=employees,
        weekend_working=weekend_working,
        absences=absences,
        year=year,
        prev_year=year-1,
        next_year=year+1,
        active_page="availability",
    )


@availability_bp.route("/bank-holiday/add", methods=["POST"])
@manager_required
def add_bank_holiday():
    date = request.form.get("date","").strip()
    name = request.form.get("name","").strip()
    if not date or not name:
        flash("Date and name are required.", "error")
        return redirect(url_for("availability.index"))
    db = get_db()
    try:
        db.execute("INSERT INTO bank_holidays(date,name,auto_loaded) VALUES(?,?,0)", (date, name))
        db.commit()
        log_action(session["user_id"], "add_bank_holiday", f"{date} — {name}")
        flash(f"Bank holiday added: {name} on {date}.", "success")
    except Exception:
        flash("That date already exists.", "error")
    db.close()
    return redirect(url_for("availability.index"))


@availability_bp.route("/bank-holiday/<int:bh_id>/delete", methods=["POST"])
@manager_required
def delete_bank_holiday(bh_id):
    db = get_db()
    row = db.execute("SELECT date, name FROM bank_holidays WHERE id=?", (bh_id,)).fetchone()
    if row:
        db.execute("DELETE FROM bank_holidays WHERE id=?", (bh_id,))
        db.commit()
        log_action(session["user_id"], "delete_bank_holiday", f"{row['date']} — {row['name']}")
        flash(f"Removed: {row['name']} ({row['date']}).", "success")
    db.close()
    return redirect(url_for("availability.index"))


@availability_bp.route("/weekend-working/add", methods=["POST"])
@manager_required
def add_weekend_working():
    employee_id = request.form.get("employee_id", type=int)
    date        = request.form.get("date","").strip()
    if not employee_id or not date:
        flash("Employee and date are required.", "error")
        return redirect(url_for("availability.index"))

    # Validate it's actually a weekend
    try:
        d = datetime.date.fromisoformat(date)
        if d.weekday() < 5:
            flash("That date is a weekday — only add weekend working for Saturdays/Sundays.", "error")
            return redirect(url_for("availability.index"))
    except ValueError:
        flash("Invalid date.", "error")
        return redirect(url_for("availability.index"))

    db  = get_db()
    emp = db.execute("SELECT name FROM employees WHERE id=?", (employee_id,)).fetchone()
    try:
        db.execute("""
            INSERT OR IGNORE INTO employee_availability(employee_id, date, unavailable, day_type)
            VALUES(?,?,0,'weekend_working')
        """, (employee_id, date))
        db.commit()
        log_action(session["user_id"], "add_weekend_working", f"{emp['name']} working {date}")
        flash(f"{emp['name']} added as working on {date}.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    db.close()
    return redirect(url_for("availability.index"))


@availability_bp.route("/weekend-working/<int:entry_id>/delete", methods=["POST"])
@manager_required
def delete_weekend_working(entry_id):
    db  = get_db()
    row = db.execute("""
        SELECT ea.date, e.name FROM employee_availability ea
        JOIN employees e ON e.id=ea.employee_id WHERE ea.id=?
    """, (entry_id,)).fetchone()
    if row:
        db.execute("DELETE FROM employee_availability WHERE id=?", (entry_id,))
        db.commit()
        flash(f"Removed weekend working: {row['name']} on {row['date']}.", "success")
    db.close()
    return redirect(url_for("availability.index"))


@availability_bp.route("/absence/add", methods=["POST"])
@manager_required
def add_absence():
    employee_id = request.form.get("employee_id", type=int)
    date        = request.form.get("date","").strip()
    reason      = request.form.get("reason","").strip()
    if not employee_id or not date:
        flash("Employee and date are required.", "error")
        return redirect(url_for("availability.index"))
    db  = get_db()
    emp = db.execute("SELECT name FROM employees WHERE id=?", (employee_id,)).fetchone()
    try:
        db.execute("""
            INSERT OR IGNORE INTO employee_availability(employee_id, date, unavailable, reason, day_type)
            VALUES(?,?,1,?,'absence')
        """, (employee_id, date, reason))
        db.commit()
        log_action(session["user_id"], "add_absence", f"{emp['name']} absent {date}")
        flash(f"{emp['name']} marked absent on {date}.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    db.close()
    return redirect(url_for("availability.index"))


@availability_bp.route("/absence/<int:entry_id>/delete", methods=["POST"])
@manager_required
def delete_absence(entry_id):
    db  = get_db()
    row = db.execute("""
        SELECT ea.date, e.name FROM employee_availability ea
        JOIN employees e ON e.id=ea.employee_id WHERE ea.id=?
    """, (entry_id,)).fetchone()
    if row:
        db.execute("DELETE FROM employee_availability WHERE id=?", (entry_id,))
        db.commit()
        flash(f"Removed absence: {row['name']} on {row['date']}.", "success")
    db.close()
    return redirect(url_for("availability.index"))
