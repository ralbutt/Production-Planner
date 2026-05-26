from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, jsonify
)
from database import get_db
from blueprints.auth import manager_required, log_action
from scheduler import run_8week_scheduler, apply_8week_schedule, eight_week_starts, next_monday
import datetime

scheduler_bp = Blueprint("scheduler", __name__, url_prefix="/manager/schedule")


def _current_monday():
    today = datetime.date.today()
    return (today - datetime.timedelta(days=today.weekday())).isoformat()


@scheduler_bp.route("/")
@manager_required
def index():
    tab        = request.args.get("tab", "gantt")
    from_date  = request.args.get("from", next_monday())
    mode_label = request.args.get("mode_label", "Deadline First")
    db         = get_db()

    weeks      = eight_week_starts(from_date)
    week_labels = []
    for w in weeks:
        d = datetime.date.fromisoformat(w)
        week_labels.append(f"W/C {d.strftime('%d %b')}")

    # All assignments across 8 weeks
    assignments = db.execute("""
        SELECT ja.*,
               e.name emp_name, e.team emp_team,
               j.job_number, j.status job_status, j.team job_team,
               j.quantity, j.planned_qty, j.for_stock,
               p.part_number, p.description part_desc
        FROM job_assignments ja
        JOIN employees e ON e.id = ja.employee_id
        JOIN jobs j      ON j.id = ja.job_id
        LEFT JOIN parts p ON p.id = j.part_id
        WHERE ja.week_start IN ({})
        ORDER BY e.name, ja.week_start, ja.day_of_week, ja.hour_slot
    """.format(",".join("?"*len(weeks))), weeks).fetchall()

    # Employees
    employees = db.execute("""
        SELECT e.*, t.name grade, t.rank grade_rank
        FROM employees e JOIN tiers t ON t.id = e.tier_id
        WHERE e.active = 1
        ORDER BY e.team, t.rank DESC, e.name
    """).fetchall()

    # Unscheduled jobs
    unscheduled = db.execute("""
        SELECT j.*, p.part_number, p.description part_desc,
               p.estimated_hours, t.name min_grade
        FROM jobs j
        LEFT JOIN parts p ON p.id = j.part_id
        LEFT JOIN tiers t ON t.id = p.min_grade_id
        WHERE j.status NOT IN ('Complete','Scheduled')
          AND j.waiting_parts = 0
          AND (j.skip_week IS NULL OR j.skip_week != ?)
        ORDER BY j.due_date ASC NULLS LAST, j.job_number ASC
    """, (weeks[0],)).fetchall()

    # Skipped this week
    skipped = db.execute("""
        SELECT j.*, p.part_number
        FROM jobs j LEFT JOIN parts p ON p.id = j.part_id
        WHERE j.skip_week = ?
    """, (weeks[0],)).fetchall()

    # Capacity analysis for issues tab
    # Build {week: {grade: {needed, available, shortfall}}}
    capacity_data = {}
    grade_avail   = {}
    for emp in employees:
        for w in weeks:
            dates = [(datetime.date.fromisoformat(w) + datetime.timedelta(days=d)).isoformat() for d in range(5)]
            emp_abs = set()  # would need to load absences properly — simplified here
            avail_slots = 5 * 8 - len(emp_abs)
            grade_avail.setdefault(w, {}).setdefault(emp["grade"], 0)
            grade_avail[w][emp["grade"]] += avail_slots

    grade_needed = {}
    for a in assignments:
        j = next((x for x in [a] if True), None)
        w = a["week_start"]
        grade_needed.setdefault(w, {})

    for w in weeks:
        capacity_data[w] = {}

    # Collect warnings from last run (stored in session)
    last_warnings  = session.pop("plan_warnings",  [])
    last_unscheduled_detail = session.pop("plan_unscheduled", [])

    # Build gantt: {emp_id: {week: {day: {slot: assignment}}}}
    gantt = {}
    for a in assignments:
        gantt.setdefault(a["employee_id"], {}) \
             .setdefault(a["week_start"],  {}) \
             .setdefault(a["day_of_week"], {})[a["hour_slot"]] = a

    DAYS_LOCAL = 5
    SLOTS_LOCAL = 8

    db.close()
    return render_template("manager/schedule/index.html",
        tab=tab,
        weeks=weeks,
        week_labels=week_labels,
        employees=employees,
        assignments=assignments,
        gantt=gantt,
        unscheduled=unscheduled,
        skipped=skipped,
        last_warnings=last_warnings,
        last_unscheduled_detail=last_unscheduled_detail,
        from_date=from_date,
        mode_label=mode_label,
        DAYS=DAYS_LOCAL,
        SLOTS=SLOTS_LOCAL,
        active_page="schedule",
    )


