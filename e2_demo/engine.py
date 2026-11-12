"""
engine.py – Core logic for VersionVault E2: Checkpoint, Version Persistence, and Manuscript Engine.

Requirement mapping:
  FR2.2  – safety checkpoint (pre_restore_checkpoint)
  FR2.3  – snapshot + delta storage + SHA-256 hash (save_version)
  FR2.4  – rebuild any version by replaying deltas (rebuild_version)
  FR2.5  – paginated version timeline (get_versions)
  FR2.6  – Lamport timestamp tagging (save_version Lamport logic)
  FR4.6  – audit log (add_audit, get_audit)
  Manuscript Workflow – State machine, roles, milestones, reviewer assignments, reviews, decisions
"""

import difflib
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from db import get_conn, DB_PATH

# ---------------------------------------------------------------------------
# Config (FR2.3)
# ---------------------------------------------------------------------------

SNAPSHOT_EVERY: int = 5  # version 1, 6, 11, 16, … are full snapshots

# Thread-safe flag: when True, the next write will raise an exception after
# INSERT but before COMMIT to demonstrate atomic rollback.
_sim_failure_lock = threading.Lock()
_simulate_failure: bool = False


def set_simulate_failure(flag: bool) -> None:
    """Enable or disable the simulated-failure flag (demo/debug only)."""
    global _simulate_failure
    with _sim_failure_lock:
        _simulate_failure = flag


def get_simulate_failure() -> bool:
    with _sim_failure_lock:
        return _simulate_failure


# ---------------------------------------------------------------------------
# Status State Machine (Manuscript Workflow)
# ---------------------------------------------------------------------------

VALID_TRANSITIONS = {
    "DRAFT": ["SUBMITTED"],
    "SUBMITTED": ["UNDER_REVIEW", "REJECTED"],
    "UNDER_REVIEW": ["REVISION_REQUESTED", "ACCEPTED", "REJECTED"],
    "REVISION_REQUESTED": ["REVISED"],
    "REVISED": ["UNDER_REVIEW"],
    "ACCEPTED": ["PUBLISHED"],
    "REJECTED": [],
    "PUBLISHED": [],
}


class StatusTransitionError(Exception):
    """Raised when an illegal status transition is attempted."""
    pass


class RoleAuthorizationError(Exception):
    """Raised when access control check fails."""
    pass


class DocumentLockedError(Exception):
    """Raised when write action is attempted on a published document."""
    pass


def validate_status_transition(current_status: str, new_status: str) -> None:
    allowed = VALID_TRANSITIONS.get(current_status, [])
    if new_status not in allowed:
        raise StatusTransitionError(
            f"Illegal status transition from {current_status} to {new_status}. Allowed: {allowed}"
        )


# ---------------------------------------------------------------------------
# Delta helpers (FR2.3)
# ---------------------------------------------------------------------------

def make_delta(old: str, new: str) -> list:
    """
    Compute a character-level diff from *old* to *new* using SequenceMatcher.

    Returns a JSON-serialisable list of ops:
      ["equal",  i1, i2]             – keep old[i1:i2]
      ["replace", i1, i2, new_text]  – replace old[i1:i2] with new_text
      ["delete",  i1, i2]            – delete old[i1:i2]
      ["insert",  i1, i2, new_text]  – insert new_text before old[i1]

    FR2.3: uses difflib.SequenceMatcher at character level.
    """
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ops.append(["equal", i1, i2])
        elif tag == "replace":
            ops.append(["replace", i1, i2, new[j1:j2]])
        elif tag == "delete":
            ops.append(["delete", i1, i2])
        elif tag == "insert":
            ops.append(["insert", i1, i2, new[j1:j2]])
    return ops


def apply_delta(old: str, delta: list) -> str:
    """
    Reconstruct the new text by applying *delta* (produced by make_delta) to *old*.

    FR2.4: replays deltas in order to rebuild a version.
    """
    result_parts: list[str] = []
    for op in delta:
        tag = op[0]
        if tag == "equal":
            _, i1, i2 = op
            result_parts.append(old[i1:i2])
        elif tag in ("replace", "insert"):
            _, i1, i2, new_text = op
            result_parts.append(new_text)
        elif tag == "delete":
            pass  # discard old[i1:i2]
        else:
            raise ValueError(f"Unknown delta op tag: {tag!r}")
    return "".join(result_parts)


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of *text* encoded as UTF-8. FR2.3."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------

def _get_doc_state(conn: sqlite3.Connection, doc_id: str) -> tuple[int, int]:
    """Return (last_lamport, last_version_no) for *doc_id*, inserting a row if absent."""
    row = conn.execute(
        "SELECT last_lamport, last_version_no FROM doc_state WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO doc_state(doc_id, last_lamport, last_version_no) VALUES (?,0,0)",
            (doc_id,),
        )
        return 0, 0
    return row["last_lamport"], row["last_version_no"]


def _update_doc_state(
    conn: sqlite3.Connection, doc_id: str, lamport: int, version_no: int
) -> None:
    """Upsert (last_lamport, last_version_no) for *doc_id*."""
    conn.execute(
        """INSERT INTO doc_state(doc_id, last_lamport, last_version_no)
           VALUES (?, ?, ?)
           ON CONFLICT(doc_id) DO UPDATE SET
               last_lamport    = excluded.last_lamport,
               last_version_no = excluded.last_version_no""",
        (doc_id, lamport, version_no),
    )


# ---------------------------------------------------------------------------
# Core: save_version (FR2.3, FR2.6, Manuscript Milestones)
# ---------------------------------------------------------------------------

