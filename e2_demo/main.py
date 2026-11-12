"""
main.py – FastAPI application for VersionVault: Journal Publication and Manuscript Management.

Integrates E2 Checkpoint & Version Persistence with the Manuscript Workflow Layer.
CORS is not needed because the static UI is served from the same app.
Swagger UI at /docs doubles as message-contract documentation.
"""

from __future__ import annotations

import os
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import engine
from db import init_db

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

app = FastAPI(
    title="VersionVault: Journal Publication and Manuscript Management",
    description=(
        "VersionVault Engine E2 extended with Journal Publication & Manuscript Workflow. "
        "Provides snapshot/delta storage, SHA-256 integrity, atomic safety checkpoints, "
        "non-destructive restores, Lamport clocks, audit logs, and an 8-step review workflow."
    ),
    version="2.0.0",
)


@app.on_event("startup")
def startup() -> None:
    """Initialise the SQLite database and run schema migrations on startup."""
    init_db()


# ---------------------------------------------------------------------------
# Serve static files
# ---------------------------------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """Serve the demo UI."""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class CheckpointRequest(BaseModel):
    """checkpoint_request: docId, userId, lamport, editCount, text"""
    docId: str
    userId: str
    lamport: int
    editCount: int = 0
    text: str
    role: Optional[str] = "author"


class PreRestoreRequest(BaseModel):
    """pre_restore_checkpoint_request: docId, userId, lamport, text"""
    docId: str
    userId: str
    lamport: int
    text: str
    role: Optional[str] = "author"


class RestoreRequest(BaseModel):
    """restore_request: docId, userId, lamport, versionNo, currentText"""
    docId: str
    userId: str
    lamport: int
    versionNo: int
    currentText: str
    role: Optional[str] = "author"


class AuditRequest(BaseModel):
    """audit_event_request: userId, docId, eventType, versionRef"""
    userId: str
    docId: str
    eventType: str
    versionRef: Optional[int] = None


class ConfigRequest(BaseModel):
    """config_request: snapshotEvery"""
    snapshotEvery: int


# Manuscript Workflow Models
class CreateManuscriptRequest(BaseModel):
    """create_manuscript_request: manuscriptId, title, authorId, initialText"""
    manuscriptId: str
    title: str
    authorId: str
    initialText: Optional[str] = None


class SubmitManuscriptRequest(BaseModel):
    """submit_manuscript_request: userId, role, lamport, text"""
    userId: str
    role: str = "author"
    lamport: int = 1
    text: str


class AssignReviewersRequest(BaseModel):
    """assign_reviewers_request: editorId, role, reviewerIds, roundNo"""
    editorId: str
    role: str = "editor"
    reviewerIds: List[str]
    roundNo: Optional[int] = None


class ReviewRequest(BaseModel):
    """submit_review_request: reviewerId, role, versionNo, roundNo, comments, recommendation, requestedChanges"""
    reviewerId: str
    role: str = "reviewer"
    versionNo: int
    roundNo: Optional[int] = None
    comments: str
    recommendation: str = Field(..., description="accept | minor | major | reject")
    requestedChanges: Optional[List[str]] = Field(default_factory=list)
    requested_changes: Optional[List[str]] = None


class RequestRevisionRequest(BaseModel):
    """request_revision_request: editorId, role, comments, revisionNotes, override"""
    editorId: str
    role: str = "editor"
    comments: Optional[str] = None
    revisionNotes: Optional[str] = None
    revision_notes: Optional[str] = None
    override: bool = False


class SubmitRevisionRequest(BaseModel):
    """submit_revision_request: userId, role, lamport, text"""
    userId: str
    role: str = "author"
    lamport: int = 1
    text: str


class DecisionRequest(BaseModel):
    """decision_request: editorId, role, decision, override"""
    editorId: str
    role: str = "editor"
    decision: str = Field(..., description="accept | reject")
    override: bool = False


class PublishRequest(BaseModel):
    """publish_request: editorId, role, lamport"""
    editorId: str
    role: str = "editor"
    lamport: int = 1


