from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, jsonify
)
from database import get_db
from blueprints.auth import manager_required, log_action
from bcrypt_compat import hashpw, gensalt

employees_bp = Blueprint("employees", __name__, url_prefix="/manager/employees")

# Teams — add new teams here as the business grows
TEAMS = ["General", "Davenham"]
DAVENHAM_CUSTOMERS = ["davenham switchgear", "davenham"]


# ── helpers ────────────────────────────────────────────────────────────────

def _all_skills(db):
    return db.execute("SELECT * FROM skill_types ORDER BY name").fetchall()

def _all_tiers(db):
    return db.execute("SELECT * FROM tiers ORDER BY rank").fetchall()

def _get_employee(db, emp_id):
    return db.execute("""
        SELECT e.*, t.name tier_name, t.rank tier_rank
        FROM employees e
        JOIN tiers t ON t.id = e.tier_id
        WHERE e.id = ?
    """, (emp_id,)).fetchone()

def _emp_skills(db, emp_id):
    rows = db.execute("""
        SELECT st.id, st.name
        FROM employee_skills es
        JOIN skill_types st ON st.id = es.skill_type_id
        WHERE es.employee_id = ?
        ORDER BY st.name
    """, (emp_id,)).fetchall()
    return rows

def _emp_availability(db, emp_id):
    return db.execute("""
        SELECT * FROM employee_availability
        WHERE employee_id = ?
        ORDER BY date
    """, (emp_id,)).fetchall()

def _linked_user(db, emp_id):
    return db.execute(
        "SELECT id, username, role, active FROM users WHERE employee_id = ?",
        (emp_id,)
    ).fetchone()


# ── list ───────────────────────────────────────────────────────────────────

@employees_bp.route("/")
@manager_required
def list_employees():
    db = get_db()
    employees = db.execute("""
        SELECT e.*, t.name tier_name, t.rank tier_rank,
               GROUP_CONCAT(st.name, '|') skills
        FROM employees e
        JOIN tiers t ON t.id = e.tier_id
        LEFT JOIN employee_skills es ON es.employee_id = e.id
        LEFT JOIN skill_types st ON st.id = es.skill_type_id
        GROUP BY e.id
        ORDER BY e.team, e.active DESC, t.rank DESC, e.name
    """).fetchall()
    db.close()
    return render_template("manager/employees/list.html",
                           employees=employees,
                           active_page="employees")


# ── add ────────────────────────────────────────────────────────────────────

@employees_bp.route("/add", methods=["GET", "POST"])
@manager_required
def add_employee():
    db = get_db()
    skills = _all_skills(db)
    tiers  = _all_tiers(db)

    if request.method == "POST":
        name       = request.form.get("name", "").strip()
        department = request.form.get("department", "").strip()
        tier_id    = request.form.get("tier_id", "")
        team       = request.form.get("team", "General")
        skill_ids  = request.form.getlist("skill_ids")

        errors = []
        if not name:       errors.append("Name is required.")
        if not tier_id:    errors.append("Tier is required.")
        if team not in TEAMS: team = "General"

        if not errors:
            db.execute(
                "INSERT INTO employees(name, department, tier_id, team) VALUES(?,?,?,?)",
                (name, department, tier_id, team)
            )
            emp_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for sid in skill_ids:
                db.execute(
                    "INSERT OR IGNORE INTO employee_skills(employee_id, skill_type_id) VALUES(?,?)",
                    (emp_id, sid)
                )
            db.commit()
            log_action(session["user_id"], "add_employee", f"Added employee {name} (id={emp_id})")
            db.close()
            flash(f"{name} added successfully.", "success")
            return redirect(url_for("employees.list_employees"))

        db.close()
        return render_template("manager/employees/form.html",
                               mode="add", errors=errors,
                               skills=skills, tiers=tiers,
                               form=request.form,
                               active_page="employees")

    db.close()
    return render_template("manager/employees/form.html",
                           mode="add", errors=[],
                           skills=skills, tiers=tiers, teams=TEAMS,
                           form={}, active_page="employees")


# ── edit ───────────────────────────────────────────────────────────────────

