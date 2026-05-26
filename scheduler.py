"""
Rapid Assembly — Auto-scheduling Engine (Section 6)
=====================================================
8-week rolling plan. Runs every Friday for the coming 8 weeks.

Key rules:
- Always forward from next Monday (8 weeks)
- Incomplete jobs from current week → front of queue (rolled over)
- Completed jobs → drop out
- Splits Denisa has set → preserved, scheduler respects split quantities
- Waiting on Parts → skipped
- Stock WOs sorted by WO number ascending (oldest demand first)
- Stock WOs: partial qty if full qty doesn't fit
- Customer/assembly jobs: all-or-nothing
- Team restriction: Davenham jobs → Davenham operators
- Grade matching: exact first, step up if needed
- Davenham team jobs → Trainee minimum regardless of part grade
- Primary products: assigned first, non-primary bumped to make room
- Secondary products: fallback
"""

import datetime
import re
from database import get_db

GRADE_RANK   = {"Trainee": 1, "Competent": 2, "Skilled": 3, "Advanced Wireman": 4}
DAYS         = 5
SLOTS        = 8
DEFAULT_HOURS = 1
PLAN_WEEKS   = 8


def this_monday():
    """Return this week's Monday — correct start for the 8-week plan."""
    today  = datetime.date.today()
    return (today - datetime.timedelta(days=today.weekday())).isoformat()


def next_monday():
    """Return next Monday (kept for compatibility)."""
    today  = datetime.date.today()
    return (today + datetime.timedelta(days=(7 - today.weekday()) % 7 or 7)).isoformat()


def week_dates(week_start_str):
    monday = datetime.date.fromisoformat(week_start_str)
    return [(monday + datetime.timedelta(days=i)).isoformat() for i in range(5)]


def eight_week_starts(from_date=None):
    """Return list of 8 Monday ISO strings starting from this Monday."""
    start = datetime.date.fromisoformat(from_date) if from_date else datetime.date.fromisoformat(this_monday())
    return [(start + datetime.timedelta(weeks=i)).isoformat() for i in range(PLAN_WEEKS)]


def _effective_min_grade(job, part):
    if job["team"] == "Davenham":
        return "Trainee"
    if part is not None:
        try:
            grade = part["min_grade_name"]
            if grade:
                return grade
        except (KeyError, IndexError, TypeError):
            pass
    return "Trainee"


def _wo_number_key(job_number):
    m = re.search(r'\d+', job_number or "")
    return int(m.group()) if m else 999999



