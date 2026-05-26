import datetime
from flask import Blueprint, render_template, redirect, url_for, session, flash
from database import get_db
from blueprints.auth import login_required

operator_bp = Blueprint("operator", __name__, url_prefix="/operator")


def _get_operator_employee(db, user_id):
    """Link logged-in user to their employee record by matching username/name."""
    user = db.execute("SELECT id, username, employee_id FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return None
    # Try linked employee_id first, then name match on username
    if user["employee_id"]:
        emp = db.execute("""
            SELECT e.id, e.name, e.team, t.name grade
            FROM employees e JOIN tiers t ON t.id=e.tier_id
            WHERE e.id=?
        """, (user["employee_id"],)).fetchone()
        if emp:
            return emp
    emp = db.execute("""
        SELECT e.id, e.name, e.team, t.name grade
        FROM employees e JOIN tiers t ON t.id=e.tier_id
        WHERE LOWER(e.name) = LOWER(?)
    """, (user["username"] or "",)).fetchone()
    return emp


@operator_bp.route("/")
@operator_bp.route("/dashboard")
@login_required
def dashboard():
    db     = get_db()
    today  = datetime.date.today()
    # Week commencing Monday
    monday = today - datetime.timedelta(days=today.weekday())
    week_start = monday.isoformat()

    emp = _get_operator_employee(db, session["user_id"])

    if not emp:
        # Fall back: show by username match to let manager see any operator
        # In production Denisa would link users to employees
        db.close()
        return render_template("operator/no_employee.html",
                               username=session.get("username",""))

    emp_id = emp["id"]

    # ── This week's assignments ───────────────────────────────
    this_week_raw = db.execute("""
        SELECT j.id job_id, j.job_number, j.description,
               p.part_number, j.quantity, j.status, j.due_date,
               j.sale_value, j.erp_ref so_ref, j.customer,
               COUNT(ja.id) hours_this_week
        FROM job_assignments ja
        JOIN jobs j ON j.id = ja.job_id
        JOIN parts p ON p.id = j.part_id
        WHERE ja.employee_id = ?
          AND ja.week_start = ?
        GROUP BY j.id
        ORDER BY j.due_date ASC NULLS LAST, j.job_number
    """, (emp_id, week_start)).fetchall()

    this_week = [dict(r) for r in this_week_raw]

    # ── Upcoming assignments (next 7 weeks) ───────────────────
    upcoming_raw = db.execute("""
        SELECT j.id job_id, j.job_number, j.description,
               p.part_number, j.quantity, j.status, j.due_date,
               j.erp_ref so_ref, ja.week_start,
               COUNT(ja.id) hours_that_week
        FROM job_assignments ja
        JOIN jobs j ON j.id = ja.job_id
        JOIN parts p ON p.id = j.part_id
        WHERE ja.employee_id = ?
          AND ja.week_start > ?
        GROUP BY j.id, ja.week_start
        ORDER BY ja.week_start, j.due_date ASC NULLS LAST
        LIMIT 20
    """, (emp_id, week_start)).fetchall()

    upcoming = [dict(r) for r in upcoming_raw]

    db.close()
    return render_template("operator/dashboard.html",
        operator=emp,
        this_week=this_week,
        upcoming=upcoming,
        week_start=week_start,
        today=today.isoformat(),
        active_page="operator",
    )


@operator_bp.route("/job/<int:job_id>/complete", methods=["POST"])
@login_required
def mark_complete(job_id):
    db  = get_db()
    emp = _get_operator_employee(db, session["user_id"])
    if emp:
        # Only allow marking jobs assigned to this operator
        assigned = db.execute("""
            SELECT 1 FROM job_assignments
            WHERE job_id=? AND employee_id=?
        """, (job_id, emp["id"])).fetchone()
        if assigned:
            db.execute("""UPDATE jobs SET status='Complete',
                          updated_at=datetime('now') WHERE id=?""", (job_id,))
            db.commit()
            flash("Job marked complete.", "success")
        else:
            flash("You are not assigned to that job.", "error")
    db.close()
    return redirect(url_for("operator.dashboard"))
