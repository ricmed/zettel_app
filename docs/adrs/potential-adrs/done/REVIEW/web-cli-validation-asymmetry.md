# Potential ADR: Web/CLI Validation Asymmetry in Auto-Approve Threshold Enforcement

**Module**: REVIEW  
**Category**: Security / Validation / Web/CLI Parity  
**Priority**: Consider (Score: 75)  
**Date Identified**: 2026-08-30

---

## Existing ADR Context

**KNOWN INCONSISTENCY**: Per mapping.md analysis (Cross-Cutting Concerns, noted in component review):
- Web `/review/action` path enforces auto-approve confidence threshold **only client-side**
- CLI enforces threshold **server-side** in `run_review()` function
- Web's manual review operation bypasses `literature_review.auto_approve_min_confidence` entirely
- Mapping explicitly flags this as "a candidate worth examining in Phase 2 for either a fix or an ADR on intended web/CLI parity"

---

## What Was Identified

The system exhibits asymmetric validation of the auto-approve confidence threshold between the CLI and web UI:

### CLI Behavior (review.py:160-206, server-side)
```python
def run_review(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    *, source_id: str | None = None,
    auto_approve: bool = False,
    interactive: bool = True,
    ...
) -> dict[str, int]:
    ...
    chunks = db.get_chunks_by_status("awaiting_review", source_id=source_id)
    limiar = cfg.literature_review.auto_approve_min_confidence
    ...
    if auto_approve or not interactive:
        for chunk in chunks:
            conf = chunk.get("review_confidence") or 0
            if conf >= limiar:  # ← SERVER-SIDE CHECK
                if approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                    stats["approved"] += 1
```

The threshold check is **enforced on the server** in the non-interactive code path.

### Web UI Behavior

**Client-Side Filtering (web.py:458-462)**:
```python
@app.get("/review", response_class=HTMLResponse)
async def review(request: Request, ...):
    ...
    if confidence in {"low", "medium", "high"}:
        threshold = _service(request).cfg.literature_review.auto_approve_min_confidence
        enriched = [c for c in enriched if
                    ("low" if (c.get("review_confidence") or 0) < .4 else
                     "medium" if (c.get("review_confidence") or 0) < threshold else "high") == confidence]
```

The threshold is used to **filter chunks for display only**. No server-side re-validation.

**Server-Side Dispatch (web_app.py review handler)**:
```python
if operation == "review":
    from zettel.review import approve_chunk, finalize_approved_concepts, reject_chunk
    ...
    chunk_ids = list(payload.get("chunk_ids") or [])
    ...
    for number, chunk_id in enumerate(chunk_ids, 1):
        ...
        ok = (
            approve_chunk(cfg, db, idx, chunk_id)
            if action == "approve" else reject_chunk(cfg, db, idx, chunk_id)
        )
```

The web endpoint **approves whatever chunk_ids are sent**, with **no validation of the confidence threshold**.

### The Asymmetry

| Path | Threshold Validation | Enforcement |
|------|---------------------|------------|
| **CLI non-interactive** | Server-side | In `run_review()` loop: `if conf >= limiar` |
| **Web `/review/action`** | Client-side only | Filtering in browser; POST endpoint accepts any chunk_ids |
| **Web manual review** | None | Completely bypasses threshold logic |

**Consequence**: A user could manually craft a POST request to `/review/action` with chunk_ids that don't meet the threshold, and they would be approved without checking.

---

## Why This Might Deserve an ADR

- **Security & Predictability**: The auto-approve threshold is meant to be a configurable gate (`literature_review.auto_approve_min_confidence`, default 0.7). If one code path enforces it server-side and another doesn't, the system's behavior is unpredictable. An operator tuning the threshold would see different results depending on which interface they use.

- **Inconsistent Model**:
  - CLI treats threshold as a **contract**: all non-interactive approvals respect it
  - Web treats threshold as a **UI hint**: displayed in bands, but not validated on approval
  - This difference is not documented anywhere; maintainers might assume both enforce it uniformly

- **Web-Layer Governance**: The web layer represents the system to external users (if ever exposed beyond local). Inconsistent validation suggests unclear governance of where input validation belongs — client vs. server.

- **Design Intent Unknown**: Two possibilities:
  1. **Bug**: Oversight in web_app.py — should re-validate threshold before approval
  2. **Intentional**: Web UI is meant to allow bypassing thresholds as a convenience (different from CLI strictness)
  
  The mapping notes this is "a candidate worth examining in Phase 2 for either a fix or an ADR on **intended** web/CLI parity" (emphasis on "intended"). Until that intent is clarified, the asymmetry is ambiguous.

- **Team Knowledge**: Developers implementing web features or tuning threshold configuration need to understand which enforcement applies where. Current state risks silent bugs (e.g., a developer assumes web also enforces, and deploys untested code).

- **Stability**: The asymmetry has existed since the web UI was introduced (2026-08-29 commit 5d9b504). No recent changes suggest it's intentional rather than overlooked.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/review.py`](../../../zettel/review.py) - Lines 160-206: CLI path with server-side threshold check
- [`zettel/web.py`](../../../zettel/web.py) - Lines 450-469: Web GET route with client-side filtering + POST endpoint without validation
- [`zettel/web_app.py`](../../../zettel/web_app.py) - Review operation handler (no threshold check before approval)

