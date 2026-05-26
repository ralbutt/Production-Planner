from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, jsonify
)
from database import get_db
from blueprints.auth import manager_required, log_action

library_bp = Blueprint("library", __name__, url_prefix="/manager/library")


# ── helpers ────────────────────────────────────────────────

def _get_part(db, part_id):
    return db.execute("""
        SELECT p.*, t.name min_grade_name, t.rank min_grade_rank
        FROM parts p
        LEFT JOIN tiers t ON t.id = p.min_grade_id
        WHERE p.id = ?
    """, (part_id,)).fetchone()


def _build_tree(db, part_id, depth=0, max_depth=15):
    """Recursively build BOM tree. Returns list of node dicts."""
    if depth > max_depth:
        return []
    rows = db.execute("""
        SELECT bl.id bom_id, bl.quantity, bl.sequence_order, bl.run_mode,
               p.id, p.part_number, p.description, p.part_type,
               p.estimated_hours, p.actual_hours_sum, p.actual_hours_count
        FROM bom_lines bl
        JOIN parts p ON p.id = bl.child_part_id
        WHERE bl.parent_part_id = ?
        ORDER BY bl.sequence_order, p.part_number
    """, (part_id,)).fetchall()

    nodes = []
    for r in rows:
        children = _build_tree(db, r["id"], depth+1, max_depth)
        nodes.append({
            "bom_id":       r["bom_id"],
            "id":           r["id"],
            "part_number":  r["part_number"],
            "description":  r["description"],
            "part_type":    r["part_type"],
            "quantity":     r["quantity"],
            "sequence_order": r["sequence_order"],
            "run_mode":     r["run_mode"],
            "estimated_hours": r["estimated_hours"],
            "actual_hours_sum": r["actual_hours_sum"],
            "actual_hours_count": r["actual_hours_count"],
            "avg_hours":    round(r["actual_hours_sum"] / r["actual_hours_count"], 2)
                            if r["actual_hours_count"] else r["estimated_hours"],
            "depth":        depth,
            "children":     children,
        })
    return nodes


def _all_parts_flat(db):
    return db.execute(
        "SELECT id, part_number, description, part_type FROM parts ORDER BY part_number"
    ).fetchall()


# ── list ───────────────────────────────────────────────────

@library_bp.route("/")
@manager_required
def list_parts():
    db = get_db()
    parts = db.execute("""
        SELECT p.*,
               p.actual_hours_count,
               p.actual_hours_sum,
               t.name min_grade_name,
               t.rank min_grade_rank,
               (SELECT COUNT(*) FROM bom_lines WHERE parent_part_id=p.id) child_count
        FROM parts p
        LEFT JOIN tiers t ON t.id = p.min_grade_id
        ORDER BY p.part_number
    """).fetchall()
    db.close()
    return render_template("manager/library/list.html",
                           parts=parts, active_page="library")


# ── add part ───────────────────────────────────────────────

@library_bp.route("/add", methods=["GET", "POST"])
@manager_required
def add_part():
    db = get_db()
    all_parts = _all_parts_flat(db)

    if request.method == "POST":
        part_number   = request.form.get("part_number", "").strip()
        description   = request.form.get("description", "").strip()
        part_type     = request.form.get("part_type", "single")
        estimated_hours = request.form.get("estimated_hours", "").strip()

        errors = []
        if not part_number:
            errors.append("Part number is required.")
        else:
            existing = db.execute(
                "SELECT id FROM parts WHERE part_number=? COLLATE NOCASE",
                (part_number,)
            ).fetchone()
            if existing:
                errors.append(f"Part number '{part_number}' already exists.")

        min_grade_id = request.form.get('min_grade_id') or None
        est_h = None
        if estimated_hours:
            try:
                est_h = float(estimated_hours)
            except ValueError:
                errors.append("Estimated hours must be a number.")
        else:
            est_h = 1.0  # default 1 hour per unit

        if not errors:
            db.execute("""
                INSERT INTO parts(part_number, description, part_type, estimated_hours, min_grade_id)
                VALUES(?,?,?,?,?)
            """, (part_number, description or None, part_type, est_h, min_grade_id))
            new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.commit()
            log_action(session["user_id"], "add_part",
                       f"Added part {part_number} (id={new_id})")
            db.close()
            flash(f"Part {part_number} added.", "success")
            return redirect(url_for("library.detail", part_id=new_id))

        db.close()
        tiers = db.execute('SELECT * FROM tiers ORDER BY rank').fetchall()
        return render_template("manager/library/form.html",
                               mode="add", errors=errors, tiers=tiers,
                               all_parts=all_parts, form=request.form,
                               active_page="library")

    tiers = db.execute('SELECT * FROM tiers ORDER BY rank').fetchall()
    db.close()
    return render_template("manager/library/form.html",
                           mode="add", errors=[], all_parts=all_parts,
                           tiers=tiers, form={}, active_page="library")


