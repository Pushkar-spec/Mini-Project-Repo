# VersionVault: Journal Publication and Manuscript Management
## E2 Checkpoint & Version Persistence Engine with Manuscript Workflow

> **Demo scope:** This is a **standalone, runnable demo** of **Engine E2** (Checkpoint & Version Persistence) extended with an academic **Journal Publication & Manuscript Governance Workflow**.
>
> | Engine | Role in System | Status in this Demo |
> |--------|----------------|---------------------|
> | **E1 – Live Collaboration** | WebSocket sync, CRDT/Yjs, operational transform | Simulated by the single-editor UI & Lamport counter |
> | **E2 – Checkpoint & Persistence** | Snapshot + delta storage, SHA-256 hashing, atomic safety checkpoints, immutable restore, audit trail | **Fully built & verified** |
> | **E3 – Merge / Diff** | Three-way merge, diff colouring | Mocked via built-in `difflib` comparison in the UI |
> | **E4 – Login / Access Control** | Role-based authorization (Author, Editor, Reviewer) | Simulated via role switcher & `require_role()` checks |

---

## Key Features & Invariants Proven

1. **Snapshot + Delta Storage (FR2.3):** Full snapshots every N versions; character-level deltas in between. Milestone versions (`submission`, `revision`, `published`) are **always forced snapshots** and labelled in `milestone`.
2. **SHA-256 Verification & Delta Replay (FR2.4):** Any version is rebuilt from the nearest snapshot + deltas, then verified against its stored SHA-256 digest. Tampered content immediately flags a mismatch.
3. **Atomic Safety Checkpoint (FR2.2):** Before restoring or submitting, a safety version is committed in its own transaction. If any failure occurs, the operation rolls back leaving history pristine.
4. **Non-Destructive History:** Restores append a NEW version whose parent is the target version (`parent_ids = [target]`). Prior history is never overwritten.
5. **8-Step Manuscript State Machine:**
   `DRAFT` → `SUBMITTED` → `UNDER_REVIEW (R1)` → `REVISION_REQUESTED` → `REVISED (R2)` → `UNDER_REVIEW (R2)` → `ACCEPTED` → `PUBLISHED` (and `UNDER_REVIEW` → `REJECTED`). Illegal transitions return `409 Conflict`.
6. **Role-Based Access Control:**
   - **Author (Alice):** Saves drafts, submits manuscripts, submits revisions, restores versions.
   - **Editor (Eric):** Assigns reviewers, requests revisions, issues Accept/Reject decisions, publishes.
   - **Reviewer (Rita & Raj):** Can ONLY read submitted milestone versions of assigned manuscripts; submitting reviews logs `review_submitted`; viewing logs `reviewer_viewed_version`.
7. **Post-Publish Immutable Lock:** Once in `PUBLISHED` status, all write operations (`checkpoint`, `submit`, `restore`, `decision`) are permanently locked with `409 Conflict`.
8. **Lamport Clocks (FR2.6) & Audit Log (FR4.6):** Strictly increasing `max(received, last) + 1` clock; every workflow event is recorded in `audit_log`.

---

## Project Structure

```
e2_demo/
├── main.py            # FastAPI app: routes, contracts, role enforcement, static serving
├── db.py              # SQLite connection helper, schema DDL, idempotent migrations
├── engine.py          # ALL core logic: snapshot/delta, rebuild, SHA-256, safety checkpoint,
│                      #   restore, state machine, reviews, decisions, compare, audit
├── seed_demo.py       # Seeds manuscript-1 (8-step journey) and doc-1 (collaboration)
├── test_engine.py     # 77 pytest tests: requirements, edge cases and defect regressions (DEF-01…05)
├── static/
│   └── index.html     # Single-page UI with Role Switcher, Stepper, Timeline, Reviews, Diff
├── requirements.txt
└── README.md          # Setup instructions & demonstration guide
```

---

## Setup & Running

### 1. Install Dependencies

```bash
cd e2_demo
pip install -r requirements.txt
```

### 2. Run the Test Suite (77 Tests)

```bash
pytest test_engine.py -v
```

All 77 tests should pass in under 10 seconds (47 test functions; one is parametrised over
20 random strings and one over 2 failure points). `TestDefectRegressions` holds the retests
for the Sprint 2 defect register (DEF-01 … DEF-05).