def resolve_bom_dependencies(jobs, db):
    """
    For each assembly job, check whether sub-assy WOs have enough
    available quantity (In Progress + Scheduled + Complete) to cover it.

    Returns:
        can_schedule  : {job_id: True/False}
        shortfalls    : [{job_id, job_number, part_number, need, have, short, missing_parts}]
        sub_must_precede : {assembly_job_id: [sub_assy_job_ids]}
    """
    # Build part -> available qty map (In Progress / Scheduled / Complete all count)
    # We allocate on first-come-first-served by due date then WO number
    part_available = {}   # part_id -> total available units across all WOs
    part_wos       = {}   # part_id -> [(job_id, qty, status, due_date, job_number)]

    for job in jobs:
        pid = job["part_id"]
        if pid is None:
            continue
        status = job["status"]
        if status in ("In Progress", "Scheduled", "Unscheduled"):
            qty = job["quantity"] or 0
            part_available[pid] = part_available.get(pid, 0) + qty
            part_wos.setdefault(pid, []).append((
                job["id"], qty, status,
                job["due_date"] or "9999", job["job_number"]
            ))

    # Load BOM
    bom = {}  # parent_part_id -> [(child_part_id, qty_per_unit)]
    for row in db.execute("""
        SELECT parent_part_id, child_part_id, quantity
        FROM bom_lines
    """).fetchall():
        bom.setdefault(row["parent_part_id"], []).append(
            (row["child_part_id"], row["quantity"])
        )

    # Sort assembly jobs by due date (earliest first = first claim on stock)
    assembly_jobs = sorted(
        [j for j in jobs if j.get("part_type") == "assembly" or _is_assembly(j, db)],
        key=lambda j: (j["due_date"] or "9999", j["job_number"])
    )

    # Track allocated quantities per part as we process consoles in order
    allocated = {}   # part_id -> units already claimed by earlier console jobs

    can_schedule = {}
    shortfalls   = []
    sub_must_precede = {}

    for job in assembly_jobs:
        pid = job["part_id"]
        if pid is None or pid not in bom:
            can_schedule[job["id"]] = True
            continue

        console_qty = job["quantity"] or 1
        children    = bom[pid]
        job_ok      = True
        missing     = []
        preceding   = []

        for child_pid, qty_per_unit in children:
            needed    = qty_per_unit * console_qty
            available = part_available.get(child_pid, 0)
            already   = allocated.get(child_pid, 0)
            remaining = available - already

            if remaining < needed:
                short = needed - remaining
                child_pn = db.execute(
                    "SELECT part_number FROM parts WHERE id=?", (child_pid,)
                ).fetchone()
                missing.append({
                    "child_part_id": child_pid,
                    "part_number":   child_pn["part_number"] if child_pn else str(child_pid),
                    "needed":        needed,
                    "available":     max(0, remaining),
                    "short":         short,
                })
                job_ok = False
            else:
                # Allocate this console's share
                allocated[child_pid] = already + needed

            # Sub-assy WOs for this child must precede the console in the plan
            for wo_id, _, _, _, _ in part_wos.get(child_pid, []):
                preceding.append(wo_id)

        can_schedule[job["id"]] = job_ok
        sub_must_precede[job["id"]] = list(set(preceding))

        if missing:
            shortfalls.append({
                "job_id":      job["id"],
                "job_number":  job["job_number"],
                "part_number": db.execute(
                    "SELECT part_number FROM parts WHERE id=?", (pid,)
                ).fetchone()["part_number"],
                "console_qty": console_qty,
                "missing":     missing,
            })

    return can_schedule, shortfalls, sub_must_precede


def _is_assembly(job, db):
    """Check if a job's part is an assembly type."""
    if not job.get("part_id"):
        return False
    row = db.execute(
        "SELECT part_type FROM parts WHERE id=?", (job["part_id"],)
    ).fetchone()
    return row and row["part_type"] == "assembly"


