from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, jsonify
)
from database import get_db
from blueprints.auth import manager_required, log_action
import datetime, os, tempfile

jobs_bp = Blueprint("jobs", __name__, url_prefix="/manager/jobs")

EXCEL_DATE_BASE = datetime.date(1899, 12, 30)

# Team auto-detection — customer name fragments mapped to team
CUSTOMER_TEAM_MAP = {
    "davenham": "Davenham",
}

def _detect_team(customer):
    """Return team name based on customer, defaulting to General."""
    if not customer:
        return "General"
    cl = customer.lower()
    for fragment, team in CUSTOMER_TEAM_MAP.items():
        if fragment in cl:
            return team
    return "General"

def excel_serial_to_date(serial):
    """Convert Excel date serial to ISO string."""
    try:
        return (EXCEL_DATE_BASE + datetime.timedelta(days=int(serial))).isoformat()
    except Exception:
        return None

def _job_status_class(status):
    return {
        "Unscheduled": "neutral",
        "Scheduled":   "green",
        "In Progress": "amber",
        "Complete":    "green",
        "On Hold":     "red",
    }.get(status, "neutral")


# ── list ───────────────────────────────────────────────────

@jobs_bp.route("/")
@manager_required
def list_jobs():
    db = get_db()
    status_filter = request.args.get("status", "")
    customer_filter = request.args.get("customer", "")

    query = """
        SELECT j.*,
               p.part_number, p.description part_desc,
               p.part_type
        FROM jobs j
        LEFT JOIN parts p ON p.id = j.part_id
        WHERE 1=1
    """
    params = []
    if status_filter:
        query += " AND j.status = ?"
        params.append(status_filter)
    if customer_filter:
        query += " AND j.customer LIKE ?"
        params.append(f"%{customer_filter}%")
    query += " ORDER BY j.due_date ASC, j.job_number"

    jobs = db.execute(query, params).fetchall()
    customers = db.execute(
        "SELECT DISTINCT customer FROM jobs WHERE customer IS NOT NULL ORDER BY customer"
    ).fetchall()
    db.close()
    return render_template("manager/jobs/list.html",
                           jobs=jobs,
                           customers=customers,
                           status_filter=status_filter,
                           customer_filter=customer_filter,
                           active_page="jobs")


# ── add manual job ─────────────────────────────────────────

@jobs_bp.route("/add", methods=["GET", "POST"])
@manager_required
def add_job():
    db = get_db()
    parts = db.execute(
        "SELECT id, part_number, description FROM parts ORDER BY part_number"
    ).fetchall()

    if request.method == "POST":
        job_number  = request.form.get("job_number", "").strip()
        part_id     = request.form.get("part_id") or None
        description = request.form.get("description", "").strip()
        customer    = request.form.get("customer", "").strip()
        quantity    = request.form.get("quantity", "1").strip()
        due_date    = request.form.get("due_date", "").strip()
        sale_value  = request.form.get("sale_value", "").strip()
        eas_number  = request.form.get("eas_number", "").strip()
        customers_po = request.form.get("customers_po", "").strip()
        for_stock   = 1 if request.form.get("for_stock") else 0

        errors = []
        if not job_number:
            errors.append("Job number is required.")
        else:
            exists = db.execute(
                "SELECT id FROM jobs WHERE job_number=?", (job_number,)
            ).fetchone()
            if exists:
                errors.append(f"Job number '{job_number}' already exists.")

        try:
            qty = float(quantity) if quantity else 1
        except ValueError:
            qty = 1

        # Auto-detect team from customer, allow manual override
        team = request.form.get("team", "").strip() or _detect_team(customer or "")

        if not errors:
            db.execute("""
                INSERT INTO jobs(job_number, part_id, description, customer,
                                 quantity, due_date, sale_value, eas_number,
                                 customers_po, for_stock, team, source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'manual')
            """, (
                job_number,
                part_id or None,
                description or None,
                customer or None,
                qty,
                due_date or None,
                float(sale_value) if sale_value else None,
                eas_number or None,
                customers_po or None,
                for_stock,
                team,
            ))
            db.commit()
            log_action(session["user_id"], "add_job", f"Manual job {job_number}")
            db.close()
            flash(f"Job {job_number} created.", "success")
            return redirect(url_for("jobs.list_jobs"))

        db.close()
        return render_template("manager/jobs/form.html",
                               mode="add", errors=errors,
                               parts=parts, form=request.form,
                               active_page="jobs")

    db.close()
    return render_template("manager/jobs/form.html",
                           mode="add", errors=[], parts=parts,
                           form={}, active_page="jobs")