@scheduler_bp.route("/auto-plan", methods=["POST"])
@manager_required
def auto_plan():
    mode      = request.form.get("mode", "deadline")
    from_date = request.form.get("from_date", next_monday())
    db        = get_db()

    result = run_8week_scheduler(from_date=from_date, mode=mode, db=db)
    apply_8week_schedule(result, db=db)
    db.close()

    log_action(session["user_id"], "auto_plan_8week",
               f"from={from_date} mode={mode} "
               f"scheduled={len(result['scheduled'])} "
               f"unscheduled={len(result['unscheduled'])}")

    # Store warnings for display
    session["plan_warnings"]    = result["warnings"][:20]
    session["plan_unscheduled"] = [
        {"job_id": u["job_id"], "reason": u["reason"],
         "hours_needed": u.get("hours_needed", 0),
         "hours_available": u.get("hours_available", 0)}
        for u in result["unscheduled"][:50]
    ]

    mode_labels = {"deadline":"Deadline First","value":"Max Sales Value",
                   "efficiency":"Max Efficiency","balanced":"Balanced Mix"}
    n = len(result["scheduled"]); u = len(result["unscheduled"])
    flash(
        f"8-week plan complete ({mode_labels.get(mode,mode)}): "
        f"{n} jobs scheduled, {u} could not be placed.",
        "success" if u == 0 else "info"
    )

    return redirect(url_for("scheduler.index",
                            from_date=from_date,
                            mode_label=mode_labels.get(mode,""),
                            tab="gantt"))


@scheduler_bp.route("/clear", methods=["POST"])
@manager_required
def clear_plan():
    from_date = request.form.get("from_date", next_monday())
    weeks     = eight_week_starts(from_date)
    db        = get_db()
    for w in weeks:
        db.execute("DELETE FROM job_assignments WHERE week_start=? AND auto_planned=1", (w,))
    db.commit(); db.close()
    flash("8-week auto-plan cleared.", "success")
    return redirect(url_for("scheduler.index", from_date=from_date))


@scheduler_bp.route("/split", methods=["POST"])
@manager_required
def create_split():
    """Create a job split — preserves across replans."""
    job_id     = request.form.get("job_id", type=int)
    split_qty  = request.form.get("split_qty", type=float)
    week_pref  = request.form.get("week_preference", "")
    db         = get_db()

    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        flash("Job not found.", "error")
        db.close()
        return redirect(url_for("scheduler.index"))

    # Check split qty is valid
    existing_splits = db.execute(
        "SELECT SUM(split_qty) total FROM job_splits WHERE parent_job_id=? AND status!='Complete'",
        (job_id,)
    ).fetchone()["total"] or 0

    if split_qty <= 0 or (existing_splits + split_qty) > job["quantity"]:
        flash(f"Invalid split quantity. Maximum remaining: {job['quantity'] - existing_splits}", "error")
        db.close()
        return redirect(url_for("scheduler.index"))

    # If no splits exist yet, create first split for the original portion too
    if existing_splits == 0:
        remainder = job["quantity"] - split_qty
        if remainder > 0:
            db.execute("""
                INSERT INTO job_splits(parent_job_id, split_qty, week_preference, manually_set, created_at, updated_at)
                VALUES(?,?,?,1,datetime('now'),datetime('now'))
            """, (job_id, remainder, ""))

    db.execute("""
        INSERT INTO job_splits(parent_job_id, split_qty, week_preference, manually_set, created_at, updated_at)
        VALUES(?,?,?,1,datetime('now'),datetime('now'))
    """, (job_id, split_qty, week_pref or None))
    db.commit()

    log_action(session["user_id"], "create_split",
               f"Job {job['job_number']}: split {split_qty} units to week {week_pref}")
    flash(f"Split created: {split_qty:.0f} units → week of {week_pref}. Rerun plan to apply.", "success")
    db.close()
    return redirect(url_for("scheduler.index"))


