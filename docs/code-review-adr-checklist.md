# Code Review Checklist — ADR Integration

**Purpose**: Use this when reviewing PRs to ensure changes align with architectural decisions  
**How to use**: Find the module being changed, consult relevant ADRs, verify compliance

---

## **Quick Module → ADR Map**

| Module Changed | Read These ADRs | Verify |
|---|---|---|
| **harvester.py** | ADR-011, 012, 013, 014 | Dedup still works? Paging logic? Chunking strategy? |
| **extractor.py** | ADR-015, 016, 025 | Literature note format? Dedup timing? Prompting? |
| **review.py** | ADR-016, 017, 018 | Approval flow? Thresholds? Validation? |
| **retrieval.py** | ADR-003, 009, 010 | RRF fusion? Relevance floor? Graph expansion? |
| **connector.py** | ADR-003, 009, 010, 025 | RAG retrieval? Graph? Prompting? |
| **gardener.py** | ADR-019, 021, 025 | Clustering? Routing? Prompting? |
| **gardener_hub.py** | ADR-020, 021, 025 | Hub selection? Graph expansion? LLM routing? |
| **web.py, web_app.py** | ADR-022, 023, 018 | Server rendering? Job queue? Validation? |
| **state.py, index.py** | ADR-001, 002, 005, 008 | Persistence? Dual-store? Repository pattern? |
| **config.py** | ADR-004, 006 | YAML-first? Pydantic? |
| **llm.py** | ADR-024, 025 | Multi-provider? Prompt caching? |
| **cli.py** | ADR-026 | Typer/Rich framework? |

---

## **Code Review Workflow**

### **Step 1: Identify Module**
```
What files are being changed?
Example: harvester.py + paging.py
```

### **Step 2: Consult ADRs**
```
Look up relevant ADRs from the table above
Example: ADR-011 (3-layer dedup), ADR-013 (page inference)
```

### **Step 3: Check Compliance**
```
Read the ADR's "Pros and Cons" section:
  - Does this change violate any cons?
  - Does it still satisfy the pros?
  - Are dependencies still honored?
```

### **Step 4: Ask Questions**
```
If change looks risky:
  - "Does this still satisfy ADR-XXX?"
  - "Have you considered the trade-off in ADR-XXX?"
  - "This touches ADR-XXX decision — agreed?"
```

### **Step 5: Approve/Request Changes**
```
✅ Approve if:
  - Change aligns with relevant ADRs
  - No new architectural pattern introduced
  - Tests pass

❌ Request changes if:
  - Violates ADR guidance
  - Introduces new pattern without ADR
  - Relevant ADR was ignored
```

---

## **Review Comments Template**

### **When change aligns with ADR:**
```
✅ Good — this aligns with ADR-XXX (reason).
See docs/adrs/generated/MODULE/ADR-XXX-*.md for context.
```

### **When change violates ADR:**
```
❌ This violates ADR-XXX (specific con/consequence).
Either:
1. Adjust the change to comply with ADR
2. Update the ADR with new reasoning (and create issue)

See docs/adrs/generated/MODULE/ADR-XXX-*.md
```

### **When new pattern is introduced:**
```
⚠️ This introduces a new architectural pattern not covered by ADRs.
Either:
1. Show it fits an existing ADR
2. Create a GitHub issue for a new ADR

See docs/adrs/RUNBOOK.md section "When to Create a New ADR"
```

### **When dependency might be broken:**
```
⚠️ This might affect ADR-YYY which depends on ADR-XXX.
Check docs/adrs/ADR-OVERVIEW.md for relationship map.
Are you also updating ADR-YYY?
```

---

## **Critical ADRs (Always Check)**

These 6 are load-bearing — changes here cascade:

### **ADR-001: SQLite+WAL+FTS5**
```
✅ Check if: Adding persistence, schema changes, transaction logic
❌ Red flags: 
  - Removing WAL mode
  - Adding new persistence layer
  - Breaking FTS5 usage
Escalate to: Database architect
```

### **ADR-003: Hybrid Retrieval**
```
✅ Check if: Modifying search, RRF fusion, relevance floor
❌ Red flags:
  - Removing RRF fusion
  - Disabling relevance floor
  - Changing without threshold validation
Escalate to: Retrieval specialist
```

### **ADR-005: Dual-Store Persistence**
```
✅ Check if: Touching SQLite + ChromaDB together
❌ Red flags:
  - New cross-store transaction logic
  - Inconsistency windows not documented
  - Manual reconciliation bypassed
Escalate to: Data consistency owner
```

### **ADR-007: Layered Hashing**
```
✅ Check if: Modifying dedup, caching, hashing
❌ Red flags:
  - Changing normalization rules
  - Removing hash layers
  - Breaking deterministic caching
Escalate to: Dedup/caching owner
```

### **ADR-018: Web/CLI Validation**
```
✅ Check if: Modifying review approval, web validation
❌ Red flags:
  - Adding bypass to threshold validation
  - Asymmetry between web and CLI
  - Validation removed
Escalate to: Security reviewer
```

