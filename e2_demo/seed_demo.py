"""
seed_demo.py – Populate demo manuscripts and documents via the engine.

Demonstrates the complete 8-step Journal Publication and Manuscript Management journey:
  Step 1: DRAFT               (Author Alice writes initial manuscript)
  Step 2: SUBMITTED           (Author Alice submits -> safety checkpoint + snapshot v_sub)
  Step 3: UNDER_REVIEW (R1)   (Editor Eric assigns Reviewers Rita & Raj)
  Step 4: REVIEWS SUBMITTED   (Rita: minor revision, Raj: major revision)
  Step 5: REVISION_REQUESTED  (Editor Eric requests author revision)
  Step 6: REVISED (R2)        (Author Alice submits revision -> parent = previous submission)
  Step 7: ACCEPTED            (Reviewer Rita recommends accept -> Editor Eric accepts)
  Step 8: PUBLISHED           (Editor Eric publishes -> milestone snapshot + permanent lock)

Usage:
    python seed_demo.py
    python seed_demo.py --doc manuscript-1
"""

import argparse
import sys
import os

# Allow running from any directory by adding e2_demo to the path.
sys.path.insert(0, os.path.dirname(__file__))

import engine
from db import init_db, DB_PATH


def seed_manuscript(mid: str = "manuscript-1", db_path: str | None = None) -> None:
    db = db_path or DB_PATH
    init_db(db_path=db)
    engine.reset_document(mid, db_path=db)
    print(f"\n========================================================")
    print(f"[seed] Running 8-step Journal Workflow on '{mid}'")
    print(f"========================================================")

    # ----------------------------------------------------
    # Step 1: DRAFT (Author Alice drafts manuscript)
    # ----------------------------------------------------
    print("\n--- Step 1: DRAFT (Author Alice drafts initial text) ---")
    draft_text_v1 = (
        "Title: Byzantine Fault-Tolerant State Machine Replication in Edge Clouds\n"
        "Authors: Alice Smith, Bob Jones\n\n"
        "Abstract: We introduce EdgeBFT, an adaptive consensus engine designed for\n"
        "low-latency peer-to-peer persistence across edge nodes.\n\n"
        "1. Introduction\n"
        "Distributed consensus at the network edge faces transient partitions..."
    )
    m = engine.create_manuscript(
        manuscript_id=mid,
        title="Byzantine Fault-Tolerant State Machine Replication in Edge Clouds",
        author_id="alice",
        initial_text=draft_text_v1,
        db_path=db,
    )
    print(f"  [v1 Draft] Status={m['status']} Author=alice (Snapshot)")

    # Alice makes an intermediate edit before submission
    draft_text_v2 = (
        draft_text_v1 + "\n\n"
        "2. Related Work\n"
        "Classical PBFT algorithms incur O(n^2) message complexity..."
    )
    v2 = engine.save_version(mid, "alice", 2, draft_text_v2, reason="checkpoint", round_no=1, db_path=db)
    print(f"  [v2 Draft Edit] Author=alice Type={v2['type']}")

    # ----------------------------------------------------
    # Step 2: SUBMITTED (Alice submits manuscript)
    # ----------------------------------------------------
    print("\n--- Step 2: SUBMITTED (Author Alice submits manuscript) ---")
    sub_res = engine.submit_manuscript(mid, "alice", 3, draft_text_v2, db_path=db)
    sub_ver = sub_res["version"]
    print(f"  [Safety Checkpoint v3] Created atomically before submission")
    print(f"  [v4 Milestone] Type={sub_ver['type']} Reason={sub_ver['reason']} Milestone='{sub_ver['milestone']}'")
    print(f"  Status={sub_res['status']}")

    # ----------------------------------------------------
    # Step 3: UNDER_REVIEW (Editor Eric assigns reviewers)
    # ----------------------------------------------------
    print("\n--- Step 3: UNDER_REVIEW Round 1 (Editor Eric assigns Rita and Raj) ---")
    assign_res = engine.assign_reviewers(mid, "eric", ["rita", "raj"], round_no=1, db_path=db)
    print(f"  Status={assign_res['status']} Reviewers assigned: {assign_res['reviewers']}")

    # ----------------------------------------------------
    # Step 4: REVIEWS (Rita and Raj submit peer reviews)
    # ----------------------------------------------------
    print("\n--- Step 4: REVIEWS SUBMITTED ---")
    # Reviewer views version (audit logged)
    engine.add_audit("rita", mid, "reviewer_viewed_version", sub_ver["version_no"], db_path=db)
    engine.add_audit("raj", mid, "reviewer_viewed_version", sub_ver["version_no"], db_path=db)

    rev1 = engine.add_review(
        manuscript_id=mid,
        reviewer_id="rita",
        version_no=sub_ver["version_no"],
        round_no=1,
        comments="Solid protocol analysis. Please provide benchmark numbers comparing with Raft.",
        recommendation="minor",
        requested_changes=[
            "Provide benchmark numbers comparing with Raft under packet loss",
            "Clarify throughput measurement setup in Section 3",
        ],
        db_path=db,
    )
    print(f"  Reviewer Rita -> recommendation='{rev1['recommendation']}' requested_changes={len(rev1['requested_changes'])}")

    rev2 = engine.add_review(
        manuscript_id=mid,
        reviewer_id="raj",
        version_no=sub_ver["version_no"],
        round_no=1,
        comments="Theoretical proof for Lemma 2 is incomplete. Section 3 needs formal latency bounds.",
        recommendation="major",
        requested_changes=[
            "Complete theoretical proof for Lemma 2",
            "Establish formal latency bounds Delta under partial synchrony",
        ],
        db_path=db,
    )
    print(f"  Reviewer Raj -> recommendation='{rev2['recommendation']}' requested_changes={len(rev2['requested_changes'])}")

    # ----------------------------------------------------
    # Step 5: REVISION_REQUESTED (Editor Eric requests revision)
    # ----------------------------------------------------
    print("\n--- Step 5: REVISION_REQUESTED (Editor Eric requests revisions) ---")
    compiled_notes = (
        "Reviewer rita (minor):\n"
        "- Provide benchmark numbers comparing with Raft under packet loss\n"
        "- Clarify throughput measurement setup in Section 3\n"
        "Comments: Solid protocol analysis. Please provide benchmark numbers comparing with Raft.\n\n"
        "Reviewer raj (major):\n"
        "- Complete theoretical proof for Lemma 2\n"
        "- Establish formal latency bounds Delta under partial synchrony\n"
        "Comments: Theoretical proof for Lemma 2 is incomplete. Section 3 needs formal latency bounds."
    )
    req_res = engine.request_revision(
        mid, "eric",
        comments="Please address reviewer recommendations.",
        revision_notes=compiled_notes,
        db_path=db,
    )
    print(f"  Status={req_res['status']}")
    print(f"  Revision Notes Stored (length={len(req_res['revision_notes'])} chars)")

    # ----------------------------------------------------
    # Step 6: REVISED (Author Alice submits revision)
    # ----------------------------------------------------
    print("\n--- Step 6: REVISED Round 2 (Author Alice submits Revision R1) ---")
    revised_text = (
        draft_text_v2 + "\n\n"
        "3. Experimental Evaluation\n"
        "Benchmark: EdgeBFT achieves 42% lower commit latency compared to Raft under 15% packet loss.\n\n"
        "4. Formal Proof of Lemma 2\n"
        "Under partial synchrony with bound Delta, safety is guaranteed by quorum intersection."
    )
    rev_res = engine.submit_revision(mid, "alice", 5, revised_text, db_path=db)
    rev_ver = rev_res["version"]
    print(f"  [v6 Milestone] Type={rev_ver['type']} Milestone='{rev_ver['milestone']}' Round={rev_ver['round_no']}")
    print(f"  Parent IDs: {rev_ver['parent_ids']} (Points back to submitted v{sub_ver['version_no']})")
    print(f"  Status={rev_res['status']}")

    # ----------------------------------------------------
    # Step 7: ACCEPTED (Re-review & Editor Eric accepts)
    # ----------------------------------------------------
    print("\n--- Step 7: UNDER_REVIEW Round 2 & ACCEPTED ---")
    engine.assign_reviewers(mid, "eric", ["rita"], round_no=2, db_path=db)
    engine.add_audit("rita", mid, "reviewer_viewed_version", rev_ver["version_no"], db_path=db)

    engine.add_review(
        manuscript_id=mid,
        reviewer_id="rita",
        version_no=rev_ver["version_no"],
        round_no=2,
        comments="Revisions fully address all concerns. Benchmark data is convincing.",
        recommendation="accept",
        db_path=db,
    )
    dec_res = engine.make_decision(mid, "eric", "accept", db_path=db)
    print(f"  Decision={dec_res['decision']} -> Status={dec_res['status']}")

    # ----------------------------------------------------
    # Step 8: PUBLISHED (Editor Eric publishes manuscript)
    # ----------------------------------------------------
    print("\n--- Step 8: PUBLISHED (Editor Eric publishes -> Permanent Lock) ---")
    pub_res = engine.publish_manuscript(mid, "eric", lamport=6, db_path=db)
    pub_ver = pub_res["version"]
    print(f"  [v8 Milestone] Type={pub_ver['type']} Milestone='{pub_ver['milestone']}'")
    print(f"  Status={pub_res['status']} (Document is now locked against further edits)")

    # Storage statistics
    stats = engine.get_stats(mid, db_path=db)
    pct = 100 * (1 - stats['stored_bytes'] / stats['full_text_bytes']) if stats['full_text_bytes'] else 0
    print(f"\n[seed] Completed '{mid}': {stats['total_versions']} versions ({stats['snapshots']} snapshots, {stats['deltas']} deltas)")
    print(f"[seed] Stored {stats['stored_bytes']} bytes vs {stats['full_text_bytes']} full-copy bytes ({pct:.1f}% space saved)")


