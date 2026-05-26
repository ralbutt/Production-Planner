import datetime
from flask import Blueprint, render_template, jsonify
from database import get_db

display_bp = Blueprint("display", __name__, url_prefix="/display")

# No auth required — this is the workshop screen


@display_bp.route("/")
def screen():
    """Auto-rotating display for workshop screen — no login needed."""
    return render_template("display/screen.html")


@display_bp.route("/data")
def data():
    """JSON endpoint polled by the display screen every 60s."""
    db    = get_db()
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    week_start = monday.isoformat()

    # Current week assignments grouped by employee
    # Individual slot rows for Gantt bar placement
    rows = db.execute("""
        SELECT e.id emp_id, e.name emp_name, e.team,
               j.id job_id, j.job_number, j.description,
               p.part_number, j.quantity, j.status,
               j.due_date, j.erp_ref,
               ja.week_start, ja.day_of_week, ja.hour_slot
        FROM job_assignments ja
        JOIN jobs j  ON j.id  = ja.job_id
        JOIN parts p ON p.id  = j.part_id
        JOIN employees e ON e.id = ja.employee_id
        WHERE ja.week_start = ?
          AND j.status != 'Complete'
        ORDER BY e.team, e.name, ja.day_of_week, ja.hour_slot
    """, (week_start,)).fetchall()

    next_week = (monday + datetime.timedelta(days=7)).isoformat()
    next_rows = db.execute("""
        SELECT e.id emp_id, e.name emp_name, e.team,
               j.id job_id, j.job_number, j.description,
               p.part_number, j.quantity, j.status,
               j.due_date, j.erp_ref,
               ja.week_start, ja.day_of_week, ja.hour_slot
        FROM job_assignments ja
        JOIN jobs j  ON j.id  = ja.job_id
        JOIN parts p ON p.id  = j.part_id
        JOIN employees e ON e.id = ja.employee_id
        WHERE ja.week_start = ?
          AND j.status != 'Complete'
        ORDER BY e.team, e.name, ja.day_of_week, ja.hour_slot
    """, (next_week,)).fetchall()

    db.close()

    def fmt(rows):
        ops = {}
        job_slots = {}  # (emp_id, job_id) -> list of [day_of_week, hour_slot]
        for r in rows:
            eid = r["emp_id"]
            if eid not in ops:
                ops[eid] = {"id": eid, "name": r["emp_name"],
                            "team": r["team"], "jobs": []}
            # Track slots per job per operator
            key = (eid, r["job_number"])
            if key not in job_slots:
                job_slots[key] = {
                    "job_number":  r["job_number"],
                    "description": r["description"] or r["part_number"],
                    "quantity":    int(r["quantity"]),
                    "status":      r["status"],
                    "due_date":    r["due_date"],
                    "hours":       0,
                    "slots":       [],
                    "so_ref":      r["erp_ref"],
                    "is_late":     bool(r["due_date"] and r["due_date"] < today.isoformat()),
                }
                ops[eid]["jobs"].append(job_slots[key])
            job_slots[key]["hours"] += 1
            job_slots[key]["slots"].append([r["day_of_week"], r["hour_slot"]])
        return list(ops.values())

    return jsonify({
        "week_start":  week_start,
        "next_week":   next_week,
        "today":       today.isoformat(),
        "this_week":   fmt(rows),
        "next_week_ops": fmt(next_rows),
    })