### **ADR-024/025: LLM Integration**
```
✅ Check if: Adding LLM calls, prompts, caching
❌ Red flags:
  - Using provider directly (not get_llm())
  - Prompt not split with <!-- zettel:user -->
  - Caching bypassed
Escalate to: LLM integration owner
```

---

## **Review Decision Tree**

```
Does the PR touch a critical module?
├─ YES (INFRA, RETRIEVAL, HARVEST)
│  ├─ All relevant ADRs listed in PR?
│  │  ├─ YES → Check compliance
│  │  └─ NO → Request addition (ask PR author to link ADRs)
│  ├─ Changes comply with ADRs?
│  │  ├─ YES → Approve (if tests pass)
│  │  └─ NO → Request changes (explain which ADR violated)
│  └─ New pattern introduced?
│     ├─ YES → Create issue for new ADR (before merging)
│     └─ NO → Approve
│
└─ NO (WEB, CLI, LLM, other)
   ├─ Any ADRs affected?
   │  ├─ YES → Check compliance (same as above)
   │  └─ NO → Standard code review
   └─ Approve if tests pass
```

---

## **Example Reviews**

### **Review #1: ADR-Compliant Change**

```
PR: "Optimize retrieval thresholds"
Files: retrieval.py, config.py

❌ Initial check: No ADRs listed in PR description

✏️ Comment:
"Please link relevant ADRs in PR description. 
This touches retrieval, so check:
  - ADR-003 (Hybrid retrieval)
  - ADR-010 (Result transparency)
  - ADR-007 (Hashing)

See docs/code-review-adr-checklist.md"

→ Author updates PR description
✅ Second check: ADRs listed + compliant → APPROVE
```

### **Review #2: ADR Violation**

```
PR: "Add new dedup layer to harvest"
Files: harvester.py, state.py

✓ ADRs listed: ADR-011, ADR-012, ADR-014

❌ Issue found:
PR removes layer-2 (extraction hash) dedup.
ADR-011 specifies 3-layer detection for a reason:
  "Layer 1 cheap, Layer 2 catches cross-format, Layer 3 semantic"

✏️ Comment:
"This violates ADR-011 (3-layer duplicate detection).
Removing layer-2 means cross-format re-exports won't be detected.

Either:
1. Keep layer-2 in the dedup flow
2. Create issue to update ADR-011 with new reasoning

See docs/adrs/generated/HARVEST/ADR-011-*.md"

→ Author adjusts OR creates ADR update issue
✅ Revised PR → APPROVE
```

### **Review #3: New Pattern**

```
PR: "Add Redis cache for retrieval results"
Files: retrieval.py, config.py

❌ Issue found:
PR introduces Redis (new cache layer).
This is a NEW architectural decision NOT covered by ADRs.

✏️ Comment:
"This introduces a new architectural pattern (Redis caching).
Before merging, we should document this as an ADR.

Please:
1. Create GitHub issue: 'ADR-XXX: Redis caching layer'
   Reference: docs/adrs/RUNBOOK.md section 'When to Create a New ADR'
2. Discuss trade-offs with team
3. Update ADR if decision is to keep Redis

See docs/adrs/ACTION-PLAN-2026-08-31.md for ADR creation process"

→ Author creates ADR issue
⏸️ Pause review until ADR is drafted
✅ After ADR approval → APPROVE PR
```

---

## **PR Author Checklist**

Before submitting a PR, check this:

```
Module being changed?
  └─ Look up in "Quick Module → ADR Map" above

ADRs listed in PR description?
  └─ Copy template from .github/pull_request_template.md

Changes comply with ADRs?
  └─ Read each relevant ADR's "Pros and Cons"
  └─ Verify no cons are violated

New architectural pattern?
  └─ If YES: Create GitHub issue for new ADR first
  └─ If NO: Continue

Ready to submit!
```

---

## **Key Principles**

1. **ADRs are reference, not gospel**
   - But changing them requires conscious decision + issue

2. **Compliance is the default expectation**
   - Violating ADR = friction, not blockers
   - But friction should surface team discussion

3. **New patterns require documentation**
   - Small patterns can be absorbed into existing ADRs
   - Large patterns get their own ADR issue

4. **Code review is collaborative**
   - Reviewer points out ADR implications
   - Author explains reasoning or updates ADR
   - Together, you strengthen both the code and the architecture

---

## **Questions?**

- "Which ADRs apply here?" → See "Quick Module → ADR Map"
- "Does this violate an ADR?" → Read the ADR's "Consequences" section
- "Should we change the ADR?" → Create GitHub issue, discuss
- "I don't know what to do" → Escalate (see "Critical ADRs" section)

---

**Document location**: `docs/code-review-adr-checklist.md`  
**For quick ref**: `docs/adrs/RUNBOOK.md`  
**For full details**: `docs/adrs/ADR-INDEX.md`
