#!/usr/bin/env python3
"""
init_db.py — Create (or verify) the pipeline SQLite database.

Usage:
    python db/init_db.py                  # creates pipeline.db at project root
    python db/init_db.py --check          # verify schema without recreating
    python db/init_db.py --reset          # DROP and recreate (destructive!)
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Resolve paths relative to this file so the script works from any cwd
DB_DIR    = Path(__file__).parent
SCHEMA    = DB_DIR / "schema.sql"
DB_PATH   = DB_DIR.parent / "pipeline.db"

EXPECTED_TABLES = {
    "structures",
    "docking_runs",
    "poses",
    "files",
    "transformations",
    "scores",
}

EXPECTED_VIEWS = {
    "v_pose_summary",
    "v_file_lineage",
    "v_run_overview",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def existing_views(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    ).fetchall()
    return {r["name"] for r in rows}


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def create_db(db_path: Path = DB_PATH) -> None:
    if not SCHEMA.exists():
        sys.exit(f"[ERROR] Schema file not found: {SCHEMA}")

    sql = SCHEMA.read_text()

    print(f"Creating database at: {db_path}")
    with get_conn(db_path) as conn:
        conn.executescript(sql)

    # Verify
    with get_conn(db_path) as conn:
        tables = existing_tables(conn)
        views  = existing_views(conn)

    missing_tables = EXPECTED_TABLES - tables
    missing_views  = EXPECTED_VIEWS  - views

    if missing_tables or missing_views:
        print("[WARNING] Some objects were not created:")
        for t in sorted(missing_tables):
            print(f"  missing table: {t}")
        for v in sorted(missing_views):
            print(f"  missing view:  {v}")
    else:
        print("[OK] All tables and views created successfully.")
        print("\nTables:")
        for t in sorted(EXPECTED_TABLES):
            print(f"  {t}")
        print("\nViews:")
        for v in sorted(EXPECTED_VIEWS):
            print(f"  {v}")


def check_db(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        sys.exit(f"[ERROR] Database not found: {db_path}")

    with get_conn(db_path) as conn:
        tables = existing_tables(conn)
        views  = existing_views(conn)

    missing_tables = EXPECTED_TABLES - tables
    missing_views  = EXPECTED_VIEWS  - views

    if missing_tables or missing_views:
        print("[FAIL] Schema is incomplete:")
        for t in sorted(missing_tables):
            print(f"  missing table: {t}")
        for v in sorted(missing_views):
            print(f"  missing view:  {v}")
        sys.exit(1)
    else:
        print(f"[OK] Schema looks good at {db_path}")


def reset_db(db_path: Path = DB_PATH) -> None:
    if db_path.exists():
        confirm = input(
            f"[WARNING] This will DELETE {db_path} and all its data.\n"
            "Type 'yes' to continue: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return
        db_path.unlink()
        print(f"Deleted {db_path}")
    create_db(db_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialise the pipeline database.")
    parser.add_argument("--db",    default=str(DB_PATH), help="Path to the .db file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Verify schema only")
    group.add_argument("--reset", action="store_true", help="Drop and recreate (destructive)")
    args = parser.parse_args()

    db_path = Path(args.db)

    if args.check:
        check_db(db_path)
    elif args.reset:
        reset_db(db_path)
    else:
        if db_path.exists():
            print(f"[INFO] Database already exists at {db_path}")
            print("       Run with --check to verify, or --reset to recreate.")
        else:
            create_db(db_path)