# ── edit part ──────────────────────────────────────────────

@library_bp.route("/<int:part_id>/edit", methods=["GET", "POST"])
@manager_required
def edit_part(part_id):
    db   = get_db()
    part = _get_part(db, part_id)
    if not part:
        db.close()
        flash("Part not found.", "error")
        return redirect(url_for("library.list_parts"))

    if request.method == "POST":
        description     = request.form.get("description", "").strip()
        estimated_hours = request.form.get("estimated_hours", "").strip()
        min_grade_id    = request.form.get("min_grade_id") or None

        est_h = None
        errors = []
        if estimated_hours:
            try:
                est_h = float(estimated_hours)
            except ValueError:
                errors.append("Estimated hours must be a number.")

        if not errors:
            db.execute("""
                UPDATE parts SET description=?, estimated_hours=?, min_grade_id=?,
                updated_at=datetime('now') WHERE id=?
            """, (description or None, est_h, min_grade_id, part_id))
            db.commit()
            log_action(session["user_id"], "edit_part", f"Edited part id={part_id}")
            db.close()
            flash("Part updated.", "success")
            return redirect(url_for("library.detail", part_id=part_id))

        tiers = db.execute('SELECT * FROM tiers ORDER BY rank').fetchall()
        db.close()
        return render_template("manager/library/form.html",
                               mode="edit", part=part, errors=errors, tiers=tiers,
                               form=request.form, active_page="library")

    tiers = db.execute('SELECT * FROM tiers ORDER BY rank').fetchall()
    db.close()
    return render_template("manager/library/form.html",
                           mode="edit", part=part, errors=[], tiers=tiers,
                           form=part, active_page="library")


# ── detail + BOM tree ──────────────────────────────────────

@library_bp.route("/<int:part_id>")
@manager_required
def detail(part_id):
    db   = get_db()
    part = _get_part(db, part_id)
    if not part:
        db.close()
        flash("Part not found.", "error")
        return redirect(url_for("library.list_parts"))

    tree      = _build_tree(db, part_id)
    all_parts = _all_parts_flat(db)
    db.close()
    return render_template("manager/library/detail.html",
                           part=part, tree=tree, all_parts=all_parts,
                           active_page="library")


# ── add BOM child ──────────────────────────────────────────

@library_bp.route("/<int:part_id>/add-child", methods=["POST"])
@manager_required
def add_child(part_id):
    db = get_db()
    child_pn  = request.form.get("child_pn", "").strip()
    run_mode  = request.form.get("run_mode", "parallel")
    quantity  = request.form.get("quantity", "1").strip()

    errors = []
    if not child_pn:
        errors.append("Part number is required.")

    # look up child
    child = db.execute(
        "SELECT * FROM parts WHERE part_number=? COLLATE NOCASE", (child_pn,)
    ).fetchone()

    if not child:
        db.close()
        flash(f"Part '{child_pn}' not found in library. Add it first.", "error")
        return redirect(url_for("library.detail", part_id=part_id))

    if child["id"] == part_id:
        db.close()
        flash("A part cannot be a child of itself.", "error")
        return redirect(url_for("library.detail", part_id=part_id))

    # get next sequence order
    max_seq = db.execute(
        "SELECT COALESCE(MAX(sequence_order),0) FROM bom_lines WHERE parent_part_id=?",
        (part_id,)
    ).fetchone()[0]

    try:
        qty = float(quantity) if quantity else 1.0
    except ValueError:
        qty = 1.0

    try:
        db.execute("""
            INSERT INTO bom_lines(parent_part_id, child_part_id, quantity,
                                  sequence_order, run_mode)
            VALUES(?,?,?,?,?)
        """, (part_id, child["id"], qty, max_seq + 1, run_mode))
        # mark parent as assembly
        db.execute(
            "UPDATE parts SET part_type='assembly', updated_at=datetime('now') WHERE id=?",
            (part_id,)
        )
        db.commit()
        log_action(session["user_id"], "add_bom_child",
                   f"Added child {child_pn} to part id={part_id}")
        flash(f"{child_pn} added to BOM.", "success")
    except Exception as e:
        flash(f"Could not add child: {e}", "error")

    db.close()
    return redirect(url_for("library.detail", part_id=part_id))


