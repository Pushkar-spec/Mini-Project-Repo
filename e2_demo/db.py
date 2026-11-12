"""
db.py – SQLite connection helper and schema bootstrap for VersionVault E2 Demo.

Uses Python's built-in sqlite3; no ORM. All tables, migrations, and indexes are created
on startup via init_db().
"""

import sqlite3
import os
from typing import Optional

# Default path relative to this file; override via env var VV_DB_PATH.
_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "versionvault.db")
DB_PATH: str = os.environ.get("VV_DB_PATH", _DEFAULT_DB_PATH)


def get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open and return a sqlite3 connection with row_factory and WAL mode enabled."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode for better concurrent read performance
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS versions (
    version_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id              TEXT    NOT NULL,
    version_no          INTEGER NOT NULL,
    type                TEXT    NOT NULL CHECK(type IN ('snapshot', 'delta')),
    content_or_delta    TEXT    NOT NULL,
    sha256              TEXT    NOT NULL,
    author_id           TEXT    NOT NULL,
    lamport_ts          INTEGER NOT NULL,
    parent_ids          TEXT    NOT NULL,   -- JSON list, e.g. "[4]" or "[]"
    reason              TEXT    NOT NULL DEFAULT 'checkpoint'
                                    CHECK(reason IN ('checkpoint','safety','restore','submission','revision','published')),
    round_no            INTEGER NOT NULL DEFAULT 1,
    milestone           TEXT,
    created_at          TEXT    NOT NULL,
    UNIQUE(doc_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_versions_doc_ver
    ON versions(doc_id, version_no);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    doc_id      TEXT,
    event_type  TEXT,
    version_ref INTEGER,
    time        TEXT
);

CREATE TABLE IF NOT EXISTS doc_state (
    doc_id          TEXT    PRIMARY KEY,
    last_lamport    INTEGER NOT NULL DEFAULT 0,
    last_version_no INTEGER NOT NULL DEFAULT 0
);

-- Manuscript Workflow Tables
CREATE TABLE IF NOT EXISTS manuscripts (
    manuscript_id   TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    author_id       TEXT NOT NULL,
    status          TEXT NOT NULL,
    current_round   INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewer_assignments (
    manuscript_id   TEXT NOT NULL,
    reviewer_id     TEXT NOT NULL,
    assigned_by     TEXT NOT NULL,
    round_no        INTEGER NOT NULL,
    PRIMARY KEY (manuscript_id, reviewer_id, round_no)
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    manuscript_id   TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    round_no        INTEGER NOT NULL,
    reviewer_id     TEXT NOT NULL,
    comments        TEXT NOT NULL,
    recommendation  TEXT NOT NULL CHECK(recommendation IN ('accept','minor','major','reject')),
    requested_changes TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    manuscript_id   TEXT NOT NULL,
    round_no        INTEGER NOT NULL,
    editor_id       TEXT NOT NULL,
    decision        TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    revision_notes  TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (manuscript_id, round_no)
);

CREATE INDEX IF NOT EXISTS idx_reviewer_assignments ON reviewer_assignments(manuscript_id, round_no);
CREATE INDEX IF NOT EXISTS idx_reviews_manuscript ON reviews(manuscript_id, round_no);
"""


def _migrate_versions_if_needed(conn: sqlite3.Connection) -> None:
    """Safely migrate versions table to include round_no, milestone, and extended reason check."""
    cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='versions'")
    row = cur.fetchone()
    if not row:
        return

    table_sql = row["sql"] or ""
    # Check if migration is required (either missing reason values or missing columns)
    needs_recreate = ("submission" not in table_sql)
    has_round_no = "round_no" in [c["name"] for c in conn.execute("PRAGMA table_info(versions)").fetchall()]
    has_milestone = "milestone" in [c["name"] for c in conn.execute("PRAGMA table_info(versions)").fetchall()]

    if needs_recreate:
        conn.execute("PRAGMA foreign_keys=OFF;")
        conn.execute("ALTER TABLE versions RENAME TO versions_old_migration;")
        # Create new table
        conn.execute("""
            CREATE TABLE versions (
                version_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id              TEXT    NOT NULL,
                version_no          INTEGER NOT NULL,
                type                TEXT    NOT NULL CHECK(type IN ('snapshot', 'delta')),
                content_or_delta    TEXT    NOT NULL,
                sha256              TEXT    NOT NULL,
                author_id           TEXT    NOT NULL,
                lamport_ts          INTEGER NOT NULL,
                parent_ids          TEXT    NOT NULL,
                reason              TEXT    NOT NULL DEFAULT 'checkpoint'
                                                CHECK(reason IN ('checkpoint','safety','restore','submission','revision','published')),
                round_no            INTEGER NOT NULL DEFAULT 1,
                milestone           TEXT,
                created_at          TEXT    NOT NULL,
                UNIQUE(doc_id, version_no)
            );
        """)
        # Copy data
        old_cols = [c["name"] for c in conn.execute("PRAGMA table_info(versions_old_migration)").fetchall()]
        sel_round = "round_no" if "round_no" in old_cols else "1"
        sel_milestone = "milestone" if "milestone" in old_cols else "NULL"

        conn.execute(f"""
            INSERT INTO versions (
                version_id, doc_id, version_no, type, content_or_delta, sha256,
                author_id, lamport_ts, parent_ids, reason, round_no, milestone, created_at
            )
            SELECT version_id, doc_id, version_no, type, content_or_delta, sha256,
                   author_id, lamport_ts, parent_ids, reason, {sel_round}, {sel_milestone}, created_at
            FROM versions_old_migration;
        """)
        conn.execute("DROP TABLE versions_old_migration;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_doc_ver ON versions(doc_id, version_no);")
        conn.execute("PRAGMA foreign_keys=ON;")
    else:
        # Just ensure columns exist
        if not has_round_no:
            conn.execute("ALTER TABLE versions ADD COLUMN round_no INTEGER NOT NULL DEFAULT 1;")
        if not has_milestone:
            conn.execute("ALTER TABLE versions ADD COLUMN milestone TEXT;")


def _migrate_workflow_tables_if_needed(conn: sqlite3.Connection) -> None:
    """Safely migrate reviews and decisions tables to include requested_changes and revision_notes."""
    # Check reviews table
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reviews'")
    if cur.fetchone():
        cols = [c["name"] for c in conn.execute("PRAGMA table_info(reviews)").fetchall()]
        if "requested_changes" not in cols:
            conn.execute("ALTER TABLE reviews ADD COLUMN requested_changes TEXT NOT NULL DEFAULT '[]';")

    # Check decisions table
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'")
    if cur.fetchone():
        cols = [c["name"] for c in conn.execute("PRAGMA table_info(decisions)").fetchall()]
        if "revision_notes" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN revision_notes TEXT;")


def init_db(db_path: Optional[str] = None) -> None:
    """Create tables and indexes if they do not already exist, and run migrations."""
    conn = get_conn(db_path)
    try:
        _migrate_versions_if_needed(conn)
        _migrate_workflow_tables_if_needed(conn)
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