@employees_bp.route("/<int:emp_id>/edit", methods=["GET", "POST"])
@manager_required
def edit_employee(emp_id):
    db = get_db()
    emp    = _get_employee(db, emp_id)
    skills = _all_skills(db)
    tiers  = _all_tiers(db)

    if not emp:
        db.close()
        flash("Employee not found.", "error")
        return redirect(url_for("employees.list_employees"))

    current_skill_ids = [str(r["id"]) for r in _emp_skills(db, emp_id)]

    if request.method == "POST":
        name       = request.form.get("name", "").strip()
        department = request.form.get("department", "").strip()
        tier_id    = request.form.get("tier_id", "")
        team       = request.form.get("team", "General")
        skill_ids  = request.form.getlist("skill_ids")
        if team not in TEAMS: team = "General"

        errors = []
        if not name:      errors.append("Name is required.")
        if not tier_id:   errors.append("Tier is required.")

        if not errors:
            db.execute(
                "UPDATE employees SET name=?, department=?, tier_id=?, team=? WHERE id=?",
                (name, department, tier_id, team, emp_id))
            # replace skills
            db.execute("DELETE FROM employee_skills WHERE employee_id=?", (emp_id,))
            for sid in skill_ids:
                db.execute(
                    "INSERT OR IGNORE INTO employee_skills(employee_id, skill_type_id) VALUES(?,?)",
                    (emp_id, sid)
                )
            db.commit()
            log_action(session["user_id"], "edit_employee", f"Edited employee id={emp_id}")
            db.close()
            flash(f"{name} updated.", "success")
            return redirect(url_for("employees.detail", emp_id=emp_id))

        db.close()
        return render_template("manager/employees/form.html",
                               mode="edit", emp=emp, errors=errors,
                               skills=skills, tiers=tiers, teams=TEAMS,
                               current_skill_ids=request.form.getlist("skill_ids"),
                               form=request.form, active_page="employees")

    db.close()
    return render_template("manager/employees/form.html",
                           mode="edit", emp=emp, errors=[],
                           skills=skills, tiers=tiers, teams=TEAMS,
                           current_skill_ids=current_skill_ids,
                           form=emp, active_page="employees")


# ── detail ─────────────────────────────────────────────────────────────────

@employees_bp.route("/<int:emp_id>")
@manager_required
def detail(emp_id):
    db  = get_db()
    emp = _get_employee(db, emp_id)
    if not emp:
        db.close()
        flash("Employee not found.", "error")
        return redirect(url_for("employees.list_employees"))

    emp_skills       = _emp_skills(db, emp_id)
    availability     = _emp_availability(db, emp_id)
    linked_user      = _linked_user(db, emp_id)
    all_skills       = _all_skills(db)
    unlinked_users   = db.execute(
        "SELECT id, username, role FROM users WHERE employee_id IS NULL AND active=1 ORDER BY username"
    ).fetchall()

    # Products
    specialisms = db.execute("""
        SELECT es.id, es.level, p.id part_id, p.part_number, p.description
        FROM employee_specialisms es
        JOIN parts p ON p.id = es.part_id
        WHERE es.employee_id = ?
        ORDER BY es.level DESC, p.part_number
    """, (emp_id,)).fetchall()

    # Parts not already assigned as products
    already_ids = [s["part_id"] for s in specialisms]
    all_parts = db.execute(
        "SELECT id, part_number, description FROM parts ORDER BY part_number"
    ).fetchall()
    available_parts = [p for p in all_parts if p["id"] not in already_ids]

    db.close()

    return render_template("manager/employees/detail.html",
                           emp=emp, emp_skills=emp_skills,
                           availability=availability,
                           linked_user=linked_user,
                           all_skills=all_skills,
                           unlinked_users=unlinked_users,
                           specialisms=specialisms,
                           available_parts=available_parts,
                           active_page="employees")


# ── toggle active ──────────────────────────────────────────────────────────

@employees_bp.route("/<int:emp_id>/toggle", methods=["POST"])
@manager_required
def toggle_active(emp_id):
    db  = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if emp:
        new_state = 0 if emp["active"] else 1
        db.execute("UPDATE employees SET active=? WHERE id=?", (new_state, emp_id))
        db.commit()
        action = "activated" if new_state else "deactivated"
        log_action(session["user_id"], f"employee_{action}", f"Employee id={emp_id}")
        flash(f"{emp['name']} {action}.", "success")
    db.close()
    return redirect(url_for("employees.list_employees"))


# ── availability ───────────────────────────────────────────────────────────