### 3. Seed Demo Data

```bash
python seed_demo.py
```

This seeds:
- `manuscript-1`: Completed 8-step review journey with reviews, decisions, and milestones.
- `doc-1`: Multi-author collaboration session demonstrating standard snapshot/delta cadence.

### 4. Start the Application

```bash
uvicorn main:app --reload
```

The **Demo Controls** (simulate DB failure, corrupt version, reset, snapshot cadence) and
`POST /audit` are **disabled by default** and return `403`. For the evaluator demo, start the
server with demo mode on:

```bash
# Windows PowerShell
$env:VV_DEMO_MODE="1"; uvicorn main:app --reload
# Linux / macOS
VV_DEMO_MODE=1 uvicorn main:app --reload
```

Only registered editors may use the editor role (default `eric`; change with
`VV_EDITORS="eric,other"`). User identity is still **not authenticated** – real login
belongs to the E4 engine.

Open **http://127.0.0.1:8000** in your browser.
Interactive API documentation is at **http://127.0.0.1:8000/docs**.

---

## 5-Minute Demonstration Script for Evaluators

### Step 1: Role Switcher & The 8-Step Stepper
1. Open `http://127.0.0.1:8000`.
2. Notice the **Role Switcher** in the top bar: **Author (Alice)**, **Editor (Eric)**, **Reviewer (Rita)**, **Reviewer (Raj)**.
3. Switch between roles to see dynamic permission updates:
   - When set to **Author**, Editor buttons (Assign Reviewers, Accept/Publish) are hidden/disabled.
   - When set to **Editor**, editing and restore buttons are disabled.
   - When set to **Reviewer**, only the review tools are accessible.
4. Point out the **8-step Stepper** showing the current workflow phase:
   `DRAFT → SUBMITTED → UNDER REVIEW (R1) → REVISION REQUESTED → REVISED (R2) → UNDER REVIEW (R2) → ACCEPTED → PUBLISHED`.

---

### Step 2: Milestone Versions are Always Snapshots (FR2.3)
1. In the middle column **Version Timeline**, observe the badges:
   - 🟣 **INITIAL SUBMISSION** (v4) — forced snapshot milestone.
   - 🔵 **REVISION R1** (v6) — forced snapshot milestone, `parent: [4]` pointing back to the previous submitted version.
   - 🟢 **PUBLISHED VERSION** (v8) — final snapshot milestone.
   - ⚫ **DELTA** versions in between showing character-level compact diffs.
2. Read the **Storage Efficiency Card**: shows ~30% space saved compared to full copies.

---

### Step 3: Peer Reviews, Recommendations Summary & Editor Decisions
1. Click the **Reviews & Decisions** tab in the middle column.
2. Review Round 1 remarks:
   - **Rita:** `MINOR` recommendation.
   - **Raj:** `MAJOR` recommendation requesting formal latency bounds.
3. Switch role to **Editor (Eric)**:
   - Notice the **Reviewer Recommendations Summary** panel listing assigned reviewers and their recommendations (`MINOR`, `MAJOR`, or `pending`).
   - Notice that decisions (`Request Revision`, `Accept`, `Reject`) are gated by pending reviews unless **"Override: decide without all reviews"** is ticked. If overridden, it is explicitly recorded in the audit log (`editor_override_decision`).
   - Desk-reject is also supported directly at `SUBMITTED` status before reviewer assignment.
   - Editors cannot modify content (`/checkpoint` and `/revisions` are strictly restricted to authors).
4. Show the **DECISION: ACCEPT** card issued by Editor Eric in Round 2.

---

### Step 4: Rebuild Trace & SHA-256 Verification (FR2.4)
1. Switch back to the **Version Timeline** tab and click **v6** (Revision R1).
2. Look at the right panel **Preview & Restore**:
   - Rebuilt text is rendered cleanly.
   - **Rebuild trace:** shows exact snapshot used and delta count.
   - **Hash status:** `✅ SHA-256 verified — version integrity intact`.

---

### Step 5: Mocked E3 Diff Compare View
1. Click the **Compare (Mocked E3 Diff)** tab in the right column.
2. Set Version A = `4` (Initial Submission) and Version B = `6` (Revision R1).
3. Click **Compare**:
   - See visual green additions (`<ins>`) and red deletions (`<del>`).
   - Notice added Section 3 benchmarks and Section 4 proofs added by Alice during revision.