def seed_doc(doc_id: str = "doc-1", db_path: str | None = None) -> None:
    """Seed the standard collaboration doc-1."""
    db = db_path or DB_PATH
    init_db(db_path=db)
    engine.reset_document(doc_id, db_path=db)
    print(f"\n[seed] Seeding collaboration document '{doc_id}'")

    edits = [
        ("alice", 1, "Project Proposal: VersionVault\n\nDate: 2024-01-10\n\nSummary: Smart version history."),
        ("alice", 2, "Project Proposal: VersionVault\n\nDate: 2024-01-10\n\nSummary: Smart version history.\n\nTeam: 4 members."),
        ("bob", 3, "Project Proposal: VersionVault\n\nDate: 2024-01-11\n\nSummary: Smart version history.\n\nTeam: 4 members.\n\nEngine: E2 persistence."),
        ("bob", 4, "Project Proposal: VersionVault\n\nDate: 2024-01-11\n\nSummary: Smart version history.\n\nTeam: 4 members.\n\nEngine: E2 persistence.\n\nBudget: $5,000"),
        ("carol", 5, "Project Proposal: VersionVault\n\nDate: 2024-01-12\n\nSummary: Smart version history.\n\nTeam: 5 members.\n\nEngine: E2 snapshot+delta.\n\nBudget: $5,000"),
        ("carol", 6, "Project Proposal: VersionVault\n\nDate: 2024-01-12\n\nSummary: Smart version history.\n\nTeam: 5 members.\n\nEngine: E2 snapshot+delta.\n\nBudget: $6,500\n\nDeadline: 2024-03-01"),
        ("dave", 7, "Project Proposal: VersionVault\n\nDate: 2024-01-13\n\nSummary: Smart version history.\n\nTeam: 5 members.\n\nEngine: E2 snapshot+delta.\n\nBudget: $6,500\n\nDeadline: 2024-03-15"),
        ("alice", 8, "Project Proposal: VersionVault\n\nDate: 2024-01-14\n\nSummary: Smart version history.\n\nTeam: 5 members.\n\nEngine: E2 snapshot+delta.\n\nBudget: $6,500\n\nDeadline: 2024-03-15\n\nUI demo: 3 columns"),
    ]
    for user, lam, text in edits:
        engine.save_version(doc_id, user, lam, text, db_path=db)
    print(f"[seed] Created {len(edits)} versions for '{doc_id}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed demo data for VersionVault E2 & Journal Workflow")
    parser.add_argument("--doc", default="both", help="Document ID to seed ('manuscript-1', 'doc-1', or 'both')")
    args = parser.parse_args()

    if args.doc == "both":
        seed_doc("doc-1")
        seed_manuscript("manuscript-1")
    elif args.doc.startswith("manuscript"):
        seed_manuscript(args.doc)
    else:
        seed_doc(args.doc)