# ── job detail ─────────────────────────────────────────────

@jobs_bp.route("/<int:job_id>")
@manager_required
def detail(job_id):
    db  = get_db()
    job = db.execute("""
        SELECT j.*, p.part_number, p.description part_desc, p.part_type
        FROM jobs j
        LEFT JOIN parts p ON p.id = j.part_id
        WHERE j.id = ?
    """, (job_id,)).fetchone()
    if not job:
        db.close()
        flash("Job not found.", "error")
        return redirect(url_for("jobs.list_jobs"))
    db.close()
    return render_template("manager/jobs/detail.html",
                           job=job, active_page="jobs")


# ── update status ──────────────────────────────────────────

@jobs_bp.route("/<int:job_id>/status", methods=["POST"])
@manager_required
def update_status(job_id):
    db     = get_db()
    status = request.form.get("status", "")
    valid  = ["Unscheduled","Scheduled","In Progress","Complete","On Hold"]
    if status in valid:
        db.execute(
            "UPDATE jobs SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, job_id)
        )
        db.commit()
        log_action(session["user_id"], "update_job_status",
                   f"Job {job_id} → {status}")
        flash(f"Status updated to {status}.", "success")
    db.close()
    return redirect(request.referrer or url_for("jobs.list_jobs"))


# ── toggle waiting on parts ────────────────────────────────