---

### Step 6: Corruption Detection via SHA-256 Mismatch
1. Click **v2** or **v5** in the timeline.
2. In the **Demo Controls** bar at the bottom, click **💀 Corrupt Selected Version** and confirm.
3. The right panel immediately updates to:
   `❌ SHA-256 MISMATCH — version CORRUPTED!`
4. Proves the cryptographic integrity check guarantees against undetected bit-rot or silent tampering.

---

### Step 7: Atomic Safety Checkpoint & Non-Destructive Restore (FR2.2)
1. Switch role to **Author (Alice)**.
2. Type some experimental unsaved text into the editor.
3. Click **v4** in the timeline, then click **♻️ Restore This Version** and confirm.
4. Toast notification shows:
   `Safety checkpoint v9 saved. Restored as new v10!`
5. Check the timeline:
   - 🟡 **SAFETY** (v9) holds the unsaved text you had in the editor.
   - 🟢 **RESTORE** (v10) holds v4's content, with `parent: [4]`.
   - History is completely preserved; nothing was overwritten.

---

### Step 8: Atomic Rollback on Simulated DB Failure
1. In Demo Controls, click **⚠️ Simulate DB Failure on Next Write**.
   The amber warning indicator lights up.
2. Try to save a checkpoint or restore a version.
3. An error toast appears: *Simulated DB failure: rolling back transaction*.
4. Notice the version count and database state remain **completely unchanged** — proving `BEGIN IMMEDIATE ... ROLLBACK` atomicity.

---

### Step 9: Post-Publish Permanent Lock
1. When viewing `manuscript-1` (which is in `PUBLISHED` status), see the green lock banner:
   `🔒 MANUSCRIPT PUBLISHED & PERMANENTLY LOCKED`.
2. Any attempt to modify, save checkpoints, or restore returns `409 Conflict`.

---

## API Summary

| Method | Endpoint | Description | Role / Auth |
|--------|----------|-------------|-------------|
| `POST` | `/manuscripts` | Create new manuscript in `DRAFT` | Author |
| `POST` | `/manuscripts/{id}/submit` | Safety checkpoint → snapshot milestone → `SUBMITTED` | Author |
| `POST` | `/manuscripts/{id}/assign-reviewers` | Assign reviewers → `UNDER_REVIEW` | Editor |
| `POST` | `/manuscripts/{id}/reviews` | Submit comments, recommendation & requested_changes | Assigned Reviewer |
| `GET` | `/manuscripts/{id}/reviews` | Fetch reviews with requested_changes (default round = current) | Editor, Author |
| `GET` | `/manuscripts/{id}/review-summary` | Fetch reviewer recommendation summary for round | Editor |
| `POST` | `/manuscripts/{id}/request-revision` | Move status to `REVISION_REQUESTED` (supports `revision_notes`, `override`) | Editor |
| `POST` | `/manuscripts/{id}/revisions` | Safety checkpoint → snapshot milestone → `REVISED` | Author |
| `POST` | `/manuscripts/{id}/decision` | Issue `ACCEPT`, `REJECT`, or desk-reject (supports `override: bool`) | Editor |
| `POST` | `/manuscripts/{id}/publish` | Create published snapshot & lock manuscript | Editor |
| `GET` | `/manuscripts/{id}/compare` | difflib-based comparison between two versions | Author, Editor; Reviewer (milestones only) |
| `GET` | `/manuscripts/{id}/workflow` | Current state, round, reviews, allowed actions | Any |
| `POST` | `/checkpoint` | Save draft version (blocked if published) | Author |
| `GET` | `/versions` | Paginated timeline (newest first) | Author, Editor; Reviewer sees milestones only |
| `GET` | `/versions/{id}/{no}` | Rebuild version & verify SHA-256 | Author, Editor, Assigned Reviewer |
| `POST` | `/restore` | Non-destructive restore with safety checkpoint | Author |
| `GET` | `/audit` | Paginated audit trail | Author, Editor, Assigned Reviewer |
| `GET`/`POST` | `/config` | Read (any) or adjust (demo mode only) snapshot cadence N | Any / `VV_DEMO_MODE=1` |
| `POST` | `/debug/*`, `/audit` | Demo controls and manual audit entry | `VV_DEMO_MODE=1` only |