def ensure_manuscript_exists(manuscript_id: str, user_id: str, role: str, db_path: Optional[str] = None) -> None:
    """
    Auto-creates the manuscripts row in DRAFT status if the ID does not exist and caller
    is an author; otherwise raises HTTP 404 with 'Manuscript not found - create it first'.
    """
    m = engine.get_manuscript(manuscript_id, db_path=db_path)
    if not m:
        if role == "author":
            engine.create_manuscript(
                manuscript_id=manuscript_id,
                title=manuscript_id,
                author_id=user_id,
                current_round=0,
                db_path=db_path,
            )
        else:
            raise HTTPException(status_code=404, detail="Manuscript not found - create it first")


# ---------------------------------------------------------------------------
# Checkpoint & Restore (Existing E2 endpoints with role and lock checks)
# ---------------------------------------------------------------------------

@app.post("/checkpoint", summary="Save a version checkpoint")
def checkpoint(
    req: CheckpointRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    checkpoint_request: docId, userId, lamport, editCount, text

    Only the author can save drafts. Rejected if manuscript is published.
    Auto-creates manuscript in DRAFT if it does not exist and caller is author.
    """
    role = x_role or req.role or "author"
    user_id = x_user_id or req.userId

    ensure_manuscript_exists(req.docId, user_id, role)

    try:
        engine.require_role(req.docId, user_id, role, ["author"])
        meta = engine.save_version(
            doc_id=req.docId,
            user_id=user_id,
            lamport=req.lamport,
            text=req.text,
            reason="checkpoint",
        )
        return meta
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/versions", summary="Paginated version timeline")
def versions(
    docId: str = Query(..., description="Document identifier"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
) -> dict:
    """FR2.5: Returns a paginated list of versions for *docId*, newest first."""
    return engine.get_versions(doc_id=docId, page=page, page_size=page_size)


@app.get("/versions/{docId}/{versionNo}", summary="Rebuild a specific version")
def get_version(
    docId: str,
    versionNo: int,
    userId: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    Rebuilds version *versionNo* of *docId* by replaying deltas from nearest snapshot.

    Reviewer limits: Reviewers can ONLY read the submitted milestone versions of
    manuscripts they are assigned to. Reading triggers a 'reviewer_viewed_version' audit.
    """
    eff_role = x_role or role
    eff_user = x_user_id or userId

    m = engine.get_manuscript(docId)
    if m and eff_role == "reviewer":
        # Check reviewer assignment
        try:
            engine.require_role(docId, eff_user, "reviewer", ["reviewer"])
        except engine.RoleAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

        # Check that this version is a submitted milestone version
        conn = engine.get_conn()
        try:
            vrow = conn.execute(
                "SELECT reason, milestone FROM versions WHERE doc_id = ? AND version_no = ?",
                (docId, versionNo),
            ).fetchone()
        finally:
            conn.close()

        if not vrow or vrow["reason"] not in ("submission", "revision", "published"):
            raise HTTPException(
                status_code=403,
                detail=f"Reviewers can only view submitted milestone versions of manuscript '{docId}'. Version {versionNo} is not accessible.",
            )

        # Audit log for reviewer reading version
        engine.add_audit(eff_user, docId, "reviewer_viewed_version", versionNo)

    text, verified, info = engine.rebuild_version(doc_id=docId, version_no=versionNo)
    if "error" in info and not text:
        raise HTTPException(status_code=404, detail=info["error"])

    return {
        "doc_id": docId,
        "version_no": versionNo,
        "text": text,
        "verified": verified,
        "snapshot_used": info.get("snapshot_used"),
        "deltas_replayed": info.get("deltas_replayed"),
        "error": info.get("error"),
    }


@app.post("/pre-restore-checkpoint", summary="Save a safety checkpoint before restore")
def pre_restore_checkpoint(
    req: PreRestoreRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """FR2.2: Saves current text as a 'safety' version. Author only."""
    role = x_role or req.role or "author"
    user_id = x_user_id or req.userId
    ensure_manuscript_exists(req.docId, user_id, role)
    try:
        engine.require_role(req.docId, user_id, role, ["author"])
        result = engine.pre_restore_checkpoint(
            doc_id=req.docId,
            user_id=user_id,
            lamport=req.lamport,
            current_text=req.text,
        )
        if result["status"] != "OK":
            raise HTTPException(status_code=500, detail=result.get("error", "unknown"))
        return result
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@app.post("/restore", summary="Restore a previous version (non-destructive)")
def restore(
    req: RestoreRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    restore_request: docId, userId, lamport, versionNo, currentText

    FR2.2: Appends a NEW version whose parent is the restored version. Author only.
    """
    role = x_role or req.role or "author"
    user_id = x_user_id or req.userId
    ensure_manuscript_exists(req.docId, user_id, role)
    try:
        engine.require_role(req.docId, user_id, role, ["author"])
        result = engine.restore_version(
            doc_id=req.docId,
            user_id=user_id,
            lamport=req.lamport,
            target_version_no=req.versionNo,
            current_text=req.currentText,
        )
        if result["status"] == "ABORTED":
            raise HTTPException(status_code=409, detail=result)
        return result
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# ---------------------------------------------------------------------------
# Manuscript Workflow Endpoints
# ---------------------------------------------------------------------------

@app.post("/manuscripts", summary="Create a new manuscript")
def create_manuscript(req: CreateManuscriptRequest) -> dict:
    """
    create_manuscript: manuscriptId, title, authorId, initialText

    Creates a manuscript in DRAFT status with an optional initial checkpoint.
    """
    try:
        return engine.create_manuscript(
            manuscript_id=req.manuscriptId,
            title=req.title,
            author_id=req.authorId,
            initial_text=req.initialText,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/manuscripts/{id}/submit", summary="Submit manuscript for peer review")
def submit_manuscript(
    id: str,
    req: SubmitManuscriptRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    submit_manuscript: Transitions DRAFT -> SUBMITTED.
    Author only. Performs safety checkpoint, saves milestone snapshot, sets parent_ids.
    """
    role = x_role or req.role
    user_id = x_user_id or req.userId
    try:
        engine.require_role(id, user_id, role, ["author"])
        return engine.submit_manuscript(
            manuscript_id=id,
            author_id=user_id,
            lamport=req.lamport,
            text=req.text,
        )
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.StatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/manuscripts/{id}/assign-reviewers", summary="Assign reviewers to manuscript")
def assign_reviewers(
    id: str,
    req: AssignReviewersRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    assign_reviewers: Transitions SUBMITTED or REVISED -> UNDER_REVIEW.
    Editor only. Records reviewer assignments and logs reviewer_assigned audit events.
    """
    role = x_role or req.role
    user_id = x_user_id or req.editorId
    ensure_manuscript_exists(id, user_id, role)
    try:
        engine.require_role(id, user_id, role, ["editor"])
        return engine.assign_reviewers(
            manuscript_id=id,
            editor_id=user_id,
            reviewer_ids=req.reviewerIds,
            round_no=req.roundNo,
        )
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.StatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/manuscripts/{id}/reviews", summary="Submit a peer review")
def post_review(
    id: str,
    req: ReviewRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    submit_review: Reviewer submits comments and recommendation (accept|minor|major|reject).
    Reviewer only, while status is UNDER_REVIEW.
    """
    role = x_role or req.role
    user_id = x_user_id or req.reviewerId
    ensure_manuscript_exists(id, user_id, role)
    rec = req.recommendation.lower()
    if rec not in ("accept", "minor", "major", "reject"):
        raise HTTPException(status_code=400, detail="Recommendation must be accept, minor, major, or reject")

    try:
        engine.require_role(id, user_id, role, ["reviewer"])
        m = engine.get_manuscript(id)
        if not m:
            raise HTTPException(status_code=404, detail="Manuscript not found - create it first")

        active_round = req.roundNo or m["current_round"]
        changes = req.requested_changes if req.requested_changes is not None else req.requestedChanges
        return engine.add_review(
            manuscript_id=id,
            reviewer_id=user_id,
            version_no=req.versionNo,
            round_no=active_round,
            comments=req.comments,
            recommendation=rec,
            requested_changes=changes,
        )
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.StatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/manuscripts/{id}/reviews", summary="Get reviews for manuscript")
def get_reviews(
    id: str,
    round: Optional[int] = Query(None, description="Filter reviews by round number"),
    role: Optional[str] = Query(None),
    userId: Optional[str] = Query(None),
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    db_path: Optional[str] = None,
) -> dict:
    """
    Returns reviews for the manuscript, including recommendation, comments, and requested_changes.
    Allowed for roles editor and author (not unassigned reviewers).
    Default round = current_round.
    """
    eff_role = x_role if isinstance(x_role, str) else (role if isinstance(role, str) else None)
    eff_user = x_user_id if isinstance(x_user_id, str) else (userId if isinstance(userId, str) else None)

    m = engine.get_manuscript(id, db_path=db_path)
    if not m:
        raise HTTPException(status_code=404, detail="Manuscript not found")

    target_round = round if isinstance(round, int) else m["current_round"]

    if eff_role in ("editor", "author"):
        pass  # allowed
    elif eff_role == "reviewer":
        # Must be assigned to this manuscript
        try:
            engine.require_role(id, eff_user, "reviewer", ["reviewer"], db_path=db_path)
        except engine.RoleAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=f"Unassigned reviewer cannot view reviews: {exc}")
    else:
        raise HTTPException(status_code=403, detail="Role not permitted to view reviews")

    reviews = engine.get_reviews(manuscript_id=id, round_no=target_round, db_path=db_path)
    return {"manuscript_id": id, "round_no": target_round, "reviews": reviews}


@app.get("/manuscripts/{id}/review-summary", summary="Get reviewer recommendations summary")
def get_review_summary_endpoint(
    id: str,
    round: Optional[int] = Query(None, description="Filter summary by round number"),
    role: Optional[str] = Query(None),
    userId: Optional[str] = Query(None),
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    db_path: Optional[str] = None,
) -> dict:
    """
    Editor only. Returns each assigned reviewer for the round with status
    'submitted' or 'pending', recommendation, comments, and requested_changes.
    """
    eff_role = x_role if isinstance(x_role, str) else (role if isinstance(role, str) else None)
    eff_user = x_user_id if isinstance(x_user_id, str) else (userId if isinstance(userId, str) else None)

    m = engine.get_manuscript(id, db_path=db_path)
    if not m:
        raise HTTPException(status_code=404, detail="Manuscript not found")

    if eff_role != "editor":
        raise HTTPException(status_code=403, detail="Editor role required to view review summary")

    target_round = round if isinstance(round, int) else m["current_round"]
    summary = engine.get_review_summary(manuscript_id=id, round_no=target_round, db_path=db_path)
    return {
        "manuscript_id": id,
        "round_no": target_round,
        "summary": summary,
        "reviewers": summary,
    }


@app.post("/manuscripts/{id}/request-revision", summary="Editor requests author revision")
def request_revision(
    id: str,
    req: RequestRevisionRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    request_revision: Transitions UNDER_REVIEW -> REVISION_REQUESTED.
    Editor only. Blocks if assigned reviews are pending, unless override=True.
    Stores revision_notes with decision.
    """
    role = x_role or req.role
    user_id = x_user_id or req.editorId
    ensure_manuscript_exists(id, user_id, role)
    notes = req.revision_notes if req.revision_notes is not None else (req.revisionNotes or req.comments)
    try:
        engine.require_role(id, user_id, role, ["editor"])
        return engine.request_revision(
            manuscript_id=id,
            editor_id=user_id,
            comments=notes,
            revision_notes=notes,
            override=req.override,
        )
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.StatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/manuscripts/{id}/revisions", summary="Author submits revised manuscript")
def submit_revision(
    id: str,
    req: SubmitRevisionRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    submit_revision: Transitions REVISION_REQUESTED -> REVISED.
    Author only. Editors cannot call this endpoint.
    Saves safety checkpoint, increments round_no, creates milestone snapshot
    with parent_ids pointing to previous submitted version.
    """
    role = x_role or req.role
    user_id = x_user_id or req.userId
    ensure_manuscript_exists(id, user_id, role)
    try:
        engine.require_role(id, user_id, role, ["author"])
        return engine.submit_revision(
            manuscript_id=id,
            author_id=user_id,
            lamport=req.lamport,
            text=req.text,
        )
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.StatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/manuscripts/{id}/decision", summary="Editor issues Accept, Reject, or Desk-Reject decision")
def post_decision(
    id: str,
    req: DecisionRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    decision: Transitions UNDER_REVIEW -> ACCEPTED or REJECTED.
    Also allows SUBMITTED -> REJECTED (desk-reject).
    Editor only. Blocks if reviews pending unless override=True.
    """
    role = x_role or req.role
    user_id = x_user_id or req.editorId
    ensure_manuscript_exists(id, user_id, role)
    try:
        engine.require_role(id, user_id, role, ["editor"])
        return engine.make_decision(
            manuscript_id=id,
            editor_id=user_id,
            decision=req.decision,
            override=req.override,
        )
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.StatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/manuscripts/{id}/publish", summary="Publish accepted manuscript")
def publish_manuscript(
    id: str,
    req: PublishRequest,
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    publish: Transitions ACCEPTED -> PUBLISHED.
    Editor only. Creates final milestone snapshot version and permanently locks manuscript.
    """
    role = x_role or req.role
    user_id = x_user_id or req.editorId
    ensure_manuscript_exists(id, user_id, role)
    try:
        engine.require_role(id, user_id, role, ["editor"])
        return engine.publish_manuscript(
            manuscript_id=id,
            editor_id=user_id,
            lamport=req.lamport,
        )
    except engine.DocumentLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.StatusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except engine.RoleAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/manuscripts/{id}/compare", summary="Compare two versions (difflib)")
def compare(
    id: str,
    a: int = Query(..., description="Base version number"),
    b: int = Query(..., description="Target version number"),
) -> dict:
    """
    compare: difflib-based version comparator labelled as mocked E3 diff.
    Returns unified diff lines and typed token chunks for visual diff rendering.
    """
    try:
        return engine.compare_versions(doc_id=id, ver_a=a, ver_b=b)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/manuscripts/{id}/workflow", summary="Get manuscript workflow state and allowed actions")
def get_workflow(
    id: str,
    userId: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    x_role: Optional[str] = Header(None, alias="X-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> dict:
    """
    workflow: Returns manuscript status, rounds, reviewer assignments, reviews,
    decisions, and allowed actions for the active role.
    """
    eff_role = x_role or role
    eff_user = x_user_id or userId
    return engine.get_workflow(
        manuscript_id=id,
        user_id=eff_user,
        role=eff_role,
    )


# ---------------------------------------------------------------------------
# Audit, Stats & Config Endpoints
# ---------------------------------------------------------------------------

@app.post("/audit", summary="Append an audit log event")
def post_audit(req: AuditRequest) -> dict:
    """FR4.6: Manually append an audit event."""
    engine.add_audit(
        user_id=req.userId,
        doc_id=req.docId,
        event_type=req.eventType,
        version_ref=req.versionRef,
    )
    return {"status": "ok"}


@app.get("/audit", summary="Paginated audit log")
def get_audit(
    docId: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    """FR4.6: Returns paginated audit log for *docId*, newest first."""
    return engine.get_audit(doc_id=docId, page=page, page_size=page_size)


@app.get("/stats", summary="Storage efficiency statistics")
def stats(docId: str = Query(...)) -> dict:
    """Returns storage statistics comparing stored bytes vs full text bytes."""
    return engine.get_stats(doc_id=docId)


@app.get("/config", summary="Read current engine configuration")
def get_config() -> dict:
    """Returns current SNAPSHOT_EVERY value and simulate_failure flag."""
    return {
        "snapshot_every": engine.SNAPSHOT_EVERY,
        "simulate_failure": engine.get_simulate_failure(),
    }


@app.post("/config", summary="Update engine configuration")
def set_config(req: ConfigRequest) -> dict:
    """Update SNAPSHOT_EVERY cadence."""
    if req.snapshotEvery < 1:
        raise HTTPException(status_code=400, detail="snapshotEvery must be >= 1")
    engine.SNAPSHOT_EVERY = req.snapshotEvery
    return {"snapshot_every": engine.SNAPSHOT_EVERY}


# ---------------------------------------------------------------------------
# Debug / demo controls
# ---------------------------------------------------------------------------

@app.post("/debug/fail-next-write", summary="Simulate DB failure on next write")
def fail_next_write() -> dict:
    """Sets simulate-failure flag to prove atomic rollback."""
    engine.set_simulate_failure(True)
    return {"simulate_failure": True}


@app.post("/debug/corrupt/{docId}/{versionNo}", summary="Corrupt a stored version")
def corrupt(docId: str, versionNo: int) -> dict:
    """Flips the first character in content_or_delta to trigger SHA-256 mismatch."""
    ok = engine.corrupt_version(doc_id=docId, version_no=versionNo)
    if not ok:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"corrupted": True, "doc_id": docId, "version_no": versionNo}


@app.post("/debug/reset", summary="Reset all data for a document")
def reset(docId: str = Query(...)) -> dict:
    """Deletes all data for *docId* (demo only)."""
    engine.reset_document(doc_id=docId)
    return {"reset": True, "doc_id": docId}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