def save_version(
    doc_id: str,
    user_id: str,
    lamport: int,
    text: str,
    reason: str = "checkpoint",
    parent_override: Optional[list[int]] = None,
    round_no: int = 1,
    milestone: Optional[str] = None,
    force_snapshot: bool = False,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Persist a new version of *doc_id* atomically (BEGIN IMMEDIATE … COMMIT).

    FR2.3 – stores a full snapshot every SNAPSHOT_EVERY versions, deltas in between.
    FR2.6 – advances the Lamport clock: new_ts = max(received, last) + 1.
    Milestone versions (submission, revision, published) are ALWAYS stored as snapshots.
    """
    # Check if manuscript is locked
    if is_locked_published(doc_id, db_path=db_path):
        raise DocumentLockedError("Manuscript is published and locked against further modifications.")

    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        last_lamport, last_version_no = _get_doc_state(conn, doc_id)

        # FR2.6: Lamport clock advancement
        new_ts = max(lamport, last_lamport) + 1
        next_no = last_version_no + 1

        # Milestone versions (submission, revision, published) are ALWAYS stored as snapshots
        is_milestone = reason in ("submission", "revision", "published")
        is_snapshot = is_milestone or force_snapshot or (next_no % SNAPSHOT_EVERY == 1)
        ver_type = "snapshot" if is_snapshot else "delta"

        if is_snapshot:
            content_or_delta = text
        else:
            prev_text, _, _ = rebuild_version(doc_id, last_version_no, db_path=db_path, _conn=conn)
            delta = make_delta(prev_text, text)
            content_or_delta = json.dumps(delta, ensure_ascii=False, separators=(",", ":"))

        sha = _sha256(text)

        # Parent IDs
        if parent_override is not None:
            parent_ids = parent_override
        elif next_no == 1:
            parent_ids = []
        else:
            parent_ids = [last_version_no]

        created_at = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """INSERT INTO versions
               (doc_id, version_no, type, content_or_delta, sha256,
                author_id, lamport_ts, parent_ids, reason, round_no, milestone, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                doc_id, next_no, ver_type, content_or_delta, sha,
                user_id, new_ts,
                json.dumps(parent_ids),
                reason, round_no, milestone, created_at,
            ),
        )

        _update_doc_state(conn, doc_id, new_ts, next_no)

        # Simulated failure: roll back to prove atomicity (FR2.2 demo)
        with _sim_failure_lock:
            global _simulate_failure
            if _simulate_failure:
                _simulate_failure = False  # one-shot
                raise RuntimeError(
                    "Simulated DB failure: rolling back transaction to prove atomicity."
                )

        conn.commit()

        # Audit log for standard checkpoint
        if reason == "checkpoint":
            add_audit(user_id, doc_id, "checkpoint_created", next_no, db_path=db_path)

        return {
            "doc_id": doc_id,
            "version_no": next_no,
            "type": ver_type,
            "sha256": sha,
            "author_id": user_id,
            "lamport_ts": new_ts,
            "parent_ids": parent_ids,
            "reason": reason,
            "round_no": round_no,
            "milestone": milestone,
            "created_at": created_at,
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Core: rebuild_version (FR2.4)
# ---------------------------------------------------------------------------

def rebuild_version(
    doc_id: str,
    version_no: int,
    db_path: Optional[str] = None,
    _conn: Optional[sqlite3.Connection] = None,
) -> tuple[str, bool, dict[str, Any]]:
    """
    Rebuild the full text of *version_no* by finding the nearest earlier snapshot
    and replaying deltas up to the requested version.

    FR2.4 – uses the index idx_versions_doc_ver for efficient lookup.
    """
    own_conn = _conn is None
    conn = get_conn(db_path) if own_conn else _conn
    try:
        snap_row = conn.execute(
            """SELECT version_no, content_or_delta, sha256
               FROM versions
               WHERE doc_id = ? AND version_no <= ? AND type = 'snapshot'
               ORDER BY version_no DESC LIMIT 1""",
            (doc_id, version_no),
        ).fetchone()

        if snap_row is None:
            return "", False, {"error": f"No snapshot found at or before v{version_no}"}

        snap_no = snap_row["version_no"]
        text = snap_row["content_or_delta"]

        delta_rows = conn.execute(
            """SELECT version_no, content_or_delta, sha256
               FROM versions
               WHERE doc_id = ? AND version_no > ? AND version_no <= ? AND type = 'delta'
               ORDER BY version_no ASC""",
            (doc_id, snap_no, version_no),
        ).fetchall()

        deltas_replayed = 0
        for row in delta_rows:
            try:
                delta = json.loads(row["content_or_delta"])
                text = apply_delta(text, delta)
            except (json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
                return text, False, {
                    "snapshot_used": snap_no,
                    "deltas_replayed": deltas_replayed,
                    "error": f"Corrupted delta at v{row['version_no']}: {exc}",
                }
            deltas_replayed += 1

        target_row = conn.execute(
            "SELECT sha256 FROM versions WHERE doc_id = ? AND version_no = ?",
            (doc_id, version_no),
        ).fetchone()

        if target_row is None:
            return text, False, {"error": f"Version {version_no} not found"}

        computed_hash = _sha256(text)
        stored_hash = target_row["sha256"]
        verified = computed_hash == stored_hash

        info: dict[str, Any] = {
            "snapshot_used": snap_no,
            "deltas_replayed": deltas_replayed,
        }
        if not verified:
            info["error"] = (
                f"SHA-256 mismatch: stored={stored_hash[:16]}… "
                f"computed={computed_hash[:16]}…"
            )

        return text, verified, info
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Core: pre_restore_checkpoint (FR2.2)
# ---------------------------------------------------------------------------

def pre_restore_checkpoint(
    doc_id: str,
    user_id: str,
    lamport: int,
    current_text: str,
    round_no: int = 1,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    FR2.2 – Save a safety snapshot of the current editor state before any restore or submission.
    """
    try:
        meta = save_version(
            doc_id=doc_id,
            user_id=user_id,
            lamport=lamport,
            text=current_text,
            reason="safety",
            round_no=round_no,
            db_path=db_path,
        )
        return {"status": "OK", "version_no": meta["version_no"]}
    except Exception as exc:
        return {"status": "FAIL", "error": str(exc)}


# ---------------------------------------------------------------------------
# Core: restore_version (FR2.2, FR2.4)
# ---------------------------------------------------------------------------

def restore_version(
    doc_id: str,
    user_id: str,
    lamport: int,
    target_version_no: int,
    current_text: str,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    FR2.2 – Restore a previous version without touching history.
    """
    if is_locked_published(doc_id, db_path=db_path):
        raise DocumentLockedError("Manuscript is published and locked against further modifications.")

    add_audit(user_id, doc_id, "restore_started", target_version_no, db_path=db_path)

    # Step 1: safety checkpoint (FR2.2)
    cp = pre_restore_checkpoint(doc_id, user_id, lamport, current_text, db_path=db_path)
    if cp["status"] != "OK":
        add_audit(user_id, doc_id, "restore_aborted", target_version_no, db_path=db_path)
        return {
            "status": "ABORTED",
            "reason": "safety_checkpoint_failed",
            "error": cp.get("error", "unknown"),
        }

    safety_version_no = cp["version_no"]

    # Step 2: rebuild + verify (FR2.4)
    text, verified, info = rebuild_version(doc_id, target_version_no, db_path=db_path)
    if not verified:
        add_audit(user_id, doc_id, "restore_aborted", target_version_no, db_path=db_path)
        return {
            "status": "ABORTED",
            "reason": "hash_verification_failed",
            "error": info.get("error", "hash mismatch"),
            "safety_version_no": safety_version_no,
        }

    # Step 3: save restored text as a NEW version
    try:
        new_meta = save_version(
            doc_id=doc_id,
            user_id=user_id,
            lamport=lamport,
            text=text,
            reason="restore",
            parent_override=[target_version_no],
            db_path=db_path,
        )
    except Exception as exc:
        add_audit(user_id, doc_id, "restore_aborted", target_version_no, db_path=db_path)
        return {
            "status": "ABORTED",
            "reason": "restore_write_failed",
            "error": str(exc),
            "safety_version_no": safety_version_no,
        }

    add_audit(user_id, doc_id, "restore_completed", new_meta["version_no"], db_path=db_path)

    return {
        "status": "OK",
        "safety_version_no": safety_version_no,
        "restored_version_no": new_meta["version_no"],
        "parent_target": target_version_no,
        "rebuild_info": info,
    }


# ---------------------------------------------------------------------------
# Core: get_versions (FR2.5)
# ---------------------------------------------------------------------------

def get_versions(
    doc_id: str,
    page: int = 1,
    page_size: int = 10,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """FR2.5 – Return a paginated, newest-first list of versions for *doc_id*."""
    conn = get_conn(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM versions WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0]

        total_pages = max(1, (total + page_size - 1) // page_size)
        offset = (page - 1) * page_size

        rows = conn.execute(
            """SELECT version_no, type, author_id, lamport_ts, sha256, reason,
                      round_no, milestone, parent_ids, created_at,
                      LENGTH(content_or_delta) AS size_bytes
               FROM versions
               WHERE doc_id = ?
               ORDER BY version_no DESC
               LIMIT ? OFFSET ?""",
            (doc_id, page_size, offset),
        ).fetchall()

        items = []
        for r in rows:
            items.append({
                "version_no": r["version_no"],
                "type": r["type"],
                "author_id": r["author_id"],
                "lamport_ts": r["lamport_ts"],
                "sha256_short": r["sha256"][:12],
                "reason": r["reason"],
                "round_no": r["round_no"] or 1,
                "milestone": r["milestone"],
                "parent_ids": json.loads(r["parent_ids"]),
                "created_at": r["created_at"],
                "size_bytes": r["size_bytes"],
            })

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Core: get_stats
# ---------------------------------------------------------------------------

def get_stats(doc_id: str, db_path: Optional[str] = None) -> dict[str, Any]:
    """Return storage efficiency statistics for *doc_id*."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            """SELECT type, sha256, LENGTH(content_or_delta) AS size_bytes
               FROM versions WHERE doc_id = ?""",
            (doc_id,),
        ).fetchall()

        total_versions = len(rows)
        snapshots = sum(1 for r in rows if r["type"] == "snapshot")
        deltas = sum(1 for r in rows if r["type"] == "delta")
        stored_bytes = sum(r["size_bytes"] for r in rows)

        full_text_bytes = 0
        ver_rows = conn.execute(
            "SELECT version_no FROM versions WHERE doc_id = ? ORDER BY version_no",
            (doc_id,),
        ).fetchall()
        for vr in ver_rows:
            text, _, _ = rebuild_version(doc_id, vr["version_no"], db_path=db_path, _conn=conn)
            full_text_bytes += len(text.encode("utf-8"))

        return {
            "total_versions": total_versions,
            "snapshots": snapshots,
            "deltas": deltas,
            "stored_bytes": stored_bytes,
            "full_text_bytes": full_text_bytes,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Audit log (FR4.6)
# ---------------------------------------------------------------------------

def add_audit(
    user_id: str,
    doc_id: str,
    event_type: str,
    version_ref: Optional[int] = None,
    db_path: Optional[str] = None,
) -> None:
    """FR4.6 – Append a row to audit_log. Fire-and-forget; never raises."""
    try:
        conn = get_conn(db_path)
        try:
            conn.execute(
                "INSERT INTO audit_log(user_id,doc_id,event_type,version_ref,time) VALUES(?,?,?,?,?)",
                (user_id, doc_id, event_type, version_ref,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_audit(
    doc_id: str,
    page: int = 1,
    page_size: int = 20,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """FR4.6 – Paginated audit log for *doc_id*, newest first."""
    conn = get_conn(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        offset = (page - 1) * page_size
        rows = conn.execute(
            """SELECT id, user_id, event_type, version_ref, time
               FROM audit_log WHERE doc_id = ?
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            (doc_id, page_size, offset),
        ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "total_pages": total_pages,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Manuscript Workflow Layer
# ---------------------------------------------------------------------------

def is_locked_published(manuscript_id: str, db_path: Optional[str] = None) -> bool:
    """Return True if manuscript exists and is in PUBLISHED state."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM manuscripts WHERE manuscript_id = ?", (manuscript_id,)
        ).fetchone()
        return bool(row and row["status"] == "PUBLISHED")
    finally:
        conn.close()


def get_manuscript(manuscript_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT manuscript_id, title, author_id, status, current_round, created_at FROM manuscripts WHERE manuscript_id = ?",
            (manuscript_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def require_role(
    manuscript_id: str,
    user_id: str,
    role: str,
    allowed_roles: list[str],
    db_path: Optional[str] = None,
) -> None:
    """
    Validate user role and manuscript-level authorization (stand-in for E4).
    If manuscript does not exist, role check is skipped (backwards-compatible with bare docs).
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        return

    # No changes allowed if PUBLISHED
    if m["status"] == "PUBLISHED" and role in ("author", "editor", "reviewer"):
        # Read-only operations might still be authorized by caller; write operations will fail
        pass

    if role not in allowed_roles:
        raise RoleAuthorizationError(
            f"Role '{role}' is not authorized. Allowed roles: {allowed_roles}"
        )

    if role == "author" and m["author_id"] != user_id:
        raise RoleAuthorizationError(
            f"User '{user_id}' is not the author of manuscript '{manuscript_id}'"
        )

    if role == "reviewer":
        conn = get_conn(db_path)
        try:
            assignment = conn.execute(
                """SELECT 1 FROM reviewer_assignments
                   WHERE manuscript_id = ? AND reviewer_id = ?""",
                (manuscript_id, user_id),
            ).fetchone()
            if not assignment:
                raise RoleAuthorizationError(
                    f"Reviewer '{user_id}' is not assigned to manuscript '{manuscript_id}'"
                )
        finally:
            conn.close()


def create_manuscript(
    manuscript_id: str,
    title: str,
    author_id: str,
    initial_text: Optional[str] = None,
    current_round: int = 1,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update a manuscript record in DRAFT status."""
    conn = get_conn(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO manuscripts (manuscript_id, title, author_id, status, current_round, created_at)
               VALUES (?, ?, ?, 'DRAFT', ?, ?)
               ON CONFLICT(manuscript_id) DO UPDATE SET
                   title = excluded.title,
                   author_id = excluded.author_id,
                   status = 'DRAFT',
                   current_round = excluded.current_round,
                   created_at = excluded.created_at""",
            (manuscript_id, title, author_id, current_round, now),
        )
        conn.commit()
    finally:
        conn.close()

    # Save initial version if text provided
    if initial_text:
        save_version(
            doc_id=manuscript_id,
            user_id=author_id,
            lamport=1,
            text=initial_text,
            reason="checkpoint",
            round_no=1,
            db_path=db_path,
        )

    return get_manuscript(manuscript_id, db_path=db_path) or {}


def submit_manuscript(
    manuscript_id: str,
    author_id: str,
    lamport: int,
    text: str,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Author submits manuscript: DRAFT -> SUBMITTED.
    Safety checkpoint of current text is performed first, then milestone snapshot version is saved.
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        raise ValueError(f"Manuscript '{manuscript_id}' not found")
    validate_status_transition(m["status"], "SUBMITTED")

    # Step 1: Safety checkpoint
    pre_restore_checkpoint(
        doc_id=manuscript_id,
        user_id=author_id,
        lamport=lamport,
        current_text=text,
        round_no=1,
        db_path=db_path,
    )

    # Step 2: Milestone snapshot version
    meta = save_version(
        doc_id=manuscript_id,
        user_id=author_id,
        lamport=lamport,
        text=text,
        reason="submission",
        round_no=1,
        milestone="Initial Submission",
        force_snapshot=True,
        parent_override=[],
        db_path=db_path,
    )

    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE manuscripts SET status = 'SUBMITTED' WHERE manuscript_id = ?",
            (manuscript_id,),
        )
        conn.commit()
    finally:
        conn.close()

    add_audit(author_id, manuscript_id, "submitted", meta["version_no"], db_path=db_path)
    return {"manuscript_id": manuscript_id, "status": "SUBMITTED", "version": meta}


def assign_reviewers(
    manuscript_id: str,
    editor_id: str,
    reviewer_ids: list[str],
    round_no: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Editor assigns reviewers: SUBMITTED or REVISED -> UNDER_REVIEW.
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        raise ValueError(f"Manuscript '{manuscript_id}' not found")

    validate_status_transition(m["status"], "UNDER_REVIEW")
    active_round = round_no or m["current_round"]

    conn = get_conn(db_path)
    try:
        for r_id in reviewer_ids:
            conn.execute(
                """INSERT OR REPLACE INTO reviewer_assignments
                   (manuscript_id, reviewer_id, assigned_by, round_no)
                   VALUES (?, ?, ?, ?)""",
                (manuscript_id, r_id, editor_id, active_round),
            )
        conn.execute(
            "UPDATE manuscripts SET status = 'UNDER_REVIEW' WHERE manuscript_id = ?",
            (manuscript_id,),
        )
        conn.commit()
    finally:
        conn.close()

    for r_id in reviewer_ids:
        add_audit(editor_id, manuscript_id, "reviewer_assigned", None, db_path=db_path)

    return {
        "manuscript_id": manuscript_id,
        "status": "UNDER_REVIEW",
        "round_no": active_round,
        "reviewers": reviewer_ids,
    }


def add_review(
    manuscript_id: str,
    reviewer_id: str,
    version_no: int,
    round_no: int,
    comments: str,
    recommendation: str,
    requested_changes: Optional[Any] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Reviewer submits a review during UNDER_REVIEW.
    Must be assigned to the manuscript round.
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        raise ValueError(f"Manuscript '{manuscript_id}' not found")
    if m["status"] != "UNDER_REVIEW":
        raise StatusTransitionError(f"Cannot submit review while manuscript is {m['status']}")

    if requested_changes is None:
        rc_list = []
    elif isinstance(requested_changes, list):
        rc_list = [str(x).strip() for x in requested_changes if str(x).strip()]
    elif isinstance(requested_changes, str):
        try:
            parsed = json.loads(requested_changes)
            if isinstance(parsed, list):
                rc_list = [str(x).strip() for x in parsed if str(x).strip()]
            else:
                rc_list = [requested_changes.strip()] if requested_changes.strip() else []
        except Exception:
            rc_list = [requested_changes.strip()] if requested_changes.strip() else []
    else:
        rc_list = []
    rc_json = json.dumps(rc_list)

    conn = get_conn(db_path)
    try:
        assignment = conn.execute(
            """SELECT 1 FROM reviewer_assignments
               WHERE manuscript_id = ? AND reviewer_id = ? AND round_no = ?""",
            (manuscript_id, reviewer_id, round_no),
        ).fetchone()
        if not assignment:
            raise RoleAuthorizationError(
                f"Reviewer '{reviewer_id}' is not assigned to round {round_no} of manuscript '{manuscript_id}'"
            )

        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO reviews (manuscript_id, version_no, round_no, reviewer_id, comments, recommendation, requested_changes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (manuscript_id, version_no, round_no, reviewer_id, comments, recommendation, rc_json, now),
        )
        review_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    add_audit(reviewer_id, manuscript_id, "review_submitted", version_no, db_path=db_path)
    return {
        "review_id": review_id,
        "manuscript_id": manuscript_id,
        "version_no": version_no,
        "round_no": round_no,
        "reviewer_id": reviewer_id,
        "recommendation": recommendation,
        "requested_changes": rc_list,
    }


def get_reviews(
    manuscript_id: str,
    round_no: Optional[int] = None,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Retrieve all reviews for a manuscript, optionally filtered by round."""
    conn = get_conn(db_path)
    try:
        if round_no:
            rows = conn.execute(
                """SELECT review_id, manuscript_id, version_no, round_no, reviewer_id, comments, recommendation, requested_changes, created_at
                   FROM reviews WHERE manuscript_id = ? AND round_no = ? ORDER BY review_id ASC""",
                (manuscript_id, round_no),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT review_id, manuscript_id, version_no, round_no, reviewer_id, comments, recommendation, requested_changes, created_at
                   FROM reviews WHERE manuscript_id = ? ORDER BY round_no ASC, review_id ASC""",
                (manuscript_id,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            rc = d.get("requested_changes", "[]")
            if isinstance(rc, str):
                try:
                    d["requested_changes"] = json.loads(rc)
                except Exception:
                    d["requested_changes"] = []
            else:
                d["requested_changes"] = rc or []
            result.append(d)
        return result
    finally:
        conn.close()


def get_review_summary(
    manuscript_id: str,
    round_no: Optional[int] = None,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Editor-only review summary for a given round (defaults to current_round).
    Returns each assigned reviewer with status ('submitted' or 'pending'),
    recommendation, comments, and requested_changes.
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        raise ValueError(f"Manuscript '{manuscript_id}' not found")

    target_round = round_no if round_no is not None else m["current_round"]
    conn = get_conn(db_path)
    try:
        assigned_rows = conn.execute(
            """SELECT reviewer_id FROM reviewer_assignments
               WHERE manuscript_id = ? AND round_no = ?
               ORDER BY reviewer_id ASC""",
            (manuscript_id, target_round),
        ).fetchall()

        review_rows = conn.execute(
            """SELECT reviewer_id, recommendation, comments, requested_changes
               FROM reviews WHERE manuscript_id = ? AND round_no = ?""",
            (manuscript_id, target_round),
        ).fetchall()

        reviews_by_user = {}
        for r in review_rows:
            d = dict(r)
            rc = d.get("requested_changes", "[]")
            if isinstance(rc, str):
                try:
                    d["requested_changes"] = json.loads(rc)
                except Exception:
                    d["requested_changes"] = []
            else:
                d["requested_changes"] = rc or []
            reviews_by_user[d["reviewer_id"]] = d

        summary = []
        for a in assigned_rows:
            rid = a["reviewer_id"]
            if rid in reviews_by_user:
                rev = reviews_by_user[rid]
                summary.append({
                    "reviewer_id": rid,
                    "round_no": target_round,
                    "status": "submitted",
                    "recommendation": rev["recommendation"],
                    "comments": rev["comments"],
                    "requested_changes": rev["requested_changes"],
                })
            else:
                summary.append({
                    "reviewer_id": rid,
                    "round_no": target_round,
                    "status": "pending",
                    "recommendation": None,
                    "comments": None,
                    "requested_changes": [],
                })
        return summary
    finally:
        conn.close()


def request_revision(
    manuscript_id: str,
    editor_id: str,
    comments: Optional[str] = None,
    revision_notes: Optional[str] = None,
    override: bool = False,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Editor requests revision: UNDER_REVIEW -> REVISION_REQUESTED."""
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        raise ValueError(f"Manuscript '{manuscript_id}' not found")
    validate_status_transition(m["status"], "REVISION_REQUESTED")

    notes = revision_notes if revision_notes is not None else comments

    conn = get_conn(db_path)
    try:
        # Check pending reviews for current round
        assigned_rows = conn.execute(
            "SELECT reviewer_id FROM reviewer_assignments WHERE manuscript_id = ? AND round_no = ?",
            (manuscript_id, m["current_round"]),
        ).fetchall()
        assigned = [r["reviewer_id"] for r in assigned_rows]

        reviewed_rows = conn.execute(
            "SELECT reviewer_id FROM reviews WHERE manuscript_id = ? AND round_no = ?",
            (manuscript_id, m["current_round"]),
        ).fetchall()
        reviewed = {r["reviewer_id"] for r in reviewed_rows}

        pending = [r for r in assigned if r not in reviewed]
        if pending and not override:
            raise StatusTransitionError(
                f"Cannot request revision: reviews pending from {pending}. Tick 'Override: decide without all reviews' to proceed."
            )

        last_row = conn.execute(
            "SELECT last_version_no FROM doc_state WHERE doc_id = ?", (manuscript_id,)
        ).fetchone()
        v_no = last_row["last_version_no"] if last_row else 1

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO decisions
               (manuscript_id, round_no, editor_id, decision, version_no, revision_notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (manuscript_id, m["current_round"], editor_id, "REVISION_REQUESTED", v_no, notes, now),
        )
        conn.execute(
            "UPDATE manuscripts SET status = 'REVISION_REQUESTED' WHERE manuscript_id = ?",
            (manuscript_id,),
        )
        conn.commit()
    finally:
        conn.close()

    if pending and override:
        add_audit(editor_id, manuscript_id, "editor_override_decision", v_no, db_path=db_path)

    add_audit(editor_id, manuscript_id, "revision_requested", v_no, db_path=db_path)
    return {
        "manuscript_id": manuscript_id,
        "status": "REVISION_REQUESTED",
        "comments": notes,
        "revision_notes": notes,
    }


def submit_revision(
    manuscript_id: str,
    author_id: str,
    lamport: int,
    text: str,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Author submits revision: REVISION_REQUESTED -> REVISED.
    Safety checkpoint is performed first, then milestone snapshot version is saved with parent = previous submitted version.
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        raise ValueError(f"Manuscript '{manuscript_id}' not found")
    validate_status_transition(m["status"], "REVISED")

    conn = get_conn(db_path)
    try:
        # Find previous submitted version
        prev_row = conn.execute(
            """SELECT version_no FROM versions
               WHERE doc_id = ? AND reason IN ('submission', 'revision')
               ORDER BY version_no DESC LIMIT 1""",
            (manuscript_id,),
        ).fetchone()
        prev_sub_ver = prev_row["version_no"] if prev_row else None
    finally:
        conn.close()

    parent_ids = [prev_sub_ver] if prev_sub_ver is not None else []
    new_round = m["current_round"] + 1

    # Step 1: Safety checkpoint
    pre_restore_checkpoint(
        doc_id=manuscript_id,
        user_id=author_id,
        lamport=lamport,
        current_text=text,
        round_no=m["current_round"],
        db_path=db_path,
    )

    # Step 2: Milestone snapshot version
    milestone_label = f"Revision R{new_round - 1}"
    meta = save_version(
        doc_id=manuscript_id,
        user_id=author_id,
        lamport=lamport,
        text=text,
        reason="revision",
        round_no=new_round,
        milestone=milestone_label,
        force_snapshot=True,
        parent_override=parent_ids,
        db_path=db_path,
    )

    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE manuscripts SET status = 'REVISED', current_round = ? WHERE manuscript_id = ?",
            (new_round, manuscript_id),
        )
        conn.commit()
    finally:
        conn.close()

    add_audit(author_id, manuscript_id, "revision_submitted", meta["version_no"], db_path=db_path)
    return {"manuscript_id": manuscript_id, "status": "REVISED", "round_no": new_round, "version": meta}


def make_decision(
    manuscript_id: str,
    editor_id: str,
    decision: str,
    override: bool = False,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Editor makes decision:
      UNDER_REVIEW -> ACCEPTED or REJECTED
      SUBMITTED -> REJECTED (desk-reject)
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        raise ValueError(f"Manuscript '{manuscript_id}' not found")

    decision_clean = decision.upper()
    target_status = "ACCEPTED" if decision_clean == "ACCEPT" else "REJECTED"
    validate_status_transition(m["status"], target_status)

    is_desk_reject = (m["status"] == "SUBMITTED" and target_status == "REJECTED")

    conn = get_conn(db_path)
    try:
        pending = []
        if m["status"] == "UNDER_REVIEW":
            # Check pending reviews for current round
            assigned_rows = conn.execute(
                "SELECT reviewer_id FROM reviewer_assignments WHERE manuscript_id = ? AND round_no = ?",
                (manuscript_id, m["current_round"]),
            ).fetchall()
            assigned = [r["reviewer_id"] for r in assigned_rows]

            reviewed_rows = conn.execute(
                "SELECT reviewer_id FROM reviews WHERE manuscript_id = ? AND round_no = ?",
                (manuscript_id, m["current_round"]),
            ).fetchall()
            reviewed = {r["reviewer_id"] for r in reviewed_rows}

            pending = [r for r in assigned if r not in reviewed]
            if pending and not override:
                raise StatusTransitionError(
                    f"Cannot issue decision: reviews pending from {pending}. Tick 'Override: decide without all reviews' to proceed."
                )

        # Get latest version number
        last_row = conn.execute(
            "SELECT last_version_no FROM doc_state WHERE doc_id = ?", (manuscript_id,)
        ).fetchone()
        v_no = last_row["last_version_no"] if last_row else 1

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO decisions
               (manuscript_id, round_no, editor_id, decision, version_no, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (manuscript_id, m["current_round"], editor_id, decision_clean, v_no, now),
        )
        conn.execute(
            "UPDATE manuscripts SET status = ? WHERE manuscript_id = ?",
            (target_status, manuscript_id),
        )
        conn.commit()
    finally:
        conn.close()

    if pending and override:
        add_audit(editor_id, manuscript_id, "editor_override_decision", v_no, db_path=db_path)

    event = "desk_rejected" if is_desk_reject else ("accepted" if target_status == "ACCEPTED" else "rejected")
    add_audit(editor_id, manuscript_id, event, v_no, db_path=db_path)
    return {"manuscript_id": manuscript_id, "status": target_status, "decision": decision_clean}


def publish_manuscript(
    manuscript_id: str,
    editor_id: str,
    lamport: int = 1,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Editor publishes manuscript: ACCEPTED -> PUBLISHED.
    Creates a published snapshot milestone version, and locks the manuscript permanently.
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        raise ValueError(f"Manuscript '{manuscript_id}' not found")
    validate_status_transition(m["status"], "PUBLISHED")

    # Rebuild latest version to obtain final published text
    last_row = None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT last_version_no FROM doc_state WHERE doc_id = ?", (manuscript_id,)
        ).fetchone()
        last_ver_no = row["last_version_no"] if row else 1
    finally:
        conn.close()

    final_text, _, _ = rebuild_version(manuscript_id, last_ver_no, db_path=db_path)

    # Save milestone snapshot for Published Version
    meta = save_version(
        doc_id=manuscript_id,
        user_id=editor_id,
        lamport=lamport,
        text=final_text,
        reason="published",
        round_no=m["current_round"],
        milestone="Published Version",
        force_snapshot=True,
        parent_override=[last_ver_no],
        db_path=db_path,
    )

    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE manuscripts SET status = 'PUBLISHED' WHERE manuscript_id = ?",
            (manuscript_id,),
        )
        conn.commit()
    finally:
        conn.close()

    add_audit(editor_id, manuscript_id, "published", meta["version_no"], db_path=db_path)
    return {"manuscript_id": manuscript_id, "status": "PUBLISHED", "version": meta}


def compare_versions(
    doc_id: str,
    ver_a: int,
    ver_b: int,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Compare two versions using difflib.
    Labelled as the mocked E3 diff engine.
    """
    text_a, verif_a, _ = rebuild_version(doc_id, ver_a, db_path=db_path)
    text_b, verif_b, _ = rebuild_version(doc_id, ver_b, db_path=db_path)

    lines_a = text_a.splitlines(keepends=True)
    lines_b = text_b.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(lines_a, lines_b, fromfile=f"v{ver_a}", tofile=f"v{ver_b}"))

    # Also build HTML chunks for rich rendering (green additions, red deletions)
    sm = difflib.SequenceMatcher(None, text_a, text_b)
    chunks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            chunks.append({"type": "equal", "text": text_a[i1:i2]})
        elif tag == "delete":
            chunks.append({"type": "delete", "text": text_a[i1:i2]})
        elif tag == "insert":
            chunks.append({"type": "insert", "text": text_b[j1:j2]})
        elif tag == "replace":
            chunks.append({"type": "delete", "text": text_a[i1:i2]})
            chunks.append({"type": "insert", "text": text_b[j1:j2]})

    return {
        "engine": "Mocked E3 Diff Engine (difflib)",
        "doc_id": doc_id,
        "version_a": ver_a,
        "version_b": ver_b,
        "verified_a": verif_a,
        "verified_b": verif_b,
        "diff_unified": "".join(diff_lines),
        "diff_chunks": chunks,
    }


def get_workflow(
    manuscript_id: str,
    user_id: Optional[str] = None,
    role: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Return complete workflow status, rounds, reviews, decisions, and allowed actions for UI.
    """
    m = get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        return {
            "manuscript": None,
            "assignments": [],
            "reviews": [],
            "decisions": [],
            "allowed_actions": ["checkpoint", "submit"] if role == "author" else [],
            "is_published": False,
        }

    conn = get_conn(db_path)
    try:
        assignments = conn.execute(
            "SELECT reviewer_id, assigned_by, round_no FROM reviewer_assignments WHERE manuscript_id = ? ORDER BY round_no",
            (manuscript_id,),
        ).fetchall()
        review_rows = conn.execute(
            "SELECT review_id, version_no, round_no, reviewer_id, comments, recommendation, requested_changes, created_at FROM reviews WHERE manuscript_id = ? ORDER BY round_no, review_id",
            (manuscript_id,),
        ).fetchall()
        decision_rows = conn.execute(
            "SELECT round_no, editor_id, decision, version_no, revision_notes, created_at FROM decisions WHERE manuscript_id = ? ORDER BY round_no",
            (manuscript_id,),
        ).fetchall()
    finally:
        conn.close()

    reviews = []
    for r in review_rows:
        d = dict(r)
        rc = d.get("requested_changes", "[]")
        if isinstance(rc, str):
            try:
                d["requested_changes"] = json.loads(rc)
            except Exception:
                d["requested_changes"] = []
        else:
            d["requested_changes"] = rc or []
        reviews.append(d)

    decisions = [dict(d) for d in decision_rows]

    # Find revision notes for author view
    current_revision_notes = None
    if m:
        for d in reversed(decisions):
            if d.get("decision") == "REVISION_REQUESTED" and d.get("round_no") == m["current_round"]:
                current_revision_notes = d.get("revision_notes")
                break
        if current_revision_notes is None:
            for d in reversed(decisions):
                if d.get("revision_notes"):
                    current_revision_notes = d.get("revision_notes")
                    break
        m["revision_notes"] = current_revision_notes

    status = m["status"]
    is_published = status == "PUBLISHED"

    # Compute allowed actions based on status and role
    allowed_actions = []
    if not is_published:
        if role == "author" and (user_id is None or m["author_id"] == user_id):
            if status == "DRAFT":
                allowed_actions.extend(["checkpoint", "submit"])
            elif status == "REVISION_REQUESTED":
                allowed_actions.extend(["checkpoint", "submit_revision"])
            allowed_actions.append("restore")
        elif role == "editor":
            if status in ("SUBMITTED", "REVISED"):
                allowed_actions.append("assign_reviewers")
            elif status == "UNDER_REVIEW":
                allowed_actions.extend(["request_revision", "decision_accept", "decision_reject"])
            elif status == "ACCEPTED":
                allowed_actions.append("publish")
        elif role == "reviewer":
            if status == "UNDER_REVIEW":
                # Check if this reviewer is assigned to current round
                assigned_rounds = [a["round_no"] for a in assignments if a["reviewer_id"] == user_id]
                if m["current_round"] in assigned_rounds:
                    allowed_actions.append("add_review")

    # Build reviewer recommendations summary for current round
    reviewer_summary = []
    if m:
        curr_round = m["current_round"]
        curr_assignments = [a for a in assignments if a["round_no"] == curr_round]
        curr_reviews = {r["reviewer_id"]: r for r in reviews if r["round_no"] == curr_round}
        for a in curr_assignments:
            rid = a["reviewer_id"]
            rev = curr_reviews.get(rid)
            reviewer_summary.append({
                "reviewer_id": rid,
                "round_no": curr_round,
                "status": "submitted" if rev else "pending",
                "recommendation": rev["recommendation"] if rev else None,
                "comments": rev["comments"] if rev else None,
                "requested_changes": rev["requested_changes"] if rev else [],
            })

    return {
        "manuscript": m,
        "revision_notes": current_revision_notes,
        "current_revision_notes": current_revision_notes,
        "assignments": [dict(a) for a in assignments],
        "reviews": reviews,
        "decisions": decisions,
        "reviewer_summary": reviewer_summary,
        "allowed_actions": allowed_actions,
        "is_published": is_published,
    }


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

def corrupt_version(
    doc_id: str,
    version_no: int,
    db_path: Optional[str] = None,
) -> bool:
    """Flip a character in content_or_delta for the given version (demo only)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT version_id, content_or_delta FROM versions WHERE doc_id=? AND version_no=?",
            (doc_id, version_no),
        ).fetchone()
        if row is None:
            return False
        original: str = row["content_or_delta"]
        corrupted = ("X" if original[0] != "X" else "Y") + original[1:]
        conn.execute(
            "UPDATE versions SET content_or_delta=? WHERE version_id=?",
            (corrupted, row["version_id"]),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def reset_document(doc_id: str, db_path: Optional[str] = None) -> None:
    """
    Delete all data for *doc_id* (versions, doc_state, audit_log, reviewer_assignments,
    reviews, decisions, and the manuscripts row for that ID), then re-insert a fresh
    manuscripts row (status DRAFT, current_round 0, author_id = 'alice') inside ONE transaction.
    """
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM versions WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM doc_state WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM audit_log WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM reviewer_assignments WHERE manuscript_id=?", (doc_id,))
        conn.execute("DELETE FROM reviews WHERE manuscript_id=?", (doc_id,))
        conn.execute("DELETE FROM decisions WHERE manuscript_id=?", (doc_id,))
        conn.execute("DELETE FROM manuscripts WHERE manuscript_id=?", (doc_id,))

        # Re-insert fresh manuscripts row so the ID remains usable
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO manuscripts (manuscript_id, title, author_id, status, current_round, created_at)
               VALUES (?, ?, 'alice', 'DRAFT', 0, ?)""",
            (doc_id, doc_id, now),
        )
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