### Code Evidence

**CLI Path (review.py:194-206, server-side enforcement)**:
```python
if auto_approve or not interactive:
    for chunk in chunks:
        conf = chunk.get("review_confidence") or 0
        if conf >= limiar:  # ← ENFORCED HERE
            if approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                stats["approved"] += 1
            else:
                stats["skipped"] += 1
        else:
            stats["skipped"] += 1
```

**Web GET Route (web.py:458-462, client-side filtering only)**:
```python
if confidence in {"low", "medium", "high"}:
    threshold = _service(request).cfg.literature_review.auto_approve_min_confidence
    enriched = [c for c in enriched if
                ("low" if (c.get("review_confidence") or 0) < .4 else
                 "medium" if (c.get("review_confidence") or 0) < threshold else "high") == confidence]
    # ↑ Filters for DISPLAY only; no server-side validation on approval
```

**Web POST Endpoint (web.py:472-477, no validation)**:
```python
@app.post("/review/action")
async def review_action(request: Request, action: str = Form(...), csrf: str = Form(""),
                        chunk_ids: list[str] = Form(default=[])):
    if action not in {"approve", "reject"}:
        return HTMLResponse("Ação inválida", status_code=400)
    return _post_job(request, "review", {"action": action, "chunk_ids": chunk_ids}, csrf)
    # ↑ Enqueues without checking if chunk_ids meet threshold
```

**Web Job Dispatch (web_app.py, review operation handler)**:
```python
if operation == "review":
    ...
    chunk_ids = list(payload.get("chunk_ids") or [])
    ...
    for number, chunk_id in enumerate(chunk_ids, 1):
        ...
        ok = (
            approve_chunk(cfg, db, idx, chunk_id)
            if action == "approve" else reject_chunk(cfg, db, idx, chunk_id)
        )
    # ↑ No validation of chunk confidence against limiar
```

### Impact Analysis
- **Introduced**: Web endpoint introduced 2026-08-29 17:15:35 (commit 5d9b504)
- **Validation Location**: 
  - CLI: `review.py` line 197 (server-side)
  - Web: `web.py` lines 458-462 (client-side only)
- **Risk Surface**: Any actor with web access can approve below-threshold chunks
- **Audit Trail**: Approved chunks are recorded in SQLite, but there's no flag for "approved via web bypass" vs. "approved via threshold"

---

## Alternatives (Implicit Decision)

| Option | Pros | Cons |
|--------|------|------|
| **Current (asymmetric)** | Web UI has flexibility; operators can override if needed | Unpredictable; threshold not truly a gate; confusing for team |
| **Enforce server-side everywhere** | CLI and web both respect threshold; predictable; security boundary clear | Reduces web UI flexibility; may frustrate operators who want to override |
| **Move threshold to config, not enforcement** | Acknowledge threshold as advisory, not mandatory; document clearly | Defeats the purpose of a threshold; invites inconsistent usage |
| **Document the asymmetry explicitly** | Intent becomes clear; less likely to be accidentally changed | Doesn't fix the inconsistency, just explains it |

---

## Questions to Address in ADR (if created)

1. **Intended Design**: Is the web UI meant to allow bypassing the threshold as a convenience? Or is this an oversight? (The mapping's "either a fix or an ADR" phrasing suggests this is unresolved.)

2. **Operator Expectations**: Do operators expect the threshold to be honored in both CLI and web, or do they view the web UI as a separate "admin override" path?

3. **Audit & Compliance**: If thresholds are meant to be guardrails (e.g., "auto-approve only high-confidence"), should there be an audit flag when they're bypassed?

4. **Future Scope**: If the web UI is extended (e.g., shared with team members), what validation should apply?

---

## Related Potential ADRs
- **Confidence-Scored HITL Approval with Band-Based UX** (REVIEW): Related — the threshold is a key part of the band-based strategy. This ADR is about where validation happens, not the strategy itself.
- **Dual-Store Persistence** (INFRA): Related — approving a chunk updates both SQLite and Chroma; validation asymmetry could lead to inconsistent state if thresholds are bypassed.

---

## Additional Notes

- **PT-BR Terminology**: The configuration key is `literature_review.auto_approve_min_confidence` (English); UI labels and logs use PT-BR terms like "limiar" (threshold), "faixa" (band), "confianca" (confidence).
- **Code Comments**: No comment in web.py explains why the threshold is client-side only. This absence suggests the asymmetry may be unintentional.
- **Testing Gap**: No test fixtures cover the web `/review/action` endpoint behavior. The mapping notes: "the web `/review/action` path enforces the auto-approve confidence threshold only client-side, and the web's manual review operation bypasses `literature_review.auto_approve_min_confidence` entirely — **a candidate worth examining in Phase 2 for either a fix or an ADR**."
- **Recommendation for Resolution**: Before generating a formal ADR, clarify with the team whether this is:
  - A bug to fix (move validation to server-side in web_app.py)
  - An intentional design (document explicitly and add tests)
  - A known limitation (document in CLAUDE.md or README)