@employees_bp.route("/<int:emp_id>/availability/add", methods=["POST"])
@manager_required
def add_availability(emp_id):
    db   = get_db()
    emp  = db.execute("SELECT name FROM employees WHERE id=?", (emp_id,)).fetchone()
    date   = request.form.get("date", "").strip()
    reason = request.form.get("reason", "").strip()

    if not date:
        flash("Date is required.", "error")
        db.close()
        return redirect(url_for("employees.detail", emp_id=emp_id))

    try:
        db.execute(
            "INSERT OR REPLACE INTO employee_availability(employee_id, date, reason) VALUES(?,?,?)",
            (emp_id, date, reason or None)
        )
        db.commit()
        log_action(session["user_id"], "add_absence",
                   f"Marked {emp['name']} unavailable on {date}")
        flash(f"Absence recorded for {date}.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    db.close()
    return redirect(url_for("employees.detail", emp_id=emp_id))


@employees_bp.route("/<int:emp_id>/availability/<int:avail_id>/delete", methods=["POST"])
@manager_required
def delete_availability(emp_id, avail_id):
    db = get_db()
    db.execute(
        "DELETE FROM employee_availability WHERE id=? AND employee_id=?",
        (avail_id, emp_id)
    )
    db.commit()
    db.close()
    flash("Absence removed.", "success")
    return redirect(url_for("employees.detail", emp_id=emp_id))


# ── link user account ──────────────────────────────────────────────────────

@employees_bp.route("/<int:emp_id>/link-user", methods=["POST"])
@manager_required
def link_user(emp_id):
    db      = get_db()
    user_id = request.form.get("user_id", "")
    if user_id:
        # unlink any existing assignment of this user
        db.execute("UPDATE users SET employee_id=NULL WHERE employee_id=?", (emp_id,))
        db.execute("UPDATE users SET employee_id=? WHERE id=?", (emp_id, user_id))
        db.commit()
        flash("User account linked.", "success")
    db.close()
    return redirect(url_for("employees.detail", emp_id=emp_id))


@employees_bp.route("/<int:emp_id>/unlink-user", methods=["POST"])
@manager_required
def unlink_user(emp_id):
    db = get_db()
    db.execute("UPDATE users SET employee_id=NULL WHERE employee_id=?", (emp_id,))
    db.commit()
    db.close()
    flash("User account unlinked.", "success")
    return redirect(url_for("employees.detail", emp_id=emp_id))


# ── products ────────────────────────────────────────────────────────────

@employees_bp.route("/<int:emp_id>/products/add", methods=["POST"])
@manager_required
def add_specialism(emp_id):
    db      = get_db()
    part_id = request.form.get("part_id", "").strip()
    level   = request.form.get("level", "primary")

    if level not in ("primary", "secondary"):
        level = "primary"

    if not part_id:
        flash("Select a part number.", "error")
        db.close()
        return redirect(url_for("employees.detail", emp_id=emp_id))

    part = db.execute("SELECT part_number FROM parts WHERE id=?", (part_id,)).fetchone()
    if not part:
        flash("Part not found.", "error")
        db.close()
        return redirect(url_for("employees.detail", emp_id=emp_id))

    try:
        db.execute(
            "INSERT OR REPLACE INTO employee_specialisms(employee_id, part_id, level) VALUES(?,?,?)",
            (emp_id, part_id, level)
        )
        db.commit()
        log_action(session["user_id"], "add_product",
                   f"Employee {emp_id}: {level} product in {part['part_number']}")
        flash(f"{level.capitalize()} product in {part['part_number']} added.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")

    db.close()
    return redirect(url_for("employees.detail", emp_id=emp_id))


@employees_bp.route("/<int:emp_id>/products/<int:spec_id>/delete", methods=["POST"])
@manager_required
def delete_specialism(emp_id, spec_id):
    db = get_db()
    db.execute(
        "DELETE FROM employee_specialisms WHERE id=? AND employee_id=?",
        (spec_id, emp_id)
    )
    db.commit()
    db.close()
    flash("Product removed.", "success")
    return redirect(url_for("employees.detail", emp_id=emp_id))


@employees_bp.route("/<int:emp_id>/products/<int:spec_id>/toggle", methods=["POST"])
@manager_required
def toggle_specialism_level(emp_id, spec_id):
    db   = get_db()
    spec = db.execute(
        "SELECT * FROM employee_specialisms WHERE id=? AND employee_id=?",
        (spec_id, emp_id)
    ).fetchone()
    if spec:
        new_level = "secondary" if spec["level"] == "primary" else "primary"
        db.execute(
            "UPDATE employee_specialisms SET level=? WHERE id=?",
            (new_level, spec_id)
        )
        db.commit()
        flash(f"Changed to {new_level} product.", "success")
    db.close()
    return redirect(url_for("employees.detail", emp_id=emp_id))


# ── API: who specialises in a given part ──────────────────────────────────

@employees_bp.route("/api/specialists/<int:part_id>")
@manager_required
def api_specialists(part_id):
    """Return employees with primary/secondary product in this part."""
    from flask import jsonify
    db = get_db()
    rows = db.execute("""
        SELECT e.id, e.name, e.team, t.name grade, es.level
        FROM employee_specialisms es
        JOIN employees e ON e.id = es.employee_id
        JOIN tiers t      ON t.id = e.tier_id
        WHERE es.part_id = ? AND e.active = 1
        ORDER BY es.level DESC, t.rank DESC, e.name
    """, (part_id,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])
