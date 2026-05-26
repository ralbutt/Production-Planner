import datetime
from flask import Blueprint, render_template
from database import get_db
from blueprints.auth import manager_required

sales_orders_bp = Blueprint("sales_orders", __name__, url_prefix="/manager/sales-orders")

TODAY = datetime.date.today


@sales_orders_bp.route("/")
@manager_required
def index():
    db    = get_db()
    today = datetime.date.today().isoformat()

    # ── 1. Load all Sales Orders (source='sales_order') ──────
    so_rows = db.execute("""
        SELECT j.erp_ref  so_ref,
               j.job_number,
               p.part_number,
               j.description,
               j.due_date,
               j.customer,
               j.sale_value,
               j.planned_qty   so_qty_needed
        FROM jobs j
        JOIN parts p ON p.id = j.part_id
        WHERE j.source = 'sales_order'
        ORDER BY j.due_date ASC NULLS LAST, j.erp_ref
    """).fetchall()

    # Add the no-WO SO that we know about
    no_wo_so = {
        "so_ref":       "SO021696.8",
        "job_number":   "SO021696.8.SO",
        "part_number":  "WMS00000177",
        "description":  "900-10176 - Cable Assy",
        "due_date":     "2026-05-29",
        "customer":     "NXP Semiconductors",
        "sale_value":   964.80,
        "so_qty_needed":15,
    }

    sales_orders = []
    seen_so_refs = set()

    so_dicts = [dict(r) for r in so_rows]
    # Add no-WO SO if not already imported
    if not any(r.get("so_ref",r.get("erp_ref")) == "SO021696.8" for r in so_dicts):
        all_so_dicts = so_dicts + [no_wo_so]
    else:
        all_so_dicts = so_dicts

    for so in all_so_dicts:
        so_ref      = so["so_ref"]
        part_number = so["part_number"]
        due_date    = so.get("due_date")
        so_qty      = int(so.get("so_qty_needed") or 0)

        # ── 2. Find WOs covering this part number ─────────────
        wos = db.execute("""
            SELECT j.id, j.job_number, j.quantity, j.status,
                   j.due_date, j.erp_ref
            FROM jobs j
            JOIN parts p ON p.id = j.part_id
            WHERE p.part_number = ?
              AND j.source = 'import'
              AND j.status != 'Complete'
            ORDER BY j.job_number
        """, (part_number,)).fetchall()

        # ── 3. Get assignments for those WOs ──────────────────
        wo_ids = [w["id"] for w in wos]
        assignments = []
        scheduled_before_due = 0
        expected_completion  = None

        if wo_ids:
            placeholders = ",".join("?" * len(wo_ids))
            asgns = db.execute(f"""
                SELECT ja.job_id, ja.week_start,
                       e.name emp_name, e.id emp_id,
                       COUNT(*) hours
                FROM job_assignments ja
                JOIN employees e ON e.id = ja.employee_id
                WHERE ja.job_id IN ({placeholders})
                GROUP BY ja.job_id, ja.week_start, e.id
                ORDER BY ja.week_start, e.name
            """, wo_ids).fetchall()

            for a in asgns:
                ad = dict(a)
                ad["after_due"] = bool(due_date and a["week_start"] > due_date)
                assignments.append(ad)
                if not ad["after_due"]:
                    scheduled_before_due += a["hours"]
                if expected_completion is None or a["week_start"] > expected_completion:
                    expected_completion = a["week_start"]

        # ── 4. Determine track status ─────────────────────────
        has_wos    = len(wos) > 0
        is_past    = bool(due_date and due_date < today)
        all_done   = has_wos and all(w["status"] == "Complete" for w in wos)

        if not has_wos:
            track = "no_wo"
        elif all_done:
            track = "complete"
        elif not assignments:
            track = "unscheduled"
        elif is_past:
            # Due date passed — check if SO qty was met before due
            track = "late"
        elif scheduled_before_due >= so_qty:
            track = "on_track"
        else:
            # Some units scheduled but not enough before due date
            track = "at_risk"

        # Group assignments by week for the detail panel
        weeks = {}
        for a in assignments:
            w = a["week_start"]
            if w not in weeks:
                weeks[w] = []
            # Merge same emp in same week
            existing = next((x for x in weeks[w] if x["emp_id"]==a["emp_id"]), None)
            if existing:
                existing["hours"] += a["hours"]
            else:
                weeks[w].append({"emp_name":a["emp_name"],"emp_id":a["emp_id"],
                                  "hours":a["hours"],"after_due":a["after_due"]})

        wo_qty_total = sum(int(w["quantity"]) for w in wos)

        sales_orders.append({
            "so_ref":           so_ref,
            "part_number":      part_number,
            "description":      so.get("description",""),
            "due_date":         due_date,
            "customer":         so.get("customer",""),
            "sale_value":       so.get("sale_value") or 0,
            "so_qty_needed":    so_qty,
            "wo_qty_total":     wo_qty_total,
            "track_status":     track,
            "expected_week":    expected_completion,
            "scheduled_before_due": scheduled_before_due,
            "has_wo":           has_wos,
            "wos":              [dict(w) for w in wos],
            "week_groups":      dict(sorted(weeks.items())),
        })

    db.close()

    # Summary counts
    counts = {"on_track":0,"at_risk":0,"late":0,"unscheduled":0,"complete":0,"no_wo":0}
    for s in sales_orders:
        k = s["track_status"]
        if k in counts: counts[k] += 1

    return render_template("manager/sales_orders/index.html",
        sales_orders=sales_orders,
        counts=counts,
        today=today,
        active_page="sales_orders",
    )