@scheduler_bp.route("/split/<int:split_id>/delete", methods=["POST"])
@manager_required
def delete_split(split_id):
    db = get_db()
    sp = db.execute("SELECT * FROM job_splits WHERE id=?", (split_id,)).fetchone()
    if sp:
        # If deleting leaves only one split, remove them all (merge back)
        remaining = db.execute(
            "SELECT COUNT(*) FROM job_splits WHERE parent_job_id=? AND id!=? AND status!='Complete'",
            (sp["parent_job_id"], split_id)
        ).fetchone()[0]
        if remaining <= 1:
            db.execute("DELETE FROM job_splits WHERE parent_job_id=?", (sp["parent_job_id"],))
            flash("Split removed — job merged back to single block.", "success")
        else:
            db.execute("DELETE FROM job_splits WHERE id=?", (split_id,))
            flash("Split portion removed.", "success")
        db.commit()
    db.close()
    return redirect(url_for("scheduler.index"))


@scheduler_bp.route("/job/<int:job_id>/complete", methods=["POST"])
@manager_required
def mark_complete(job_id):
    db = get_db()
    job = db.execute("SELECT job_number FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job:
        db.execute("UPDATE jobs SET status='Complete', updated_at=datetime('now') WHERE id=?", (job_id,))
        db.execute("UPDATE job_splits SET status='Complete' WHERE parent_job_id=?", (job_id,))
        db.commit()
        flash(f"{job['job_number']} marked complete — will be excluded from next plan.", "success")
    db.close()
    return redirect(url_for("scheduler.index"))


@scheduler_bp.route("/api/issues")
@manager_required
def api_issues():
    """Return capacity analysis and unscheduled job details as JSON."""
    from_date = request.args.get("from", next_monday())
    db        = get_db()
    result    = run_8week_scheduler(from_date=from_date, db=db)
    db.close()

    issues = {
        "capacity":     result["capacity"],
        "unscheduled":  result["unscheduled"],
        "warnings":     result["warnings"],
        "weeks":        result["weeks"],
    }
    return jsonify(issues)


# ── Plan readiness checks ────────────────────────────────────

@scheduler_bp.route("/readiness")
@manager_required
def readiness():
    """Return readiness status as JSON for the plan strip."""
    import datetime
    db      = get_db()
    today   = datetime.date.today()
    # Next Monday reset date
    days_until_monday = (7 - today.weekday()) % 7 or 7
    next_monday = (today + datetime.timedelta(days=days_until_monday)).isoformat()

    checks_raw = db.execute("SELECT * FROM planning_checks").fetchall()
    checks = {r["check_name"]: dict(r) for r in checks_raw}

    def is_stale(check_name, max_days=2):
        c = checks.get(check_name, {})
        if not c.get("checked_at"):
            return True
        # Auto-reset if past resets_on date
        resets = c.get("resets_on")
        if resets and today.isoformat() >= resets:
            return True
        try:
            checked = datetime.date.fromisoformat(c["checked_at"][:10])
            return (today - checked).days > max_days
        except Exception:
            return True

    # WO import recency
    last_wo_import = db.execute(
        "SELECT MAX(created_at) last FROM jobs WHERE source='import'"
    ).fetchone()["last"]
    wo_days_ago = None
    if last_wo_import:
        try:
            d = datetime.date.fromisoformat(last_wo_import[:10])
            wo_days_ago = (today - d).days
        except Exception:
            pass

    # SO import recency
    last_so_import = db.execute(
        "SELECT MAX(created_at) last FROM jobs WHERE sale_value IS NOT NULL AND source='import'"
    ).fetchone()["last"]
    so_days_ago = None
    if last_so_import:
        try:
            d = datetime.date.fromisoformat(last_so_import[:10])
            so_days_ago = (today - d).days
        except Exception:
            pass

    # Jobs due next week
    next_mon = (today + datetime.timedelta(days=(7 - today.weekday()) % 7 or 7)).isoformat()
    next_fri = (datetime.date.fromisoformat(next_mon) + datetime.timedelta(days=4)).isoformat()
    urgent_count = db.execute(
        "SELECT COUNT(*) n FROM jobs WHERE due_date BETWEEN ? AND ? AND status NOT IN ('Complete')",
        (next_mon, next_fri)
    ).fetchone()["n"]

    db.close()

    def check_status(name, days_ago, max_days=2):
        c = checks.get(name, {})
        checked = bool(c.get("checked_at")) and not is_stale(name)
        if checked:
            return "ok"
        if days_ago is not None and days_ago <= max_days:
            return "ok"
        if days_ago is not None:
            return "warn"
        return "missing"

    return {
        "absences": {
            "status":     "ok" if not is_stale("absences") else "warn",
            "checked_at": checks.get("absences", {}).get("checked_at"),
            "label":      "Absences & Availability",
        },
        "works_orders": {
            "status":   check_status("works_orders", wo_days_ago),
            "days_ago": wo_days_ago,
            "label":    "Works Orders",
        },
        "sales_orders": {
            "status":   check_status("sales_orders", so_days_ago),
            "days_ago": so_days_ago,
            "label":    "Sales Orders",
            "urgent":   urgent_count,
        },
        "next_monday":  next_monday,
        "all_clear":    all(
            (checks.get(n, {}).get("checked_at") and not is_stale(n))
            or (n == "works_orders" and wo_days_ago is not None and wo_days_ago <= 2)
            or (n == "sales_orders" and so_days_ago is not None and so_days_ago <= 2)
            for n in ["absences", "works_orders", "sales_orders"]
        ),
    }


@scheduler_bp.route("/check/<check_name>", methods=["POST"])
@manager_required
def mark_check(check_name):
    """Mark a readiness check as done. Resets next Monday."""
    import datetime
    if check_name not in ("absences", "works_orders", "sales_orders"):
        return {"error": "Unknown check"}, 400
    today        = datetime.date.today()
    days_to_mon  = (7 - today.weekday()) % 7 or 7
    next_monday  = (today + datetime.timedelta(days=days_to_mon)).isoformat()
    db = get_db()
    db.execute("""
        UPDATE planning_checks
        SET checked_at=datetime('now'), checked_by=?, resets_on=?
        WHERE check_name=?
    """, (session["user_id"], next_monday, check_name))
    db.commit()
    db.close()
    return {"ok": True, "resets_on": next_monday}


@scheduler_bp.route("/check/<check_name>/clear", methods=["POST"])
@manager_required
def clear_check(check_name):
    """Un-tick a readiness check."""
    db = get_db()
    db.execute("UPDATE planning_checks SET checked_at=NULL, resets_on=NULL WHERE check_name=?",
               (check_name,))
    db.commit()
    db.close()
    return {"ok": True}


# ── Manual override ──────────────────────────────────────────

@scheduler_bp.route("/assignment/move", methods=["POST"])
@manager_required
def move_assignment():
    """Reassign all slots for a job in a given week from one operator to another."""
    import json
    data        = request.get_json() or request.form
    job_id      = int(data.get("job_id",0))
    week_start  = data.get("week_start","")
    new_emp_id  = int(data.get("new_emp_id",0))

    if not job_id or not week_start or not new_emp_id:
        return {"error": "Missing parameters"}, 400

    db = get_db()

    # Verify the job and employee exist
    job = db.execute("SELECT id, job_number, team FROM jobs WHERE id=?", (job_id,)).fetchone()
    emp = db.execute("SELECT id, name, team FROM employees WHERE id=?", (new_emp_id,)).fetchone()

    if not job or not emp:
        return {"error": "Job or employee not found"}, 404

    # Team check
    if job["team"] != emp["team"]:
        db.close()
        return {"error": f"Cannot assign {emp['name']} — wrong team"}, 400

    # Delete existing assignments for this job+week and reassign
    db.execute("""
        DELETE FROM job_assignments
        WHERE job_id=? AND week_start=?
    """, (job_id, week_start))

    # Find how many slots were there
    # Re-assign: take the same count of slots from new operator
    # Get count from what was there before (we need to count before deleting — 
    # use the planned_quantities approach: just book new slots)
    
    # Count hours needed: query planned_quantities via job's estimated_hours * qty
    job_detail = db.execute("""
        SELECT j.quantity, p.estimated_hours
        FROM jobs j JOIN parts p ON p.id=j.part_id
        WHERE j.id=?
    """, (job_id,)).fetchone()
    
    hours_needed = int((job_detail["estimated_hours"] or 1) * job_detail["quantity"])

    # Book slots from new operator for this week
    booked = 0
    for day_of_week in range(1, 6):  # Mon-Fri
        if booked >= hours_needed:
            break
        for slot in range(1, 9):
            if booked >= hours_needed:
                break
            # Check not already booked
            clash = db.execute("""
                SELECT 1 FROM job_assignments
                WHERE employee_id=? AND week_start=? AND day_of_week=? AND hour_slot=?
            """, (new_emp_id, week_start, day_of_week, slot)).fetchone()
            if not clash:
                db.execute("""
                    INSERT INTO job_assignments(job_id, employee_id, week_start,
                                               day_of_week, hour_slot, planned_hours)
                    VALUES(?,?,?,?,?,1.0)
                """, (job_id, new_emp_id, week_start, day_of_week, slot))
                booked += 1

    db.commit()
    try:
        log_action(session["user_id"], "move_assignment",
                   f"Job {job['job_number']} W/C {week_start} → {emp['name']}")
    except Exception:
        pass  # audit log failure should not block the move
    db.close()
    return {"ok": True, "booked": booked, "operator": emp["name"]}


@scheduler_bp.route("/assignment/eligible/<int:job_id>")
@manager_required
def eligible_operators(job_id):
    """Return operators eligible to work on a job (correct team + grade)."""
    db  = get_db()
    job = db.execute("""
        SELECT j.team, t.rank min_rank
        FROM jobs j
        JOIN parts p ON p.id=j.part_id
        LEFT JOIN tiers t ON t.id=p.min_grade_id
        WHERE j.id=?
    """, (job_id,)).fetchone()

    if not job:
        db.close()
        return {"error": "Job not found"}, 404

    ops = db.execute("""
        SELECT e.id, e.name, e.team, t.name grade, t.rank
        FROM employees e JOIN tiers t ON t.id=e.tier_id
        WHERE e.active=1
          AND e.team=?
          AND t.rank >= ?
        ORDER BY t.rank, e.name
    """, (job["team"], job["min_rank"] or 1)).fetchall()

    db.close()
    return {"operators": [dict(o) for o in ops]}


# ── Manual planning API ──────────────────────────────────────

@scheduler_bp.route("/jobs/active-week")
@manager_required
def active_week_jobs():
    """Jobs In Progress this week — shown in the Active section."""
    import datetime
    db    = get_db()
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    week_start = monday.isoformat()

    rows = db.execute("""
        SELECT j.id, j.job_number, p.part_number, j.description,
               j.quantity, j.status, j.due_date, j.team, j.erp_ref,
               j.manual_locked,
               e.name emp_name, e.id emp_id,
               COUNT(ja.id) hours_this_week
        FROM jobs j
        JOIN parts p ON p.id = j.part_id
        LEFT JOIN job_assignments ja ON ja.job_id = j.id AND ja.week_start = ?
        LEFT JOIN employees e ON e.id = ja.employee_id
        WHERE j.status = 'In Progress'
          AND j.status != 'Complete'
        GROUP BY j.id, e.id
        ORDER BY j.due_date ASC NULLS LAST, j.job_number
    """, (week_start,)).fetchall()

    db.close()
    return {"jobs": [dict(r) for r in rows], "week_start": week_start}


@scheduler_bp.route("/jobs/to-plan")
@manager_required
def to_plan_jobs():
    """Jobs available to plan — excludes In Progress and Complete."""
    import datetime
    q = request.args.get("q","").strip().lower()
    team = request.args.get("team","")
    db = get_db()

    rows = db.execute("""
        SELECT j.id, j.job_number, p.part_number, j.description,
               j.quantity, j.status, j.due_date, j.team,
               j.erp_ref, j.sale_value, j.planned_qty,
               j.manual_locked,
               p.part_type
        FROM jobs j
        JOIN parts p ON p.id = j.part_id
        WHERE j.status NOT IN ('In Progress','Complete')
          AND j.source = 'import'
        ORDER BY j.due_date ASC NULLS LAST, j.job_number
        LIMIT 200
    """).fetchall()

    jobs = [dict(r) for r in rows]

    # Apply search filter
    if q:
        jobs = [j for j in jobs if
                q in (j["job_number"] or "").lower() or
                q in (j["part_number"] or "").lower() or
                q in (j["description"] or "").lower() or
                q in (j["erp_ref"] or "").lower()]
    if team:
        jobs = [j for j in jobs if j["team"] == team]

    db.close()
    return {"jobs": jobs, "total": len(jobs)}


@scheduler_bp.route("/jobs/<int:job_id>/complete", methods=["POST"])
@manager_required
def mark_job_complete(job_id):
    """Mark a job as Complete."""
    db = get_db()
    job = db.execute("SELECT id, job_number FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        db.close()
        return {"error": "Job not found"}, 404
    db.execute("UPDATE jobs SET status='Complete', updated_at=datetime('now') WHERE id=?",
               (job_id,))
    db.commit()
    try:
        log_action(session["user_id"], "mark_complete", f"Job {job['job_number']}")
    except Exception:
        pass
    db.close()
    return {"ok": True}


@scheduler_bp.route("/jobs/<int:job_id>/assign", methods=["POST"])
@manager_required
def manual_assign(job_id):
    """Manually assign a job to an operator for a given week."""
    import datetime
    data       = request.get_json() or {}
    emp_id     = int(data.get("emp_id", 0))
    week_start = data.get("week_start", "")

    if not emp_id or not week_start:
        return {"error": "emp_id and week_start required"}, 400

    db = get_db()
    job = db.execute("""
        SELECT j.id, j.job_number, j.team, j.quantity,
               p.estimated_hours, p.min_grade_id
        FROM jobs j JOIN parts p ON p.id=j.part_id
        WHERE j.id=?
    """, (job_id,)).fetchone()
    emp = db.execute("""
        SELECT e.id, e.name, e.team, t.rank grade_rank
        FROM employees e JOIN tiers t ON t.id=e.tier_id WHERE e.id=?
    """, (emp_id,)).fetchone()

    if not job or not emp:
        db.close()
        return {"error": "Job or employee not found"}, 404

    if job["team"] != emp["team"]:
        db.close()
        return {"error": f"Team mismatch: job is {job['team']}, operator is {emp['team']}"}, 400

    # Remove existing assignments for this job+week
    db.execute("DELETE FROM job_assignments WHERE job_id=? AND week_start=?",
               (job_id, week_start))

    # Book hours for this week (Mon-Fri, avoid clashes)
    spu   = max(1, round(job["estimated_hours"] or 1))
    qty   = int(job["quantity"] or 1)
    needed = spu * qty
    booked = 0

    for day in range(1, 6):
        for slot in range(1, 9):
            if booked >= needed:
                break
            clash = db.execute("""
                SELECT 1 FROM job_assignments
                WHERE employee_id=? AND week_start=? AND day_of_week=? AND hour_slot=?
            """, (emp_id, week_start, day, slot)).fetchone()
            if not clash:
                db.execute("""
                    INSERT INTO job_assignments
                        (job_id, employee_id, week_start, day_of_week, hour_slot,
                         planned_hours, auto_planned)
                    VALUES (?,?,?,?,?,1.0,0)
                """, (job_id, emp_id, week_start, day, slot))
                booked += 1
        if booked >= needed:
            break

    # Mark job as Scheduled and manual_locked
    db.execute("""
        UPDATE jobs SET status='Scheduled', manual_locked=1,
               updated_at=datetime('now') WHERE id=?
    """, (job_id,))
    db.commit()

    try:
        log_action(session["user_id"], "manual_assign",
                   f"Job {job['job_number']} → {emp['name']} W/C {week_start}")
    except Exception:
        pass

    db.close()
    return {"ok": True, "booked": booked, "operator": emp["name"]}


@scheduler_bp.route("/jobs/<int:job_id>/unassign", methods=["POST"])
@manager_required
def unassign_job(job_id):
    """Remove a manual assignment and set job back to Unscheduled."""
    data       = request.get_json() or {}
    week_start = data.get("week_start", "")
    db = get_db()
    job = db.execute("SELECT id, job_number FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        db.close()
        return {"error": "Not found"}, 404

    if week_start:
        db.execute("DELETE FROM job_assignments WHERE job_id=? AND week_start=?",
                   (job_id, week_start))
    else:
        db.execute("DELETE FROM job_assignments WHERE job_id=?", (job_id,))

    # Only reset status if no assignments remain
    remaining = db.execute(
        "SELECT COUNT(*) FROM job_assignments WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    if remaining == 0:
        db.execute("""
            UPDATE jobs SET status='Unscheduled', manual_locked=0,
                   updated_at=datetime('now') WHERE id=?
        """, (job_id,))

    db.commit()
    db.close()
    return {"ok": True}


@scheduler_bp.route("/week-operators")
@manager_required
def week_operators():
    """Return operators with their booked hours for a given week."""
    import datetime
    week_start = request.args.get("week","")
    if not week_start:
        today = datetime.date.today()
        week_start = (today - datetime.timedelta(days=today.weekday())).isoformat()

    db = get_db()
    emps = db.execute("""
        SELECT e.id, e.name, e.team, t.name grade, t.rank grade_rank
        FROM employees e JOIN tiers t ON t.id=e.tier_id
        WHERE e.active=1 ORDER BY e.team, e.name
    """).fetchall()

    result = []
    for emp in emps:
        asgns = db.execute("""
            SELECT ja.job_id, j.job_number, p.part_number, j.description,
                   j.status, j.due_date, j.quantity, j.erp_ref,
                   ja.auto_planned, COUNT(*) hours
            FROM job_assignments ja
            JOIN jobs j ON j.id=ja.job_id
            JOIN parts p ON p.id=j.part_id
            WHERE ja.employee_id=? AND ja.week_start=?
              AND j.status != 'Complete'
            GROUP BY ja.job_id, ja.auto_planned
            ORDER BY j.due_date ASC NULLS LAST
        """, (emp["id"], week_start)).fetchall()

        total_booked = sum(a["hours"] for a in asgns)
        result.append({
            **dict(emp),
            "jobs":         [dict(a) for a in asgns],
            "hours_booked": total_booked,
            "hours_free":   max(0, 40 - total_booked),
        })

    db.close()
    return {"operators": result, "week_start": week_start}