# ── remove BOM child ───────────────────────────────────────

@library_bp.route("/<int:part_id>/remove-child/<int:bom_id>", methods=["POST"])
@manager_required
def remove_child(part_id, bom_id):
    db = get_db()
    db.execute("DELETE FROM bom_lines WHERE id=? AND parent_part_id=?",
               (bom_id, part_id))
    # if no children left, revert to single
    remaining = db.execute(
        "SELECT COUNT(*) FROM bom_lines WHERE parent_part_id=?", (part_id,)
    ).fetchone()[0]
    if remaining == 0:
        db.execute("UPDATE parts SET part_type='single' WHERE id=?", (part_id,))
    db.commit()
    db.close()
    flash("Child removed from BOM.", "success")
    return redirect(url_for("library.detail", part_id=part_id))


# ── update timing (from job) ───────────────────────────────

@library_bp.route("/<int:part_id>/update-timing", methods=["POST"])
@manager_required
def update_timing(part_id):
    db    = get_db()
    hours = request.form.get("actual_hours", "").strip()
    source = request.form.get("source", "manual")   # job number or 'manual'

    try:
        h = float(hours)
        if h <= 0:
            raise ValueError
    except ValueError:
        flash("Please enter a valid number of hours.", "error")
        db.close()
        return redirect(request.referrer or url_for("library.detail", part_id=part_id))

    db.execute("""
        UPDATE parts
        SET actual_hours_sum   = actual_hours_sum + ?,
            actual_hours_count = actual_hours_count + 1,
            updated_at = datetime('now')
        WHERE id = ?
    """, (h, part_id))
    db.commit()
    log_action(session["user_id"], "update_timing",
               f"Added {h}h actual to part id={part_id} (source={source})")
    db.close()
    flash(f"Timing updated: {h}h added.", "success")
    return redirect(request.referrer or url_for("library.detail", part_id=part_id))


# ── API: search parts (for add-child autocomplete) ─────────

@library_bp.route("/api/search")
@manager_required
def api_search():
    q  = request.args.get("q", "").strip()
    db = get_db()
    rows = db.execute("""
        SELECT id, part_number, description, part_type
        FROM parts
        WHERE part_number LIKE ? OR description LIKE ?
        ORDER BY part_number LIMIT 20
    """, (f"%{q}%", f"%{q}%")).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


# ── inline hours update ────────────────────────────────────

@library_bp.route("/hours", methods=["POST"])
@manager_required
def update_hours():
    """AJAX/form endpoint for inline hours editing on the list page."""
    part_id = request.form.get("part_id")
    hours   = request.form.get("hours", "").strip()
    try:
        h = float(hours)
        if h < 0: raise ValueError
    except (ValueError, TypeError):
        flash("Invalid hours value.", "error")
        return redirect(url_for("library.list_parts"))

    db = get_db()
    db.execute(
        "UPDATE parts SET estimated_hours=?, updated_at=datetime('now') WHERE id=?",
        (h, part_id)
    )
    db.commit()
    pn = db.execute("SELECT part_number FROM parts WHERE id=?", (part_id,)).fetchone()
    db.close()
    log_action(session["user_id"], "update_hours",
               f"Set {pn['part_number']} estimated_hours={h}")
    flash(f"Hours updated to {h}h.", "success")
    return redirect(url_for("library.list_parts"))