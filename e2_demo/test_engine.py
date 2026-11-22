"""
test_engine.py – pytest test suite for VersionVault E2 engine.

Uses a temporary SQLite file per test (via tmp_path fixture) so tests are
fully isolated and can be run in any order.

Run:  pytest test_engine.py -v
"""

import json
import random
import string
import sqlite3

import pytest

import engine
from db import init_db


# ---------------------------------------------------------------------------
# Fixture: isolated DB per test
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Create and initialise a fresh SQLite DB for each test."""
    path = str(tmp_path / "test.db")
    init_db(db_path=path)
    return path


def _rand_str(n: int) -> str:
    """Return a random ASCII string of length n."""
    return "".join(random.choices(string.printable, k=n))


# ---------------------------------------------------------------------------
# Delta round-trip tests
# ---------------------------------------------------------------------------

class TestDeltaRoundTrip:
    """FR2.3 – make_delta / apply_delta must be perfect inverses for any strings."""

    def test_identical_strings(self):
        old = "hello world"
        delta = engine.make_delta(old, old)
        assert engine.apply_delta(old, delta) == old

    def test_empty_to_nonempty(self):
        old, new = "", "hello"
        delta = engine.make_delta(old, new)
        assert engine.apply_delta(old, delta) == new

    def test_nonempty_to_empty(self):
        old, new = "hello", ""
        delta = engine.make_delta(old, new)
        assert engine.apply_delta(old, delta) == new

    def test_small_edit(self):
        old = "The quick brown fox"
        new = "The quick red fox"
        delta = engine.make_delta(old, new)
        assert engine.apply_delta(old, delta) == new

    @pytest.mark.parametrize("seed", range(20))
    def test_random_strings(self, seed):
        random.seed(seed)
        old = _rand_str(random.randint(0, 200))
        new = _rand_str(random.randint(0, 200))
        delta = engine.make_delta(old, new)
        assert engine.apply_delta(old, delta) == new

    def test_unicode_content(self):
        old = "Héllo wörld"
        new = "Héllo wörld – café"
        delta = engine.make_delta(old, new)
        assert engine.apply_delta(old, delta) == new


# ---------------------------------------------------------------------------
# Snapshot cadence tests (FR2.3)
# ---------------------------------------------------------------------------

class TestSnapshotCadence:
    """Snapshots must be created at version_no % SNAPSHOT_EVERY == 1."""

    def _save(self, db, doc="doc-snap", user="u1", n=1):
        """Save n versions and return their metadata list."""
        results = []
        for i in range(n):
            meta = engine.save_version(doc, user, i, f"text version {i}", db_path=db)
            results.append(meta)
        return results

    def test_first_version_is_snapshot(self, db):
        meta = engine.save_version("doc-s", "u1", 0, "first", db_path=db)
        assert meta["type"] == "snapshot"

    def test_snapshot_every_N(self, db):
        N = engine.SNAPSHOT_EVERY  # 5
        metas = self._save(db, n=2 * N)
        for m in metas:
            expected = "snapshot" if (m["version_no"] % N == 1) else "delta"
            assert m["type"] == expected, (
                f"v{m['version_no']}: expected {expected}, got {m['type']}"
            )

    def test_second_version_is_delta(self, db):
        self._save(db, n=1)
        meta = engine.save_version("doc-snap", "u1", 1, "second text", db_path=db)
        assert meta["type"] == "delta"


# ---------------------------------------------------------------------------
# Rebuild and hash verification (FR2.4)
# ---------------------------------------------------------------------------

class TestRebuild:
    """Every saved version must be exactly rebuilable with SHA-256 verification."""

    def test_rebuild_all_versions(self, db):
        texts = [
            "Initial draft of the proposal.",
            "Initial draft of the proposal. Added section 1.",
            "Initial draft of the proposal. Added section 1 and section 2.",
            "Initial draft. Added sections 1, 2, 3.",
            "Revised draft. Added sections 1, 2, 3.",
            "Revised draft with new introduction. Added sections 1, 2, 3.",
        ]
        saved = []
        for i, t in enumerate(texts):
            meta = engine.save_version("doc-r", "u1", i, t, db_path=db)
            saved.append((meta["version_no"], t))

        for ver_no, original_text in saved:
            rebuilt, verified, info = engine.rebuild_version("doc-r", ver_no, db_path=db)
            assert rebuilt == original_text, f"v{ver_no}: text mismatch"
            assert verified, f"v{ver_no}: SHA-256 failed – {info}"

    def test_rebuild_nonexistent_returns_false(self, db):
        _, verified, info = engine.rebuild_version("doc-none", 99, db_path=db)
        assert not verified
        assert "error" in info


# ---------------------------------------------------------------------------
# Corruption detection (FR2.3, FR2.4)
# ---------------------------------------------------------------------------

class TestCorruption:
    def test_corrupted_delta_fails_verification(self, db):
        engine.save_version("doc-c", "u1", 0, "original text here", db_path=db)
        engine.save_version("doc-c", "u1", 1, "modified text here!", db_path=db)

        # Corrupt version 2 (which is a delta)
        ok = engine.corrupt_version("doc-c", 2, db_path=db)
        assert ok

        _, verified, info = engine.rebuild_version("doc-c", 2, db_path=db)
        assert not verified
        assert "error" in info

    def test_corrupted_snapshot_fails_verification(self, db):
        engine.save_version("doc-cs", "u1", 0, "snapshot text ABC", db_path=db)

        engine.corrupt_version("doc-cs", 1, db_path=db)
        _, verified, info = engine.rebuild_version("doc-cs", 1, db_path=db)
        assert not verified


# ---------------------------------------------------------------------------
# Simulated DB failure / rollback (FR2.2)
# ---------------------------------------------------------------------------

class TestAtomicRollback:
    def test_simulated_failure_leaves_db_unchanged(self, db):
        # Save version 1 successfully
        engine.save_version("doc-f", "u1", 0, "initial", db_path=db)

        # Enable simulate failure
        engine.set_simulate_failure(True)

        # Attempt save version 2 – should raise
        with pytest.raises(RuntimeError, match="Simulated DB failure"):
            engine.save_version("doc-f", "u1", 1, "after failure", db_path=db)

        # DB must still show only version 1
        result = engine.get_versions("doc-f", db_path=db)
        assert result["total"] == 1
        assert result["items"][0]["version_no"] == 1

    def test_flag_is_cleared_after_failure(self, db):
        engine.save_version("doc-flag", "u1", 0, "v1", db_path=db)
        engine.set_simulate_failure(True)
        try:
            engine.save_version("doc-flag", "u1", 1, "v2-fail", db_path=db)
        except RuntimeError:
            pass
        # Flag should be cleared; next save should succeed
        meta = engine.save_version("doc-flag", "u1", 2, "v2-ok", db_path=db)
        assert meta["version_no"] == 2


# ---------------------------------------------------------------------------
# pre_restore_checkpoint failure stops restore (FR2.2)
# ---------------------------------------------------------------------------

class TestRestoreAbort:
    def test_restore_aborted_when_safety_checkpoint_fails(self, db):
        engine.save_version("doc-ra", "u1", 0, "v1 text", db_path=db)
        engine.save_version("doc-ra", "u1", 1, "v2 text", db_path=db)

        # Force failure on next write (will hit the safety checkpoint)
        engine.set_simulate_failure(True)
        result = engine.restore_version(
            "doc-ra", "u1", 2, 1, "current editor text", db_path=db
        )
        assert result["status"] == "ABORTED"
        assert result["reason"] == "safety_checkpoint_failed"

        # DB must still have exactly 2 versions
        vr = engine.get_versions("doc-ra", db_path=db)
        assert vr["total"] == 2


# ---------------------------------------------------------------------------
# Restore correctness (FR2.2)
# ---------------------------------------------------------------------------

class TestRestore:
    def test_restore_creates_new_version_with_correct_parent(self, db):
        texts = ["alpha", "bravo", "charlie", "delta", "echo"]
        for i, t in enumerate(texts):
            engine.save_version("doc-res", "u1", i, t, db_path=db)

        # Restore version 2 ("bravo")
        result = engine.restore_version("doc-res", "u1", 5, 2, "echo", db_path=db)
        assert result["status"] == "OK"

        restored_no = result["restored_version_no"]
        safety_no = result["safety_version_no"]

        # History still intact
        vr = engine.get_versions("doc-res", db_path=db)
        version_nos = {v["version_no"] for v in vr["items"]}
        for i in range(1, 6):
            assert i in version_nos, f"Original v{i} disappeared"

        # Safety version has reason='safety'
        safety_items = [v for v in vr["items"] if v["version_no"] == safety_no]
        assert safety_items and safety_items[0]["reason"] == "safety"

        # Restored version has parent = [2]
        restored_items = [v for v in vr["items"] if v["version_no"] == restored_no]
        assert restored_items
        assert 2 in restored_items[0]["parent_ids"]

        # Rebuilt text of restored version == original v2 text
        text, verified, _ = engine.rebuild_version("doc-res", restored_no, db_path=db)
        assert text == "bravo"
        assert verified

    def test_restore_does_not_delete_old_versions(self, db):
        for i in range(3):
            engine.save_version("doc-nd", "u1", i, f"v{i+1}", db_path=db)
        engine.restore_version("doc-nd", "u1", 3, 1, "v3", db_path=db)
        vr = engine.get_versions("doc-nd", db_path=db)
        # Should have 3 original + 1 safety + 1 restore = 5
        assert vr["total"] == 5


# ---------------------------------------------------------------------------
# Lamport timestamps (FR2.6)
# ---------------------------------------------------------------------------

class TestLamport:
    def test_lamport_strictly_increasing(self, db):
        metas = []
        for i in range(8):
            m = engine.save_version("doc-l", "u1", i, f"text {i}", db_path=db)
            metas.append(m)
        ts_list = [m["lamport_ts"] for m in metas]
        assert ts_list == sorted(ts_list), "Lamport timestamps not strictly increasing"
        assert len(set(ts_list)) == len(ts_list), "Duplicate Lamport timestamps"

    def test_lamport_uses_max_of_received_and_last(self, db):
        # Send a large lamport value
        engine.save_version("doc-lm", "u1", 0, "v1", db_path=db)
        meta = engine.save_version("doc-lm", "u1", 1000, "v2 high lamport", db_path=db)
        # new_ts = max(1000, last) + 1; last was 1, so new_ts = 1001
        assert meta["lamport_ts"] == 1001

    def test_lamport_low_received_uses_last_plus_one(self, db):
        engine.save_version("doc-ll", "u1", 500, "v1 big", db_path=db)
        meta = engine.save_version("doc-ll", "u1", 0, "v2 small lamport", db_path=db)
        # new_ts = max(0, 501) + 1 = 502
        assert meta["lamport_ts"] == 502


# ---------------------------------------------------------------------------
# Pagination (FR2.5)
# ---------------------------------------------------------------------------

class TestPagination:
    def test_pagination_counts(self, db):
        for i in range(15):
            engine.save_version("doc-p", "u1", i, f"version text {i}", db_path=db)

        page1 = engine.get_versions("doc-p", page=1, page_size=10, db_path=db)
        assert page1["total"] == 15
        assert page1["total_pages"] == 2
        assert len(page1["items"]) == 10

        page2 = engine.get_versions("doc-p", page=2, page_size=10, db_path=db)
        assert len(page2["items"]) == 5

    def test_newest_first_order(self, db):
        for i in range(5):
            engine.save_version("doc-ord", "u1", i, f"ver {i}", db_path=db)
        result = engine.get_versions("doc-ord", db_path=db)
        nos = [v["version_no"] for v in result["items"]]
        assert nos == sorted(nos, reverse=True)

    def test_audit_pagination(self, db):
        for i in range(25):
            engine.save_version("doc-aud", "u1", i, f"ver {i}", db_path=db)
        page1 = engine.get_audit("doc-aud", page=1, page_size=20, db_path=db)
        assert page1["total"] >= 25
        assert page1["total_pages"] >= 2



# ---------------------------------------------------------------------------
# Manuscript Workflow & Governance Tests
# ---------------------------------------------------------------------------

class TestManuscriptWorkflow:
    def test_milestone_versions_always_snapshots(self, db):
        """Milestone versions (submission, revision, published) MUST always be snapshots."""
        mid = "ms-snap-1"
        engine.create_manuscript(mid, "Quantum Entanglement", "alice", initial_text="Draft 1", db_path=db)

        # v1 is draft (snapshot since v=1)
        v1_meta = engine.get_versions(mid, db_path=db)["items"][0]
        assert v1_meta["type"] == "snapshot"

        # Save an edit -> v2 (normally delta with SNAPSHOT_EVERY=5)
        v2 = engine.save_version(mid, "alice", 2, "Draft 2 edit", db_path=db)
        assert v2["type"] == "delta"

        # Submit manuscript -> creates safety checkpoint (delta) and submission (snapshot!)
        sub_res = engine.submit_manuscript(mid, "alice", 3, "Submitted text", db_path=db)
        sub_ver = sub_res["version"]
        assert sub_ver["reason"] == "submission"
        assert sub_ver["type"] == "snapshot"
        assert sub_ver["milestone"] == "Initial Submission"

        # Move to REVISION_REQUESTED
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(mid, "rita", 2, 1, "Major revision needed", "major", db_path=db)
        engine.request_revision(mid, "eric", "Please clarify section 3", db_path=db)

        # Submit revision -> creates safety (delta) and revision milestone (must be snapshot!)
        rev_res = engine.submit_revision(mid, "alice", 4, "Revised text with section 3 clarified", db_path=db)
        rev_ver = rev_res["version"]
        assert rev_ver["reason"] == "revision"
        assert rev_ver["type"] == "snapshot"
        assert "Revision R" in rev_ver["milestone"]

        # Accept & Publish
        engine.assign_reviewers(mid, "eric", ["rita"], round_no=2, db_path=db)
        engine.add_review(mid, "rita", 4, 2, "Looks great now", "accept", db_path=db)
        engine.make_decision(mid, "eric", "accept", db_path=db)
        pub_res = engine.publish_manuscript(mid, "eric", lamport=5, db_path=db)
        pub_ver = pub_res["version"]
        assert pub_ver["reason"] == "published"
        assert pub_ver["type"] == "snapshot"
        assert pub_ver["milestone"] == "Published Version"

    def test_illegal_status_transitions(self, db):
        """Illegal state machine transitions must be rejected."""
        mid = "ms-transit-1"
        engine.create_manuscript(mid, "Neural Networks", "alice", initial_text="Draft text", db_path=db)

        # In DRAFT: cannot assign reviewers directly
        with pytest.raises(engine.StatusTransitionError):
            engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)

        # In DRAFT: cannot make decision directly
        with pytest.raises(engine.StatusTransitionError):
            engine.make_decision(mid, "eric", "accept", db_path=db)

        # In DRAFT: cannot publish directly
        with pytest.raises(engine.StatusTransitionError):
            engine.publish_manuscript(mid, "eric", db_path=db)

        # Submit to SUBMITTED
        engine.submit_manuscript(mid, "alice", 2, "Submitted text", db_path=db)

        # In SUBMITTED: cannot submit revision
        with pytest.raises(engine.StatusTransitionError):
            engine.submit_revision(mid, "alice", 3, "Premature revision", db_path=db)

        # In SUBMITTED: cannot publish
        with pytest.raises(engine.StatusTransitionError):
            engine.publish_manuscript(mid, "eric", db_path=db)

    def test_reviewer_access_limits(self, db):
        """Reviewers can only access submitted versions of manuscripts they are assigned to."""
        mid = "ms-rev-access-1"
        engine.create_manuscript(mid, "Blockchain Security", "alice", initial_text="Secret draft", db_path=db)

        # Unassigned reviewer Rita should be rejected
        with pytest.raises(engine.RoleAuthorizationError):
            engine.require_role(mid, "rita", "reviewer", ["reviewer"], db_path=db)

        # Submit
        engine.submit_manuscript(mid, "alice", 2, "Public submitted text", db_path=db)

        # Editor assigns Rita
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)

        # Rita is now assigned
        engine.require_role(mid, "rita", "reviewer", ["reviewer"], db_path=db)

        # Raj is still unassigned
        with pytest.raises(engine.RoleAuthorizationError):
            engine.require_role(mid, "raj", "reviewer", ["reviewer"], db_path=db)

    def test_review_tied_to_version_and_round(self, db):
        """Review must be linked to active round, reviewer assignment, and specific version."""
        mid = "ms-review-tie-1"
        engine.create_manuscript(mid, "CRISPR Editing", "alice", initial_text="Draft", db_path=db)
        sub_res = engine.submit_manuscript(mid, "alice", 2, "Version for review", db_path=db)
        sub_vno = sub_res["version"]["version_no"]

        # Assign Rita for round 1
        engine.assign_reviewers(mid, "eric", ["rita"], round_no=1, db_path=db)

        # Rita submits review
        rev = engine.add_review(
            manuscript_id=mid,
            reviewer_id="rita",
            version_no=sub_vno,
            round_no=1,
            comments="Well researched, recommend minor revisions.",
            recommendation="minor",
            db_path=db,
        )
        assert rev["recommendation"] == "minor"
        assert rev["round_no"] == 1
        assert rev["version_no"] == sub_vno

        # Verify review can be retrieved by round
        r1_reviews = engine.get_reviews(mid, round_no=1, db_path=db)
        assert len(r1_reviews) == 1
        assert r1_reviews[0]["comments"] == "Well researched, recommend minor revisions."

        # Unassigned reviewer Raj cannot submit review for round 1
        with pytest.raises(engine.RoleAuthorizationError):
            engine.add_review(mid, "raj", sub_vno, 1, "Comments", "accept", db_path=db)

    def test_only_editor_decisions(self, db):
        """Only users with role editor can request revisions, accept/reject, or publish."""
        mid = "ms-editor-roles-1"
        engine.create_manuscript(mid, "Dark Matter", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted text", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)

        # Author cannot assign reviewers
        with pytest.raises(engine.RoleAuthorizationError):
            engine.require_role(mid, "alice", "author", ["editor"], db_path=db)

        # Reviewer cannot make decision
        with pytest.raises(engine.RoleAuthorizationError):
            engine.require_role(mid, "rita", "reviewer", ["editor"], db_path=db)

        # Editor can make decision
        engine.require_role(mid, "eric", "editor", ["editor"], db_path=db)

    def test_post_publish_lock(self, db):
        """Once published, no further changes of any kind are permitted."""
        mid = "ms-pub-lock-1"
        engine.create_manuscript(mid, "Higgs Boson", "alice", initial_text="Initial text", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted text", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(mid, "rita", 2, 1, "Good work", "accept", db_path=db)
        engine.make_decision(mid, "eric", "accept", db_path=db)
        engine.publish_manuscript(mid, "eric", lamport=3, db_path=db)

        assert engine.is_locked_published(mid, db_path=db) is True

        # Attempt to save checkpoint -> DocumentLockedError
        with pytest.raises(engine.DocumentLockedError):
            engine.save_version(mid, "alice", 4, "Post-publish edit", db_path=db)

        # Attempt to restore -> DocumentLockedError
        with pytest.raises(engine.DocumentLockedError):
            engine.restore_version(mid, "alice", 5, 1, "restoring", db_path=db)

    def test_revision_parent_is_previous_submitted_version(self, db):
        """Revision parent_ids must point to the previous submitted milestone version."""
        mid = "ms-parent-rev-1"
        engine.create_manuscript(mid, "Robotics", "alice", initial_text="Draft", db_path=db)
        sub_res = engine.submit_manuscript(mid, "alice", 2, "Submission 1", db_path=db)
        sub_vno = sub_res["version"]["version_no"]

        # Additional local edits
        engine.save_version(mid, "alice", 3, "Draft edit between rounds", db_path=db)

        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(mid, "rita", 2, 1, "Needs more work", "major", db_path=db)
        engine.request_revision(mid, "eric", "Revise chapter 2", db_path=db)

        rev_res = engine.submit_revision(mid, "alice", 4, "Revised manuscript", db_path=db)
        parent_ids = rev_res["version"]["parent_ids"]

        # Parent must be the previous submitted version (sub_vno)
        assert sub_vno in parent_ids

    def test_difflib_compare_versions(self, db):
        """Compare view returns unified diff and highlighted chunks."""
        mid = "ms-diff-1"
        engine.create_manuscript(mid, "Graph Theory", "alice", initial_text="Hello World\nLine 2", db_path=db)
        engine.save_version(mid, "alice", 2, "Hello Beautiful World\nLine 2\nLine 3", db_path=db)

        diff = engine.compare_versions(mid, 1, 2, db_path=db)
        assert diff["engine"] == "Mocked E3 Diff Engine (difflib)"
        assert len(diff["diff_chunks"]) > 0
        assert "diff_unified" in diff


class TestManuscriptRoleSeparationAndReset:
    """Tests for role separation, reviewer recommendation rules, editor override gating, and reset."""

    def test_reset_then_checkpoint_succeeds(self, db):
        """After reset, manuscript row exists in DRAFT and saving a checkpoint succeeds."""
        mid = "ms-reset-ckpt"
        engine.create_manuscript(mid, "Paper Title", "alice", initial_text="Initial text", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted text", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(mid, "rita", 2, 1, "Looks decent", "minor", db_path=db)

        # Reset manuscript
        engine.reset_document(mid, db_path=db)

        # Manuscript still exists in DRAFT
        m = engine.get_manuscript(mid, db_path=db)
        assert m is not None
        assert m["status"] == "DRAFT"
        assert m["current_round"] == 0

        # Checkpoint succeeds immediately
        ckpt = engine.save_version(mid, "alice", 1, "Brand new draft after reset", db_path=db)
        assert ckpt["version_no"] == 1
        assert ckpt["reason"] == "checkpoint"

    def test_reset_clears_reviews_and_decisions(self, db):
        """Reset clears reviews, reviewer_assignments, decisions, versions, and audit log."""
        mid = "ms-reset-clear"
        engine.create_manuscript(mid, "Reset Clear Test", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted text", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(mid, "rita", 2, 1, "Needs work", "major", db_path=db)
        engine.request_revision(mid, "eric", "Please revise", db_path=db)

        # Confirm data existed
        wf_before = engine.get_workflow(mid, db_path=db)
        assert len(wf_before["reviews"]) == 1
        assert len(wf_before["decisions"]) == 1
        assert len(wf_before["reviewer_summary"]) == 1

        # Reset
        engine.reset_document(mid, db_path=db)

        # Confirm data cleared
        wf_after = engine.get_workflow(mid, db_path=db)
        assert len(wf_after["reviews"]) == 0
        assert len(wf_after["decisions"]) == 0
        assert len(wf_after["reviewer_summary"]) == 0
        assert wf_after["manuscript"]["status"] == "DRAFT"
        assert wf_after["manuscript"]["current_round"] == 0

    def test_checkpoint_unknown_id_author_auto_creates(self, db):
        """Author calling checkpoint/ensure_manuscript_exists on unknown ID auto-creates manuscript in DRAFT."""
        import main
        mid = "auto-created-ms"
        main.ensure_manuscript_exists(mid, "alice", "author", db_path=db)

        m = engine.get_manuscript(mid, db_path=db)
        assert m is not None
        assert m["status"] == "DRAFT"
        assert m["author_id"] == "alice"
        assert m["current_round"] == 0

        # Save checkpoint now works
        ckpt = engine.save_version(mid, "alice", 1, "Auto-created draft content", db_path=db)
        assert ckpt["version_no"] == 1

    def test_checkpoint_unknown_id_editor_fails_404(self, db):
        """Non-author accessing unknown manuscript raises 404 with exact message."""
        import main
        mid = "non-existent-ms"
        with pytest.raises(main.HTTPException) as exc_info:
            main.ensure_manuscript_exists(mid, "eric", "editor", db_path=db)
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Manuscript not found - create it first"

        with pytest.raises(main.HTTPException) as exc_info2:
            main.ensure_manuscript_exists(mid, "rita", "reviewer", db_path=db)
        assert exc_info2.value.status_code == 404
        assert exc_info2.value.detail == "Manuscript not found - create it first"

    def test_editor_cannot_call_checkpoint_or_revisions(self, db):
        """Editors cannot edit document content (blocked from author-only routes)."""
        mid = "ms-content-lock-role"
        engine.create_manuscript(mid, "Security Paper", "alice", initial_text="Draft", db_path=db)

        # Author is permitted
        engine.require_role(mid, "alice", "author", ["author"], db_path=db)

        # Editor is blocked from content editing
        with pytest.raises(engine.RoleAuthorizationError):
            engine.require_role(mid, "eric", "editor", ["author"], db_path=db)

        # Reviewer is blocked from content editing
        with pytest.raises(engine.RoleAuthorizationError):
            engine.require_role(mid, "rita", "reviewer", ["author"], db_path=db)

    def test_reviewer_recommendation_does_not_change_status(self, db):
        """Reviewers can only submit recommendations; manuscript status remains UNDER_REVIEW."""
        mid = "ms-rec-status"
        engine.create_manuscript(mid, "Reviewer Rec Test", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submit", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)

        assert engine.get_manuscript(mid, db_path=db)["status"] == "UNDER_REVIEW"

        # Reviewer submits 'reject' recommendation
        rev = engine.add_review(mid, "rita", 2, 1, "Reject this manuscript immediately", "reject", db_path=db)
        assert rev["recommendation"] == "reject"

        # Status must STILL be UNDER_REVIEW (reviewer cannot change status!)
        assert engine.get_manuscript(mid, db_path=db)["status"] == "UNDER_REVIEW"

    def test_desk_reject_at_submitted(self, db):
        """Editor can desk-reject directly at SUBMITTED status before assigning reviewers."""
        mid = "ms-desk-rej"
        engine.create_manuscript(mid, "Desk Reject Paper", "alice", initial_text="Out of scope paper", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submit out of scope", db_path=db)

        assert engine.get_manuscript(mid, db_path=db)["status"] == "SUBMITTED"

        # Editor desk-rejects directly
        dec = engine.make_decision(mid, "eric", "reject", db_path=db)
        assert dec["decision"] == "REJECT"
        assert dec["status"] == "REJECTED"

        m = engine.get_manuscript(mid, db_path=db)
        assert m["status"] == "REJECTED"

    def test_decision_blocked_without_all_reviews_unless_override(self, db):
        """Accept/reject/request-revision blocked while reviews pending, unless override is ticked."""
        mid = "ms-override-gate"
        engine.create_manuscript(mid, "Gate Paper", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita", "raj"], db_path=db)

        # Rita submits review, Raj has not
        engine.add_review(mid, "rita", 2, 1, "Minor changes needed", "minor", db_path=db)

        # Decision blocked with override=False
        with pytest.raises(engine.StatusTransitionError) as exc:
            engine.make_decision(mid, "eric", "accept", override=False, db_path=db)
        assert "raj" in str(exc.value)

        # Request revision blocked with override=False
        with pytest.raises(engine.StatusTransitionError) as exc2:
            engine.request_revision(mid, "eric", "Please revise", override=False, db_path=db)
        assert "raj" in str(exc2.value)

        # Request revision succeeds with override=True
        rev_req = engine.request_revision(mid, "eric", "Please revise without Raj's review", override=True, db_path=db)
        assert rev_req["status"] == "REVISION_REQUESTED"

    def test_override_logged_in_audit_trail(self, db):
        """When an editor decides without all reviews, an audit event is recorded."""
        mid = "ms-override-audit"
        engine.create_manuscript(mid, "Audit Paper", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)

        # Rita hasn't submitted, editor overrides and rejects
        engine.make_decision(mid, "eric", "reject", override=True, db_path=db)

        audit_res = engine.get_audit(mid, db_path=db)
        events = [a["event_type"] for a in audit_res["items"]]
        assert "editor_override_decision" in events

    def test_get_workflow_includes_reviewer_summary(self, db):
        """Workflow includes reviewer_summary with pending or submitted status and recommendation."""
        mid = "ms-summary-check"
        engine.create_manuscript(mid, "Summary Paper", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita", "raj"], db_path=db)

        # Before reviews
        wf1 = engine.get_workflow(mid, db_path=db)
        summary1 = {s["reviewer_id"]: s["status"] for s in wf1["reviewer_summary"]}
        assert summary1["rita"] == "pending"
        assert summary1["raj"] == "pending"

        # After Rita reviews
        engine.add_review(mid, "rita", 2, 1, "Accept this paper", "accept", requested_changes=["Fix typo"], db_path=db)
        wf2 = engine.get_workflow(mid, db_path=db)
        summary2 = {s["reviewer_id"]: s["status"] for s in wf2["reviewer_summary"]}
        assert summary2["rita"] == "submitted"
        assert summary2["raj"] == "pending"
        rita_entry = next(s for s in wf2["reviewer_summary"] if s["reviewer_id"] == "rita")
        assert rita_entry["recommendation"] == "accept"
        assert rita_entry["requested_changes"] == ["Fix typo"]

    def test_editor_can_read_reviews_and_summary(self, db):
        """Editor can fetch reviews and review summary with requested_changes."""
        import main
        mid = "ms-ed-rev-test"
        engine.create_manuscript(mid, "Review Access Test", "alice", initial_text="Draft text", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted text", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita", "raj"], db_path=db)

        engine.add_review(
            mid, "rita", 2, 1, "Minor adjustments needed", "minor",
            requested_changes=["Add latency table", "Format section 4"],
            db_path=db
        )

        # Editor reads reviews via endpoint
        rev_res = main.get_reviews(mid, role="editor", userId="eric", db_path=db)
        assert len(rev_res["reviews"]) == 1
        r = rev_res["reviews"][0]
        assert r["reviewer_id"] == "rita"
        assert r["recommendation"] == "minor"
        assert r["comments"] == "Minor adjustments needed"
        assert r["requested_changes"] == ["Add latency table", "Format section 4"]

        # Editor reads review summary via endpoint
        sum_res = main.get_review_summary_endpoint(mid, role="editor", userId="eric", db_path=db)
        reviewers = sum_res["summary"]
        assert len(reviewers) == 2
        rita_s = next(x for x in reviewers if x["reviewer_id"] == "rita")
        raj_s = next(x for x in reviewers if x["reviewer_id"] == "raj")
        assert rita_s["status"] == "submitted"
        assert rita_s["recommendation"] == "minor"
        assert rita_s["requested_changes"] == ["Add latency table", "Format section 4"]
        assert raj_s["status"] == "pending"
        assert raj_s["recommendation"] is None
        assert raj_s["requested_changes"] == []

    def test_author_can_read_reviews_defaulting_to_current_round(self, db):
        """Author can read reviews; default round = current_round."""
        import main
        mid = "ms-auth-rev-test"
        engine.create_manuscript(mid, "Author Review Test", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(
            mid, "rita", 2, 1, "Well written", "accept",
            requested_changes=["Small typo fix on page 2"],
            db_path=db
        )

        # Author calls without specifying round -> defaults to current_round (1)
        res = main.get_reviews(mid, round=None, role="author", userId="alice", db_path=db)
        assert res["round_no"] == 1
        assert len(res["reviews"]) == 1
        assert res["reviews"][0]["recommendation"] == "accept"
        assert res["reviews"][0]["requested_changes"] == ["Small typo fix on page 2"]

    def test_unassigned_reviewer_cannot_read_reviews_or_summary(self, db):
        """Unassigned reviewer is rejected with HTTP 403 when trying to access reviews or summary."""
        import main
        from fastapi import HTTPException
        mid = "ms-unassigned-rev-test"
        engine.create_manuscript(mid, "Privacy Test", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)

        # Rita is assigned, Bob is NOT
        # Bob cannot read reviews
        with pytest.raises(HTTPException) as exc_info:
            main.get_reviews(mid, role="reviewer", userId="bob", db_path=db)
        assert exc_info.value.status_code == 403

        # Bob cannot read review summary (editor only)
        with pytest.raises(HTTPException) as exc_info2:
            main.get_review_summary_endpoint(mid, role="reviewer", userId="bob", db_path=db)
        assert exc_info2.value.status_code == 403

        # Author also cannot read editor review summary
        with pytest.raises(HTTPException) as exc_info3:
            main.get_review_summary_endpoint(mid, role="author", userId="alice", db_path=db)
        assert exc_info3.value.status_code == 403

    def test_revision_notes_stored_and_visible_to_author(self, db):
        """revision_notes are stored with decision and visible to author via get_workflow."""
        mid = "ms-rev-notes-test"
        engine.create_manuscript(mid, "Revision Notes Test", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(mid, "rita", 2, 1, "Major revision required", "major", requested_changes=["Fix proofs"], db_path=db)

        # Editor requests revision with specific revision notes
        notes = "Reviewer rita (major):\n- Fix proofs\nComments: Major revision required"
        engine.request_revision(mid, "eric", comments="Editor remarks", revision_notes=notes, db_path=db)

        # Author fetches workflow
        wf = engine.get_workflow(mid, user_id="alice", role="author", db_path=db)
        assert wf["manuscript"]["status"] == "REVISION_REQUESTED"
        assert wf["revision_notes"] == notes
        assert wf["current_revision_notes"] == notes

        # Also visible in decisions list
        dec = wf["decisions"][0]
        assert dec["decision"] == "REVISION_REQUESTED"
        assert dec["revision_notes"] == notes

    def test_review_summary_shows_pending_for_unsubmitted_reviewer(self, db):
        """Review summary shows 'pending' and empty requested_changes for reviewers who haven't submitted."""
        mid = "ms-pending-summary"
        engine.create_manuscript(mid, "Pending Test", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita", "raj"], db_path=db)

        # Only Rita submits
        engine.add_review(mid, "rita", 2, 1, "Looks ok", "minor", requested_changes=["Fix title"], db_path=db)

        summary = engine.get_review_summary(mid, round_no=1, db_path=db)
        summary_by_id = {s["reviewer_id"]: s for s in summary}

        assert summary_by_id["rita"]["status"] == "submitted"
        assert summary_by_id["rita"]["recommendation"] == "minor"
        assert summary_by_id["rita"]["requested_changes"] == ["Fix title"]

        assert summary_by_id["raj"]["status"] == "pending"
        assert summary_by_id["raj"]["recommendation"] is None
        assert summary_by_id["raj"]["comments"] is None
        assert summary_by_id["raj"]["requested_changes"] == []


# ---------------------------------------------------------------------------
# Defect regression tests (Sprint 2 defect register DEF-01 … DEF-05)
# Each test failed on the original code and must pass after the correction.
# ---------------------------------------------------------------------------

A = {"X-Role": "author", "X-User-Id": "alice"}
E = {"X-Role": "editor", "X-User-Id": "eric"}
R = {"X-Role": "reviewer", "X-User-Id": "rita"}


@pytest.fixture
def api(db, monkeypatch):
    """FastAPI TestClient bound to the per-test temporary database."""
    import db as db_module
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(db_module, "DB_PATH", db)
    monkeypatch.delenv("VV_DEMO_MODE", raising=False)
    monkeypatch.delenv("VV_EDITORS", raising=False)
    with TestClient(main.app) as client:
        yield client


def _accepted_manuscript(db, mid="ms-def"):
    """Create a manuscript and drive it to ACCEPTED (engine level)."""
    engine.create_manuscript(mid, "Defect Paper", "alice", initial_text="Draft one", db_path=db)
    engine.save_version(mid, "alice", 2, "Draft two", db_path=db)
    sub = engine.submit_manuscript(mid, "alice", 3, "Submitted text", db_path=db)
    engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
    engine.add_review(mid, "rita", sub["version"]["version_no"], 1, "Good", "accept", db_path=db)
    engine.make_decision(mid, "eric", "accept", db_path=db)
    return mid


class TestDefectRegressions:

    # ---- DEF-01: publish must verify SHA-256 first ---------------------------
    def test_def01_publish_refused_when_latest_version_corrupted(self, db):
        mid = _accepted_manuscript(db)
        last = engine.get_versions(mid, db_path=db)["items"][0]["version_no"]
        engine.corrupt_version(mid, last, db_path=db)
        before = engine.get_versions(mid, db_path=db)["total"]

        with pytest.raises(engine.IntegrityVerificationError):
            engine.publish_manuscript(mid, "eric", lamport=9, db_path=db)

        assert engine.get_manuscript(mid, db_path=db)["status"] == "ACCEPTED"
        assert engine.get_versions(mid, db_path=db)["total"] == before

    def test_def01_publish_api_returns_409_and_clean_publish_still_works(self, db, api):
        mid = _accepted_manuscript(db, "ms-def01-api")
        last = engine.get_versions(mid, db_path=db)["items"][0]["version_no"]
        engine.corrupt_version(mid, last, db_path=db)
        r = api.post(f"/manuscripts/{mid}/publish", headers=E, json={"editorId": "eric"})
        assert r.status_code == 409
        assert "SHA-256" in r.json()["detail"]

        ok_mid = _accepted_manuscript(db, "ms-def01-ok")
        r2 = api.post(f"/manuscripts/{ok_mid}/publish", headers=E, json={"editorId": "eric"})
        assert r2.status_code == 200
        pub_no = r2.json()["version"]["version_no"]
        _, verified, _ = engine.rebuild_version(ok_mid, pub_no, db_path=db)
        assert verified

    # ---- DEF-02: editor role cannot be claimed by any user --------------------
    def test_def02_unregistered_user_cannot_act_as_editor(self, db, api):
        mid = "ms-def02"
        engine.create_manuscript(mid, "Role Paper", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(mid, "rita", 3, 1, "ok", "accept", db_path=db)

        fake = {"X-Role": "editor", "X-User-Id": "alice"}
        r = api.post(f"/manuscripts/{mid}/decision", headers=fake,
                     json={"editorId": "alice", "decision": "accept"})
        assert r.status_code == 403
        assert engine.get_manuscript(mid, db_path=db)["status"] == "UNDER_REVIEW"

        r2 = api.post(f"/manuscripts/{mid}/decision", headers=E,
                      json={"editorId": "eric", "decision": "accept"})
        assert r2.status_code == 200

    def test_def02_unknown_role_rejected(self, db):
        engine.create_manuscript("ms-role-x", "X", "alice", initial_text="d", db_path=db)
        with pytest.raises(engine.RoleAuthorizationError):
            engine.require_role("ms-role-x", "mallory", "admin", ["admin"], db_path=db)

    # ---- DEF-03: reviewers limited to milestones on every read route --------
    def _under_review(self, db, mid):
        engine.create_manuscript(mid, "Read Paper", "alice", initial_text="Secret draft one", db_path=db)
        engine.save_version(mid, "alice", 2, "Secret draft two", db_path=db)
        engine.submit_manuscript(mid, "alice", 3, "Submitted text", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)

    def test_def03_reviewer_cannot_compare_draft_versions(self, db, api):
        mid = "ms-def03-cmp"
        self._under_review(db, mid)
        r = api.get(f"/manuscripts/{mid}/compare?a=1&b=2", headers=R)
        assert r.status_code == 403
        # author may still compare drafts
        assert api.get(f"/manuscripts/{mid}/compare?a=1&b=2", headers=A).status_code == 200

    def test_def03_reviewer_version_list_shows_only_milestones(self, db, api):
        mid = "ms-def03-list"
        self._under_review(db, mid)
        r = api.get(f"/versions?docId={mid}", headers=R)
        assert r.status_code == 200
        reasons = {v["reason"] for v in r.json()["items"]}
        assert reasons == {"submission"}
        full = api.get(f"/versions?docId={mid}", headers=A).json()
        assert full["total"] == 4  # draft, draft, safety, submission

    def test_def03_read_without_identity_rejected(self, db, api):
        mid = "ms-def03-anon"
        self._under_review(db, mid)
        assert api.get(f"/versions/{mid}/1").status_code == 403
        assert api.get(f"/audit?docId={mid}").status_code == 403

    # ---- DEF-04: demo controls disabled unless VV_DEMO_MODE=1 ----------------
    def test_def04_demo_routes_blocked_by_default(self, db, api, monkeypatch):
        engine.save_version("doc-demo", "alice", 1, "text", db_path=db)
        assert api.post("/debug/fail-next-write").status_code == 403
        assert api.post("/debug/corrupt/doc-demo/1").status_code == 403
        assert api.post("/debug/reset?docId=doc-demo").status_code == 403
        assert api.post("/config", json={"snapshotEvery": 3}).status_code == 403
        assert api.post("/audit", json={"userId": "x", "docId": "doc-demo", "eventType": "fake"}).status_code == 403
        assert engine.get_simulate_failure() is False

        monkeypatch.setenv("VV_DEMO_MODE", "1")
        assert api.post("/debug/corrupt/doc-demo/1").status_code == 200

    # ---- DEF-05: submit / revision are all-or-nothing ------------------------
    def _fail_on_call(self, monkeypatch, n):
        """Make the n-th _insert_version call inside one test raise."""
        original = engine._insert_version
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == n:
                raise RuntimeError("Injected failure for DEF-05 test")
            return original(*args, **kwargs)
        monkeypatch.setattr(engine, "_insert_version", flaky)

    @pytest.mark.parametrize("failing_step", [1, 2])  # 1 = safety, 2 = milestone
    def test_def05_failed_submit_changes_nothing(self, db, monkeypatch, failing_step):
        mid = f"ms-def05-sub-{failing_step}"
        engine.create_manuscript(mid, "Atomic Paper", "alice", initial_text="Draft", db_path=db)
        before = engine.get_versions(mid, db_path=db)["total"]
        self._fail_on_call(monkeypatch, failing_step)

        with pytest.raises(RuntimeError):
            engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)

        assert engine.get_manuscript(mid, db_path=db)["status"] == "DRAFT"
        assert engine.get_versions(mid, db_path=db)["total"] == before

    def test_def05_failed_revision_changes_nothing(self, db, monkeypatch):
        mid = "ms-def05-rev"
        engine.create_manuscript(mid, "Atomic Rev", "alice", initial_text="Draft", db_path=db)
        engine.submit_manuscript(mid, "alice", 2, "Submitted", db_path=db)
        engine.assign_reviewers(mid, "eric", ["rita"], db_path=db)
        engine.add_review(mid, "rita", 3, 1, "fix", "major", db_path=db)
        engine.request_revision(mid, "eric", "Please revise", db_path=db)
        before = engine.get_versions(mid, db_path=db)["total"]
        self._fail_on_call(monkeypatch, 2)

        with pytest.raises(RuntimeError):
            engine.submit_revision(mid, "alice", 4, "Revised", db_path=db)

        m = engine.get_manuscript(mid, db_path=db)
        assert m["status"] == "REVISION_REQUESTED"
        assert m["current_round"] == 1
        assert engine.get_versions(mid, db_path=db)["total"] == before