@jobs_bp.route("/<int:job_id>/toggle-parts", methods=["POST"])
@manager_required
def toggle_waiting_parts(job_id):
    db  = get_db()
    job = db.execute("SELECT waiting_parts FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job:
        new = 0 if job["waiting_parts"] else 1
        db.execute(
            "UPDATE jobs SET waiting_parts=?, updated_at=datetime('now') WHERE id=?",
            (new, job_id)
        )
        db.commit()
        flash("Waiting on Parts " + ("flagged." if new else "cleared."), "success")
    db.close()
    return redirect(request.referrer or url_for("jobs.list_jobs"))


# ── ERP import ─────────────────────────────────────────────

@jobs_bp.route("/import", methods=["GET", "POST"])
@manager_required
def erp_import():
    if request.method == "GET":
        return render_template("manager/jobs/import.html", active_page="jobs")

    # POST — file uploaded
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Please select a file.", "error")
        return redirect(url_for("jobs.erp_import"))

    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("xlsx", "xls", "csv"):
        flash("Only .xlsx, .xls, or .csv files are supported.", "error")
        return redirect(url_for("jobs.erp_import"))

    # save temp
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix="."+ext)
    f.save(tmp.name)
    tmp.close()

    try:
        import openpyxl, csv
        rows = []
        headers = []

        if ext in ("xlsx", "xls"):
            wb = openpyxl.load_workbook(tmp.name, read_only=True, data_only=True)
            ws = wb.active
            first = True
            for row in ws.iter_rows(values_only=True):
                if all(v is None for v in row):
                    continue
                if first:
                    headers = [str(c).strip() if c is not None else "" for c in row]
                    first = False
                else:
                    rows.append(dict(zip(headers, row)))
        else:
            with open(tmp.name, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                headers = reader.fieldnames or []
                rows = list(reader)

        os.unlink(tmp.name)

        # --- column mapping for Emax format ---
        # Try to auto-detect Emax columns, fall back to generic
        col_map = _detect_emax_columns(headers)

        db = get_db()
        matched = []
        unmatched = []
        skipped = []

        for row in rows:
            item_code   = _get_col(row, col_map, "item_code")
            sales_order = _get_col(row, col_map, "sales_order")
            eas_number  = _get_col(row, col_map, "eas_number")
            customer    = _get_col(row, col_map, "customer")
            description = _get_col(row, col_map, "description")
            qty_raw     = _get_col(row, col_map, "qty_outstanding") or _get_col(row, col_map, "qty")
            date_raw    = _get_col(row, col_map, "due_date")
            value_raw   = _get_col(row, col_map, "value")
            customers_po = _get_col(row, col_map, "customers_po")

            if not sales_order:
                skipped.append({"reason": "No job number", "row": row})
                continue

            # skip silently if already imported — purchaser re-imports frequently
            exists = db.execute(
                "SELECT id FROM jobs WHERE job_number=?", (str(sales_order).strip(),)
            ).fetchone()
            if exists:
                continue

            # parse qty
            try:
                qty = float(qty_raw) if qty_raw not in (None, "") else 1
            except (ValueError, TypeError):
                qty = 1

            # parse date (Excel serial or string)
            due_date = None
            if date_raw not in (None, ""):
                try:
                    due_date = excel_serial_to_date(date_raw)
                except Exception:
                    try:
                        due_date = str(date_raw).strip()
                    except Exception:
                        due_date = None

            # parse value
            try:
                value = float(value_raw) if value_raw not in (None, "") else None
            except (ValueError, TypeError):
                value = None

            # match part in library
            part = None
            if item_code:
                part = db.execute(
                    "SELECT * FROM parts WHERE part_number=? COLLATE NOCASE",
                    (str(item_code).strip(),)
                ).fetchone()

            # Works orders: "Stock Build" in project field means for_stock
            file_type = col_map.get("_file_type", "sales_delivery")
            eas_str = str(eas_number).strip() if eas_number else ""
            for_stock = 1 if (file_type == "works_orders" and eas_str.lower() in ("stock build", "stock")) else 0
            # For works orders the eas is in Project; skip if it is literally "Stock Build"
            eas_clean = None if for_stock else (eas_str or None)

            # sales_order_ref: for works orders, the Sales Order column is the SO reference
            so_ref = _get_col(row, col_map, "sales_order_ref")
            so_ref_str = str(so_ref).strip() if so_ref else None

            customer_str = str(customer).strip() if customer else None
            job_data = {
                "job_number":   str(sales_order).strip(),
                "part_id":      part["id"] if part else None,
                "description":  str(description).strip() if description else None,
                "customer":     customer_str,
                "quantity":     qty,
                "due_date":     due_date,
                "sale_value":   value,
                "eas_number":   eas_clean,
                "customers_po": str(customers_po).strip() if customers_po else None,
                "erp_ref":      so_ref_str,
                "for_stock":    for_stock,
                "team":         _detect_team(customer_str),
                "item_code":    str(item_code).strip() if item_code else None,
                "part_matched": part is not None,
            }

            if part:
                matched.append(job_data)
            else:
                unmatched.append(job_data)

        # create matched jobs immediately
        created = 0
        for j in matched:
            db.execute("""
                INSERT INTO jobs(job_number, part_id, description, customer,
                                 quantity, due_date, sale_value, eas_number,
                                 customers_po, for_stock, erp_ref, team, source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'erp')
            """, (j["job_number"], j["part_id"], j["description"],
                  j["customer"], j["quantity"], j["due_date"],
                  j["sale_value"], j["eas_number"], j["customers_po"],
                  j.get("for_stock", 0), j.get("erp_ref"),
                  j.get("team", "General")))
            created += 1

        db.commit()
        log_action(session["user_id"], "erp_import",
                   f"Imported {created} jobs, {len(unmatched)} unmatched, {len(skipped)} skipped")
        db.close()

        file_type = col_map.get("_file_type", "sales_delivery")
        return render_template("manager/jobs/import_result.html",
                               matched=matched,
                               unmatched=unmatched,
                               skipped=skipped,
                               created=created,
                               file_type=file_type,
                               active_page="jobs")

    except Exception as e:
        try: os.unlink(tmp.name)
        except: pass
        flash(f"Import error: {e}", "error")
        return redirect(url_for("jobs.erp_import"))


def _detect_file_type(headers):
    """Detect whether this is a Sales Delivery Lines or Works Orders export."""
    h_lower = [h.lower().strip() for h in headers]
    if "transaction no" in h_lower:
        return "works_orders"
    return "sales_delivery"


def _detect_emax_columns(headers):
    """Map logical field names to actual column headers using known Emax names."""
    h_lower = {h.lower().strip(): h for h in headers}
    def find(*candidates):
        for c in candidates:
            if c.lower() in h_lower:
                return h_lower[c.lower()]
        return None

    file_type = _detect_file_type(headers)

    if file_type == "works_orders":
        return {
            "_file_type":    "works_orders",
            "sales_order":   find("Transaction No"),
            "eas_number":    find("Project"),
            "customer":      find("CustomerSupplier", "Customer"),
            "item_code":     find("Item", "ItemCode", "Part Number"),
            "description":   find("Description1", "Description"),
            "qty":           find("Outstanding", "Qty"),
            "qty_outstanding": find("Outstanding", "Qty"),
            "due_date":      find("Delivery Date", "Date Required"),
            "value":         find("Planned Material Cost", "Material Cost"),
            "customers_po":  find("Sales Order"),
            "sales_order_ref": find("Sales Order"),
        }
    else:
        return {
            "_file_type":    "sales_delivery",
            "sales_order":   find("Sales Order", "SalesOrder", "Order", "Job Number", "Works Order"),
            "eas_number":    find("Project", "EAS", "EAS Number", "Project Number"),
            "customer":      find("Customer", "Client", "Account"),
            "item_code":     find("ItemCode", "Item Code", "Part Number", "PartNo", "Part No"),
            "description":   find("Item Description", "Description", "ItemDescription"),
            "qty":           find("Qty", "Quantity", "Ordered Qty"),
            "qty_outstanding": find("Qty Outstanding", "QtyOutstanding", "Outstanding"),
            "due_date":      find("Date Required", "Due Date", "DueDate", "Delivery Date"),
            "value":         find("Sub Total Base Currency", "Total", "Value", "SubTotal", "Sale Value"),
            "customers_po":  find("Customers PO", "Customer PO", "CustomersPO", "PO Number", "PO"),
        }

def _get_col(row, col_map, key):
    col = col_map.get(key)
    if col and col in row:
        v = row[col]
        return v if v not in (None, "") else None
    return None



# ── skip this week ─────────────────────────────────────────────────────────

@jobs_bp.route("/<int:job_id>/skip-week", methods=["POST"])
@manager_required
def skip_week(job_id):
    """Mark a job as not required for a specific week. Auto-resets next week."""
    from blueprints.scheduler import _current_monday
    db       = get_db()
    week     = request.form.get("week_start") or _current_monday()
    job      = db.execute("SELECT job_number, skip_week FROM jobs WHERE id=?", (job_id,)).fetchone()

    if not job:
        flash("Job not found.", "error")
        db.close()
        return redirect(request.referrer or url_for("jobs.list_jobs"))

    # Toggle: if already skipped this week, un-skip
    if job["skip_week"] == week:
        db.execute("UPDATE jobs SET skip_week=NULL WHERE id=?", (job_id,))
        flash(f"{job['job_number']} added back to this week's plan.", "success")
    else:
        db.execute("UPDATE jobs SET skip_week=? WHERE id=?", (week, job_id))
        flash(f"{job['job_number']} marked as not required this week. Will reset automatically next week.", "success")

    db.commit()
    db.close()
    return redirect(request.referrer or url_for("scheduler.index"))