def run_8week_scheduler(from_date=None, mode="deadline", db=None):
    """
    Run the full 8-week auto-plan.

    Returns dict with:
        weeks        : list of week_start strings
        assignments  : list of assignment dicts
        scheduled    : set of job_ids scheduled
        unscheduled  : list of {job_id, reason, hours_needed, hours_available}
        warnings     : list of str
        capacity     : {week_start: {grade: {needed, available, shortfall}}}
        planned_quantities: {job_id: {planned, total, partial}}
    """
    close_db = db is None
    if db is None:
        db = get_db()

    weeks = eight_week_starts(from_date)
    result = {
        "weeks":             weeks,
        "assignments":       [],
        "scheduled":         set(),
        "unscheduled":       [],
        "warnings":          [],
        "capacity":          {},
        "planned_quantities": {},
        "bom_shortfalls":    [],
    }

    # ── Load employees ───────────────────────────────────────
    employees = db.execute("""
        SELECT e.id, e.name, e.team, t.name grade, t.rank grade_rank
        FROM employees e
        JOIN tiers t ON t.id = e.tier_id
        WHERE e.active = 1
        ORDER BY t.rank DESC, e.name
    """).fetchall()

    # ── Load bank holidays ───────────────────────────────────
    bank_holiday_dates = {
        r["date"] for r in db.execute("SELECT date FROM bank_holidays").fetchall()
    }

    # ── Load absences and weekend working across 8 weeks ─────
    w0, w7 = weeks[0], (datetime.date.fromisoformat(weeks[-1]) + datetime.timedelta(days=6)).isoformat()
    absences        = {}
    weekend_working = {}
    for row in db.execute("""
        SELECT employee_id, date, day_type FROM employee_availability
        WHERE date BETWEEN ? AND ?
    """, (w0, w7)).fetchall():
        if row["day_type"] == "weekend_working":
            weekend_working.setdefault(row["employee_id"], set()).add(row["date"])
        else:
            absences.setdefault(row["employee_id"], set()).add(row["date"])

    # ── Specialist map ───────────────────────────────────────
    specialist_map = {}
    for row in db.execute("""
        SELECT es.part_id, es.employee_id, es.level, t.rank grade_rank
        FROM employee_specialisms es
        JOIN employees e ON e.id = es.employee_id
        JOIN tiers t ON t.id = e.tier_id
        WHERE e.active = 1
        ORDER BY es.level DESC, t.rank DESC
    """).fetchall():
        specialist_map.setdefault(row["part_id"], []).append(dict(row))

    # ── Capacity grid: {emp_id: {week: {day: set(slots)}}} ──
    cap = {}
    for emp in employees:
        cap[emp["id"]] = {w: {d: set() for d in range(1, DAYS+1)} for w in weeks}

    # ── Load jobs ────────────────────────────────────────────
    today_week = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).isoformat()

    jobs = db.execute("""
        SELECT j.*,
               p.id            part_id_val,
               p.part_number,
               p.part_type,
               p.estimated_hours,
               p.min_grade_id,
               t.name          min_grade_name,
               t.rank          min_grade_rank,
               j.planned_qty   so_urgent_qty
        FROM jobs j
        LEFT JOIN parts p ON p.id = j.part_id
        LEFT JOIN tiers t ON t.id = p.min_grade_id
        WHERE j.status NOT IN ('Complete')
          AND j.waiting_parts = 0
          AND (j.skip_week IS NULL OR j.skip_week != ?)
    """, (weeks[0],)).fetchall()

    # ── BOM dependency check ──────────────────────────────
    # Convert to dicts so resolve_bom_dependencies can work with them
    jobs_list = [dict(j) for j in jobs]
    can_schedule, bom_shortfalls, sub_must_precede = resolve_bom_dependencies(jobs_list, db)
    # Store shortfalls in result for the issues tab
    result["bom_shortfalls"] = bom_shortfalls

    # ── Load preserved splits ────────────────────────────────
    splits = db.execute("""
        SELECT * FROM job_splits WHERE manually_set = 1 AND status != 'Complete'
    """).fetchall()
    split_by_job = {}
    for s in splits:
        split_by_job.setdefault(s["parent_job_id"], []).append(dict(s))

    # ── Sort jobs ─────────────────────────────────────────────
    current_week_start = weeks[0]

    def sort_key(j):
        was_in_progress = j["status"] == "In Progress"
        is_stock        = bool(j["for_stock"])
        due             = j["due_date"] or "9999-12-31"
        value           = j["sale_value"] or 0
        has_so          = bool(j["sale_value"]) or bool(j["erp_ref"])

        # Tier 0: In Progress — already started, must finish
        # Within tier: SO-linked & most urgent due date first
        if was_in_progress:
            urgent = 0 if has_so else 1
            return (0, urgent, due, -value)

        # Tier 1: Unscheduled jobs with SO-derived due dates (urgent customer demand)
        # These take priority over undated stock builds
        if has_so and due != "9999-12-31":
            return (1, due, _wo_number_key(j["job_number"]))

        # Tier 2: Stock sub-assy WOs (no SO date) — sort by WO number ascending
        if is_stock and (j["part_type"] or "single") != "assembly":
            return (2, _wo_number_key(j["job_number"]), due)

        # Tier 3: Everything else — by chosen mode
        if mode == "deadline":
            return (3, due, -value)
        elif mode == "value":
            return (3, -value, due)
        elif mode == "efficiency":
            return (3, -(j["estimated_hours"] or DEFAULT_HOURS), due)
        else:  # balanced
            try:
                days_left = (datetime.date.fromisoformat(due) -
                             datetime.date.fromisoformat(current_week_start)).days + 1
            except Exception:
                days_left = 999
            score = (1 / max(1, days_left)) * 0.4 + (value / 100000) * 0.3 +                     ((j["estimated_hours"] or 1) / 40) * 0.3
            return (3, -score,)

    # Sub-assys first, assembly jobs after — enforces build order
    singles    = [j for j in jobs if (j["part_type"] or "single") != "assembly"]
    assemblies = [j for j in jobs if (j["part_type"] or "single") == "assembly"]
    try:
        singles.sort(key=sort_key)
        assemblies.sort(key=sort_key)
    except Exception:
        pass

    # Block assembly jobs with BOM shortfalls — send straight to unscheduled
    blocked_assemblies = []
    schedulable_assemblies = []
    for j in assemblies:
        if not can_schedule.get(j["id"], True):
            blocked_assemblies.append(j)
        else:
            schedulable_assemblies.append(j)

    for j in blocked_assemblies:
        sf = next((s for s in bom_shortfalls if s["job_id"] == j["id"]), None)
        reason = "Sub-assembly stock insufficient: " + ", ".join(
            f"{m['part_number']} short {m['short']:.0f}"
            for m in sf["missing"]
        ) if sf else "BOM dependency not met"
        result["unscheduled"].append({
            "job_id":          j["id"],
            "reason":          reason,
            "hours_needed":    (j["estimated_hours"] or 1) * (j["quantity"] or 1),
            "hours_available": 0,
        })
        result["warnings"].append(f"Job {j['job_number']} blocked — {reason}")

    result["bom_shortfalls"] = bom_shortfalls
    ordered_jobs = singles + schedulable_assemblies

    # ── Helpers ───────────────────────────────────────────────
    today_str = datetime.date.today().isoformat()

    def available_slots_week(emp_id, week):
        emp_abs        = absences.get(emp_id, set())
        emp_weekend    = weekend_working.get(emp_id, set())
        week_dates_list = week_dates(week)
        slots = []
        # Mon-Fri standard days
        for day_num, date_str in enumerate(week_dates_list, 1):
            if date_str in emp_abs:            continue
            if date_str in bank_holiday_dates: continue
            if date_str < today_str:           continue  # skip past days
            for slot in range(1, SLOTS + 1):
                if slot not in cap[emp_id][week][day_num]:
                    slots.append((week, day_num, slot))
        # Weekend days this operator is working (stored as extra day_num 6=Sat, 7=Sun)
        for date_str in emp_weekend:
            # only if in this week
            week_start_d = datetime.date.fromisoformat(week)
            try:
                d = datetime.date.fromisoformat(date_str)
            except Exception:
                continue
            if not (week_start_d <= d < week_start_d + datetime.timedelta(days=7)):
                continue
            if date_str in bank_holiday_dates: continue
            day_offset = (d - week_start_d).days  # 5=Sat, 6=Sun
            day_num    = day_offset + 1            # 6 or 7
            for slot in range(1, SLOTS + 1):
                if cap[emp_id].get(week, {}).get(day_num, set()) and slot in cap[emp_id][week][day_num]:
                    continue
                slots.append((week, day_num, slot))
        return slots

    def total_free_all_weeks(emp_id):
        return sum(
            len(available_slots_week(emp_id, w))
            for w in weeks
        )

    def eligible_operators(job, part):
        min_grade = _effective_min_grade(job, part)
        min_rank  = GRADE_RANK.get(min_grade, 1)
        job_team  = job["team"] or "General"
        result_emps = []
        for emp in employees:
            if job_team == "Davenham" and emp["team"] != "Davenham":
                continue
            if job_team != "Davenham" and emp["team"] == "Davenham":
                continue
            if emp["grade_rank"] < min_rank:
                continue
            result_emps.append(emp)
        return result_emps

    def candidate_order(eligible, job, part):
        part_id = job["part_id_val"] if job["part_id_val"] else job["part_id"]
        specs   = specialist_map.get(part_id, []) if part_id else []
        eligible_ids = {e["id"] for e in eligible}
        primary_ids   = [s["employee_id"] for s in specs if s["level"] == "primary"   and s["employee_id"] in eligible_ids]
        secondary_ids = [s["employee_id"] for s in specs if s["level"] == "secondary" and s["employee_id"] in eligible_ids]
        other_ids     = [e["id"] for e in eligible if e["id"] not in primary_ids and e["id"] not in secondary_ids]
        job_min_rank  = GRADE_RANK.get(_effective_min_grade(job, part), 1)

        def score(eid):
            emp = next(e for e in employees if e["id"] == eid)
            grade_penalty = emp["grade_rank"] - job_min_rank  # prefer closest grade match
            # Load balance: prefer operator with MOST free hours so work spreads evenly
            free = -total_free_all_weeks(eid)
            return (grade_penalty, free)

        candidates = []
        for group in [primary_ids, secondary_ids, other_ids]:
            group.sort(key=score)
            candidates.extend(group)
        return candidates

    def balance_candidates(eligible, job, part):
        """Like candidate_order but re-sorts by current free slots each call for better balance."""
        part_id = job["part_id_val"] if job["part_id_val"] else job["part_id"]
        specs   = specialist_map.get(part_id, []) if part_id else []
        eligible_ids = {e["id"] for e in eligible}
        primary_ids   = [s["employee_id"] for s in specs if s["level"] == "primary"   and s["employee_id"] in eligible_ids]
        secondary_ids = [s["employee_id"] for s in specs if s["level"] == "secondary" and s["employee_id"] in eligible_ids]
        other_ids     = [e["id"] for e in eligible if e["id"] not in primary_ids and e["id"] not in secondary_ids]
        job_min_rank  = GRADE_RANK.get(_effective_min_grade(job, part), 1)

        def score(eid):
            emp = next(e for e in employees if e["id"] == eid)
            return (emp["grade_rank"] - job_min_rank, -total_free_all_weeks(eid))

        candidates = []
        for group in [primary_ids, secondary_ids, other_ids]:
            group.sort(key=score)
            candidates.extend(group)
        return candidates

    def book(emp_id, slots_list, job_id, split_id=None):
        for (week, day, slot) in slots_list:
            cap[emp_id][week][day].add(slot)
            result["assignments"].append({
                "job_id":       job_id,
                "split_id":     split_id,
                "employee_id":  emp_id,
                "week_start":   week,
                "day_of_week":  day,
                "hour_slot":    slot,
                "planned_hours": 1.0,
                "auto_planned": 1,
            })
        result["capacity"].setdefault(week, {})

    def working_days_until(due_date_str):
        """Count available working days (Mon-Fri, excl. bank holidays) from today to due date."""
        if not due_date_str:
            return 999
        today = datetime.date.today()
        try:
            due   = datetime.date.fromisoformat(due_date_str)
        except Exception:
            return 999
        count = 0
        d     = today
        while d <= due:
            if d.weekday() < 5 and d.isoformat() not in bank_holiday_dates:
                count += 1
            d += datetime.timedelta(days=1)
        return count

    def slots_available_before_due(emp_id, due_date_str):
        """Return free slots for emp across 8 weeks that fall on or before due_date."""
        if not due_date_str:
            return available_slots_all_weeks(emp_id)
        try:
            due = datetime.date.fromisoformat(due_date_str)
        except Exception:
            return available_slots_all_weeks(emp_id)
        slots = []
        for w in weeks:
            week_start = datetime.date.fromisoformat(w)
            # Generate all dates in this week (Mon-Fri + any weekend working)
            for s in available_slots_week(emp_id, w):
                wk, day_num, slot_num = s
                day_date = week_start + datetime.timedelta(days=day_num - 1)
                if day_date <= due:
                    slots.append(s)
        return slots

    def available_slots_all_weeks(emp_id):
        slots = []
        for w in weeks:
            slots.extend(available_slots_week(emp_id, w))
        return slots

    def find_and_book_multiop(job, part, hours_per_unit, qty, due_date_str=None, split_id=None, urgent_qty=None):
        """
        Date-aware multi-operator scheduling.
        1. Try primary specialist first — assign as many units as they can finish before due date
        2. Remaining units go to next eligible operator before due date
        3. Repeat until qty covered or no more capacity before due date
        4. If still not all covered, fill remaining from any available slots (may be late)
        urgent_qty: the SO quantity that must be delivered on time — lateness judged against
                    this, not the full WO quantity (e.g. WO has 14 but SO only needs 1)
        Returns (assignments_list, planned_qty, is_late, days_late)
        """
        if urgent_qty is None:
            urgent_qty = qty
        eligible_emps = eligible_operators(job, part)
        candidates    = candidate_order(eligible_emps, job, part)
        spu           = max(1, round(hours_per_unit))

        assignments_made = []
        remaining        = qty

        for emp_id in candidates:
            if remaining <= 0:
                break
            # Slots before due date
            before_due = slots_available_before_due(emp_id, due_date_str)
            units_fit  = int(min(remaining, len(before_due) // spu))
            if units_fit >= 1:
                taken = before_due[:units_fit * spu]
                book(emp_id, taken, job["id"], split_id)
                assignments_made.append({"employee_id": emp_id, "units": units_fit})
                remaining -= units_fit

        # Late only if we couldn't cover the SO urgent quantity before due date
        # (not the full WO qty — stock builds just continue afterwards)
        urgent_scheduled_before_due = qty - remaining_before_urgent_check if hasattr(locals(), "remaining_before_urgent_check") else (qty - remaining)
        is_late  = (qty - remaining) < urgent_qty  # couldn't fill SO qty before due date
        days_late = 0

        if is_late:
            # Fill remaining from any slots regardless of date
            for emp_id in candidates:
                if remaining <= 0:
                    break
                all_free  = available_slots_all_weeks(emp_id)
                units_fit = int(min(remaining, len(all_free) // spu))
                if units_fit >= 1:
                    taken = all_free[:units_fit * spu]
                    # Find latest slot date to calculate days late
                    if taken:
                        wk, dn, _ = taken[-1]
                        last_date = datetime.date.fromisoformat(wk) + datetime.timedelta(days=dn - 1)
                        if due_date_str:
                            due = datetime.date.fromisoformat(due_date_str)
                            if last_date > due:
                                # Count working days between due and last slot
                                d2 = due + datetime.timedelta(days=1)
                                while d2 <= last_date:
                                    if d2.weekday() < 5 and d2.isoformat() not in bank_holiday_dates:
                                        days_late += 1
                                    d2 += datetime.timedelta(days=1)
                    book(emp_id, taken, job["id"], split_id)
                    assignments_made.append({"employee_id": emp_id, "units": units_fit})
                    remaining -= units_fit

        planned_qty = qty - remaining
        return assignments_made, planned_qty, is_late, days_late

    def find_and_book(job, part, hours_needed, prefer_week=None, split_id=None):
        """Try to book hours_needed slots. Returns (emp_id, slots) or (None, [])."""
        eligible   = eligible_operators(job, part)
        candidates = candidate_order(eligible, job, part)
        slots_needed = max(1, round(hours_needed))

        for emp_id in candidates:
            # Collect free slots — prefer_week first
            all_free = []
            week_order = ([prefer_week] + [w for w in weeks if w != prefer_week]) if prefer_week else weeks
            for w in week_order:
                all_free.extend(available_slots_week(emp_id, w))
            if len(all_free) >= slots_needed:
                taken = all_free[:slots_needed]
                book(emp_id, taken, job["id"], split_id)
                return emp_id, taken
        return None, []

    def find_and_book_partial(job, part, hours_per_unit, qty, prefer_week=None, split_id=None):
        """For stock WOs — book as many complete units as possible."""
        eligible   = eligible_operators(job, part)
        candidates = candidate_order(eligible, job, part)
        slots_per_unit = max(1, round(hours_per_unit))

        for emp_id in candidates:
            all_free = []
            week_order = ([prefer_week] + [w for w in weeks if w != prefer_week]) if prefer_week else weeks
            for w in week_order:
                all_free.extend(available_slots_week(emp_id, w))
            units_fit = min(int(qty), len(all_free) // slots_per_unit)
            if units_fit >= 1:
                taken = all_free[:units_fit * slots_per_unit]
                book(emp_id, taken, job["id"], split_id)
                return emp_id, taken, units_fit
        return None, [], 0

    # ── Capacity analysis ─────────────────────────────────────
    # Calculate available hours per grade per week
    grade_available = {}  # {week: {grade: hours}}
    for w in weeks:
        grade_available[w] = {}
        for emp in employees:
            g = emp["grade"]
            hrs = len(available_slots_week(emp["id"], w))
            grade_available[w][g] = grade_available[w].get(g, 0) + hrs

    # ── Schedule each job ─────────────────────────────────────
    scheduled_job_ids = set()

    for job in ordered_jobs:
        job_id  = job["id"]
        part    = job
        hours   = job["estimated_hours"] or DEFAULT_HOURS
        qty     = job["quantity"] or 1

        # Assembly: check children scheduled
        if job["part_type"] == "assembly" and job["part_id_val"]:
            children = db.execute("""
                SELECT bl.child_part_id FROM bom_lines bl
                WHERE bl.parent_part_id = ?
            """, (job["part_id_val"],)).fetchall()
            child_part_ids = {c["child_part_id"] for c in children}
            child_jobs = [j for j in jobs if j["part_id_val"] in child_part_ids]
            unscheduled_children = [cj for cj in child_jobs if cj["id"] not in scheduled_job_ids]
            if unscheduled_children:
                result["unscheduled"].append({
                    "job_id":          job_id,
                    "reason":          f"Waiting for {len(unscheduled_children)} child job(s)",
                    "hours_needed":    hours * qty,
                    "hours_available": 0,
                })
                continue

        # Check for preserved splits
        job_splits = split_by_job.get(job_id, [])

        if job_splits:
            # Schedule each split portion separately
            all_split_ok = True
            for sp in job_splits:
                sq = sp["split_qty"]
                prefer_week = sp.get("week_preference")

                if job["for_stock"] and job["part_type"] != "assembly":
                    emp_id, slots, planned = find_and_book_partial(
                        job, part, hours, sq, prefer_week=prefer_week, split_id=sp["id"]
                    )
                    if planned >= 1:
                        result["planned_quantities"][f"{job_id}_{sp['id']}"] = {
                            "planned": planned, "total": sq, "partial": planned < sq,
                            "split_id": sp["id"],
                        }
                    else:
                        all_split_ok = False
                else:
                    emp_id, slots = find_and_book(
                        job, part, hours * sq, prefer_week=prefer_week, split_id=sp["id"]
                    )
                    if emp_id:
                        result["planned_quantities"][f"{job_id}_{sp['id']}"] = {
                            "planned": sq, "total": sq, "partial": False, "split_id": sp["id"],
                        }
                    else:
                        all_split_ok = False

            if all_split_ok or any(f"{job_id}_{sp['id']}" in result["planned_quantities"] for sp in job_splits):
                scheduled_job_ids.add(job_id)
                result["scheduled"].add(job_id)
            else:
                result["unscheduled"].append({
                    "job_id": job_id, "reason": "No capacity for any split portion",
                    "hours_needed": hours * qty, "hours_available": 0,
                })

        else:
            # All jobs: date-aware multi-operator scheduling
            # For stock WOs linked to SOs, lateness is judged against SO qty only
            # e.g. WO has 14 units but SO only needs 1 — it's late only if
            # that 1 unit can't be delivered in time, not all 14
            so_urgent = job["so_urgent_qty"] if job["so_urgent_qty"] else (job["quantity"] or 1)
            try:
                so_urgent = int(so_urgent)
            except (TypeError, ValueError):
                so_urgent = int(job["quantity"] or 1)

            asgns, planned, is_late, days_late = find_and_book_multiop(
                job, part, hours, qty, due_date_str=job["due_date"],
                urgent_qty=so_urgent
            )
            if planned >= 1:
                scheduled_job_ids.add(job_id)
                result["scheduled"].add(job_id)
                is_partial = job["for_stock"] and planned < qty
                result["planned_quantities"][job_id] = {
                    "planned":   planned,
                    "total":     qty,
                    "partial":   is_partial,
                    "is_late":   is_late,
                    "days_late": days_late,
                    "operators": len(asgns),
                }
                if is_late and job["due_date"]:
                    result["warnings"].append(
                        f"Job {job['job_number']} will be {days_late} working day(s) late "                        f"(due {job['due_date']})"
                    )
            else:
                eligible = eligible_operators(job, part)
                avail    = sum(len(available_slots_all_weeks(e["id"])) for e in eligible)
                result["unscheduled"].append({
                    "job_id":          job_id,
                    "reason":          "No capacity available across 8-week window",
                    "hours_needed":    round(hours * qty, 1),
                    "hours_available": avail,
                })
                result["warnings"].append(
                    f"Job {job['job_number']}: needs {hours*qty:.0f}h, "                    f"only {avail}h available — {max(0, hours*qty-avail):.0f}h short"
                )

    # ── Build capacity analysis ───────────────────────────────
    # Hours needed per grade across all weeks
    grade_needed = {w: {} for w in weeks}
    for a in result["assignments"]:
        # Find the job's min grade
        j = next((x for x in jobs if x["id"] == a["job_id"]), None)
        if not j:
            continue
        grade = _effective_min_grade(j, j)
        w = a["week_start"]
        grade_needed[w][grade] = grade_needed[w].get(grade, 0) + 1

    for w in weeks:
        result["capacity"][w] = {}
        for grade in ["Trainee", "Competent", "Skilled", "Advanced Wireman"]:
            avail   = grade_available[w].get(grade, 0)
            needed  = grade_needed[w].get(grade, 0)
            result["capacity"][w][grade] = {
                "available": avail,
                "needed":    needed,
                "shortfall": max(0, needed - avail),
            }

    if close_db:
        db.close()

    return result


def apply_8week_schedule(result, db=None):
    """Write 8-week schedule to DB. Clears previous auto-planned assignments."""
    close_db = db is None
    if db is None:
        db = get_db()

    # Clear all auto-planned assignments across the 8-week window
    for w in result["weeks"]:
        db.execute(
            "DELETE FROM job_assignments WHERE week_start=? AND auto_planned=1",
            (w,)
        )

    for a in result["assignments"]:
        db.execute("""
            INSERT INTO job_assignments
                (job_id, split_id, employee_id, week_start, day_of_week,
                 hour_slot, planned_hours, auto_planned)
            VALUES (?,?,?,?,?,?,?,1)
        """, (
            a["job_id"], a.get("split_id"), a["employee_id"],
            a["week_start"], a["day_of_week"], a["hour_slot"], a["planned_hours"]
        ))

    # Update job statuses and planned quantities
    pq = result.get("planned_quantities", {})
    for job_id in result["scheduled"]:
        # Find the main planned_qty entry (not split-keyed)
        qty_info = pq.get(job_id) or next(
            (v for k, v in pq.items() if str(k).startswith(f"{job_id}_")), {}
        )
        planned = qty_info.get("planned") if qty_info else None
        db.execute("""
            UPDATE jobs SET status='Scheduled', planned_qty=?,
            updated_at=datetime('now') WHERE id=? AND status NOT IN ('Complete','In Progress')
        """, (planned, job_id))

    db.commit()
    if close_db:
        db.close()


# ── Legacy single-week wrapper (keeps existing routes working) ───────────────
def run_scheduler(week_start, mode="deadline", db=None):
    """Thin wrapper — runs 8-week scheduler but returns single-week style result."""
    r8 = run_8week_scheduler(from_date=week_start, mode=mode, db=db)
    return {
        "assignments":       [a for a in r8["assignments"] if a["week_start"] == week_start],
        "scheduled":         list(r8["scheduled"]),
        "unscheduled":       r8["unscheduled"],
        "warnings":          r8["warnings"],
        "operator_util":     {},
        "planned_quantities": r8["planned_quantities"],
    }


def apply_schedule(result, week_start, db=None):
    """Legacy single-week apply."""
    close_db = db is None
    if db is None:
        db = get_db()
    db.execute("DELETE FROM job_assignments WHERE week_start=? AND auto_planned=1", (week_start,))
    for a in result["assignments"]:
        db.execute("""
            INSERT INTO job_assignments
                (job_id, split_id, employee_id, week_start, day_of_week,
                 hour_slot, planned_hours, auto_planned)
            VALUES (?,?,?,?,?,?,?,1)
        """, (
            a["job_id"], a.get("split_id"), a["employee_id"],
            a["week_start"], a["day_of_week"], a["hour_slot"], a["planned_hours"]
        ))
    pq = result.get("planned_quantities", {})
    for job_id in result["scheduled"]:
        qty_info = pq.get(job_id, {})
        planned  = qty_info.get("planned")
        db.execute("""
            UPDATE jobs SET status='Scheduled', planned_qty=?, updated_at=datetime('now')
            WHERE id=? AND status NOT IN ('Complete','In Progress')
        """, (planned, job_id))
    db.commit()
    if close_db:
        db.close()
