import sqlite3
import os

DB_PATH = os.environ.get("DATABASE_URL", "rapid_assembly.db")

# Grade rules applied during scheduling
# Davenham team jobs always require Trainee grade minimum
# Console top-level (assembly) = Skilled
# Console sub-assemblies (single children) = Competent
TEAM_MIN_GRADE = {
    "Davenham": "Trainee",
}


def get_db():
    """Return a database connection with row_factory set."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create all tables and seed initial data if needed."""
    conn = get_db()
    c = conn.cursor()

    # ------------------------------------------------------------------ #
    #  USERS & AUTH                                                        #
    # ------------------------------------------------------------------ #
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            password    TEXT    NOT NULL,
            role        TEXT    NOT NULL CHECK(role IN ('manager','operator')),
            employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            active      INTEGER NOT NULL DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES users(id),
            action     TEXT    NOT NULL,
            detail     TEXT,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------ #
    #  SKILL DEFINITIONS                                                   #
    # ------------------------------------------------------------------ #
    c.execute("""
        CREATE TABLE IF NOT EXISTS skill_types (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL UNIQUE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS tiers (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT    NOT NULL UNIQUE,
            rank  INTEGER NOT NULL UNIQUE   -- used for >= comparisons
        )
    """)

    # ------------------------------------------------------------------ #
    #  EMPLOYEES                                                           #
    # ------------------------------------------------------------------ #
    c.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            department  TEXT,
            tier_id     INTEGER NOT NULL REFERENCES tiers(id),
            team        TEXT    NOT NULL DEFAULT 'General',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # migrate
    _emp_cols = [r[1] for r in c.execute("PRAGMA table_info(employees)").fetchall()]
    if 'team' not in _emp_cols:
        c.execute("ALTER TABLE employees ADD COLUMN team TEXT NOT NULL DEFAULT 'General'")

    c.execute("""
        CREATE TABLE IF NOT EXISTS employee_skills (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id  INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            skill_type_id INTEGER NOT NULL REFERENCES skill_types(id) ON DELETE CASCADE,
            UNIQUE(employee_id, skill_type_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS employee_availability (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            date        TEXT    NOT NULL,           -- ISO date YYYY-MM-DD
            unavailable INTEGER NOT NULL DEFAULT 1, -- 1 = unavailable all day
            reason      TEXT,
            UNIQUE(employee_id, date)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS employee_specialisms (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            part_id     INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
            level       TEXT    NOT NULL CHECK(level IN ('primary','secondary')),
            UNIQUE(employee_id, part_id)
        )
    """)

    # ------------------------------------------------------------------ #
    #  PRODUCT LIBRARY / BOM                                              #
    # ------------------------------------------------------------------ #
    c.execute("""
        CREATE TABLE IF NOT EXISTS parts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            part_number         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            description         TEXT,
            part_type           TEXT    NOT NULL CHECK(part_type IN ('single','assembly')),
            estimated_hours     REAL,
            min_grade_id        INTEGER REFERENCES tiers(id),
            actual_hours_sum    REAL    NOT NULL DEFAULT 0,
            actual_hours_count  INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # migrate: add columns if upgrading from older schema
    existing_cols = [r[1] for r in c.execute('PRAGMA table_info(parts)').fetchall()]
    for col, defn in [
        ('estimated_hours',    'REAL'),
        ('min_grade_id',       'INTEGER'),
        ('actual_hours_sum',   'REAL NOT NULL DEFAULT 0'),
        ('actual_hours_count', 'INTEGER NOT NULL DEFAULT 0'),
    ]:
        if col not in existing_cols:
            c.execute(f'ALTER TABLE parts ADD COLUMN {col} {defn}')

    c.execute("""
        CREATE TABLE IF NOT EXISTS bom_lines (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_part_id  INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
            child_part_id   INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
            quantity        REAL    NOT NULL DEFAULT 1,
            sequence_order  INTEGER NOT NULL DEFAULT 0,
            run_mode        TEXT    NOT NULL CHECK(run_mode IN ('parallel','sequential')),
            UNIQUE(parent_part_id, child_part_id)
        )
    """)

    # ------------------------------------------------------------------ #
    #  JOBS                                                                #
    # ------------------------------------------------------------------ #
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_number      TEXT    NOT NULL UNIQUE,
            part_id         INTEGER REFERENCES parts(id),
            description     TEXT,
            customer        TEXT,
            quantity        REAL    NOT NULL DEFAULT 1,
            status          TEXT    NOT NULL DEFAULT 'Unscheduled'
                                CHECK(status IN ('Unscheduled','Scheduled','In Progress','Complete','On Hold')),
            due_date        TEXT,
            sale_value      REAL,
            waiting_parts   INTEGER NOT NULL DEFAULT 0,
            for_stock       INTEGER NOT NULL DEFAULT 0,
            parent_job_id   INTEGER REFERENCES jobs(id),
            eas_number      TEXT,
            customers_po    TEXT,
            source          TEXT    DEFAULT 'manual',
            team            TEXT,
            skip_week       TEXT,
            planned_qty     REAL,
            erp_ref         TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # migrate older schemas
    _jobs_cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()]
    for _col, _defn in [
        ("customer",     "TEXT"),
        ("eas_number",   "TEXT"),
        ("customers_po", "TEXT"),
        ("for_stock",    "INTEGER NOT NULL DEFAULT 0"),
        ("source",       "TEXT DEFAULT 'manual'"),
        ("team",         "TEXT"),
        ("skip_week",    "TEXT"),
        ("planned_qty",  "REAL"),
    ]:
        if _col not in _jobs_cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {_col} {_defn}")

    c.execute("""
        CREATE TABLE IF NOT EXISTS bank_holidays (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            auto_loaded INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT
        )
    """)

    # Add manual_locked to jobs if missing
    _jcols = [r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()]
    if 'manual_locked' not in _jcols:
        c.execute("ALTER TABLE jobs ADD COLUMN manual_locked INTEGER NOT NULL DEFAULT 0")

    # Add manual_locked to jobs if missing
    # Extend employee_availability with day_type
    _ea_cols = [r[1] for r in c.execute("PRAGMA table_info(employee_availability)").fetchall()]
    if 'day_type' not in _ea_cols:
        c.execute("ALTER TABLE employee_availability ADD COLUMN day_type TEXT NOT NULL DEFAULT 'absence'")

    c.execute("""
        CREATE TABLE IF NOT EXISTS job_splits (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            split_qty       REAL    NOT NULL,
            week_preference TEXT,
            manually_set    INTEGER NOT NULL DEFAULT 1,
            planned_qty     REAL,
            status          TEXT    NOT NULL DEFAULT 'Unscheduled',
            created_at      TEXT,
            updated_at      TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS job_assignments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            employee_id  INTEGER NOT NULL REFERENCES employees(id),
            week_start   TEXT    NOT NULL,   -- ISO date of Monday
            day_of_week  INTEGER NOT NULL CHECK(day_of_week BETWEEN 1 AND 5), -- 1=Mon
            hour_slot    INTEGER NOT NULL CHECK(hour_slot BETWEEN 1 AND 8),
            planned_hours REAL   NOT NULL DEFAULT 1,
            auto_planned INTEGER NOT NULL DEFAULT 0,
            notes        TEXT,
            split_id     INTEGER,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # migrate split_id if missing (for existing DBs)
    _ja_cols2 = [r[1] for r in c.execute("PRAGMA table_info(job_assignments)").fetchall()]
    if 'split_id' not in _ja_cols2:
        c.execute("ALTER TABLE job_assignments ADD COLUMN split_id INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS job_actuals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            employee_id  INTEGER NOT NULL REFERENCES employees(id),
            actual_hours REAL    NOT NULL,
            logged_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            notes        TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS week_locks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start  TEXT    NOT NULL UNIQUE,
            locked_by   INTEGER REFERENCES users(id),
            locked_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            unlocked_at TEXT
        )
    """)

    # ------------------------------------------------------------------ #
    #  SEED REFERENCE DATA                                                 #
    # ------------------------------------------------------------------ #
    # Tiers (rank = higher is more senior)
    tiers = [
        ("Trainee", 1),
        ("Competent", 2),
        ("Skilled", 3),
        ("Advanced Wireman", 4),
    ]
    c.executemany(
        "INSERT OR IGNORE INTO tiers(name, rank) VALUES(?,?)", tiers
    )

    # Skill types
    skills = [
        ("Cage Console Wiring",),
        ("Harness Building",),
        ("Mechanical Assembly",),
        ("Control Panel",),
        ("Sub Assembly",),
        ("Testing & Inspection",),
    ]
    c.executemany(
        "INSERT OR IGNORE INTO skill_types(name) VALUES(?)", skills
    )

    # ------------------------------------------------------------------ #
    #  SEED EMPLOYEES                                                      #
    # ------------------------------------------------------------------ #
    employees_seed = [
        # (name, department, tier, [skills])
        ("Rob Kinton",       "Wiring",      "Advanced Wireman", ["Control Panel", "Cage Console Wiring"]),
        ("Hussein Sharif",   "Wiring",      "Skilled", ["Harness Building", "Cage Console Wiring"]),
        ("Mayer Patel",      "Wiring",      "Skilled", ["Cage Console Wiring"]),
        ("Chianna Connelly", "Wiring",      "Competent", ["Cage Console Wiring", "Sub Assembly"]),
        ("James Hayes",      "Wiring",      "Competent", ["Harness Building"]),
        ("Nick Jennings",    "Mechanical",  "Skilled", ["Mechanical Assembly", "Sub Assembly"]),
        ("Shamsher Ali",     "Wiring",      "Competent", ["Cage Console Wiring"]),
        ("Justyna Dygus",    "Wiring",      "Trainee", ["Sub Assembly"]),
        ("Skot Barratt",     "Wiring",      "Skilled", ["Cage Console Wiring", "Control Panel"]),
        ("Bradley Evans",    "Wiring",      "Competent", ["Cage Console Wiring", "Harness Building"]),
        ("Damien Rowlinson", "Wiring",      "Skilled", ["Harness Building", "Cage Console Wiring"]),
        ("Steven Borley",    "Mechanical",  "Competent", ["Mechanical Assembly"]),
    ]

    for name, dept, tier_name, skill_names in employees_seed:
        # Get tier id
        tier = c.execute("SELECT id FROM tiers WHERE name=?", (tier_name,)).fetchone()
        # Check if employee already exists
        existing = c.execute("SELECT id FROM employees WHERE name=?", (name,)).fetchone()
        if existing:
            emp_id = existing["id"]
        else:
            c.execute(
                "INSERT INTO employees(name, department, tier_id) VALUES(?,?,?)",
                (name, dept, tier["id"])
            )
            emp_id = c.lastrowid
        # Assign skills
        for skill_name in skill_names:
            skill = c.execute("SELECT id FROM skill_types WHERE name=?", (skill_name,)).fetchone()
            if skill:
                c.execute(
                    "INSERT OR IGNORE INTO employee_skills(employee_id, skill_type_id) VALUES(?,?)",
                    (emp_id, skill["id"])
                )

    # Set correct team assignments
    davenham_members = ['Justyna Dygus', 'Chianna Connelly', 'Hussein Sharif', 'Steven Borley']
    c.execute("UPDATE employees SET team='General'")
    for name in davenham_members:
        c.execute("UPDATE employees SET team='Davenham' WHERE name=?", (name,))


    c.execute("""
        CREATE TABLE IF NOT EXISTS planning_checks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            check_name   TEXT NOT NULL UNIQUE,
            checked_at   TEXT,
            checked_by   INTEGER,
            notes        TEXT,
            resets_on    TEXT
        )
    """)
    for _cn in ('absences','works_orders','sales_orders'):
        c.execute('INSERT OR IGNORE INTO planning_checks(check_name) VALUES(?)', (_cn,))

    # ── Seed England & Wales bank holidays if table is empty ─────
    _bh_count = c.execute("SELECT COUNT(*) FROM bank_holidays").fetchone()[0]
    if _bh_count == 0:
        _BANK_HOLIDAYS = [
            ("2025-01-01","New Year's Day"),("2025-04-18","Good Friday"),
            ("2025-04-21","Easter Monday"),("2025-05-05","Early May Bank Holiday"),
            ("2025-05-26","Spring Bank Holiday"),("2025-08-25","Summer Bank Holiday"),
            ("2025-12-25","Christmas Day"),("2025-12-26","Boxing Day"),
            ("2026-01-01","New Year's Day"),("2026-04-03","Good Friday"),
            ("2026-04-06","Easter Monday"),("2026-05-04","Early May Bank Holiday"),
            ("2026-05-25","Spring Bank Holiday"),("2026-08-31","Summer Bank Holiday"),
            ("2026-12-25","Christmas Day"),("2026-12-28","Boxing Day (substitute)"),
            ("2027-01-01","New Year's Day"),("2027-03-26","Good Friday"),
            ("2027-03-29","Easter Monday"),("2027-05-03","Early May Bank Holiday"),
            ("2027-05-31","Spring Bank Holiday"),("2027-08-30","Summer Bank Holiday"),
            ("2027-12-27","Christmas Day (substitute)"),("2027-12-28","Boxing Day (substitute)"),
        ]
        for _date, _name in _BANK_HOLIDAYS:
            c.execute("INSERT OR IGNORE INTO bank_holidays(date,name,auto_loaded) VALUES(?,?,1)", (_date,_name))
    conn.commit()
    conn.close()
    print("Database initialised.")


if __name__ == "__main__":
    init_db()
