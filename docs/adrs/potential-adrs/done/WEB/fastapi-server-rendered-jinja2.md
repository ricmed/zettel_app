# Potential ADR: FastAPI + Server-Rendered Jinja2 Templates (No SPA/JS Build)

**Module**: WEB
**Category**: Architecture / Primary Framework / Presentation Layer
**Priority**: Must Document (Score: 145)
**Date Identified**: 2026-08-30

---

## Existing ADR Context

No related ADRs identified. This is the sole foundational decision for the web layer.

---

## What Was Identified

The Zettelkasten web interface is built as a **server-rendered FastAPI application using Jinja2 templates**, with no client-side JavaScript framework, no build step, and no SPA (Single Page Application) architecture. Every web page is rendered on the server and returned as complete HTML to the browser; forms submit to server endpoints which return either redirects or full page HTML responses.

This was introduced on 2026-08-29 (commit 5d9b504: "Implement Python-first Zettelkasten web interface with secure uploads, persistent worker queue, progress, review, dashboards, documentation, and tests"). The decision represents a fundamental architectural choice about presentation layer design — favoring simplicity and Python-only development over modern frontend frameworks.

**Key characteristics**:
- **Framework**: FastAPI + Uvicorn (single-process, no separate API/frontend services)
- **Template engine**: Jinja2 with 14 template files under `zettel/templates/`
- **Styling**: 3 static CSS files, no CSS-in-JS or preprocessors
- **Form handling**: HTML forms with server-side validation, POST/GET redirect pattern
- **No build tooling**: No Node, webpack, npm, TypeScript, or other frontend build infrastructure
- **Language consistency**: All presentation logic written in Python (Typer decorators → Jinja context → HTML rendering)

## Why This Might Deserve an ADR

- **Impact**: Defines the entire presentation layer architecture. Every web page, form, and interaction flows through FastAPI routes → Jinja2 context → HTML response.
- **Foundational choice**: Constrains how web features are added (must use Python/Jinja, server-side form validation, no client-side SPAs).
- **Team knowledge**: Every developer working on the web UI must understand FastAPI routing, Jinja2 templating, form submission patterns, and the absence of JavaScript tooling.
- **Cost to change**: Switching to a SPA (React/Vue/Angular) would require 6+ months of work: extract API layer, build frontend framework, restructure data flow, manage state on client vs. server.
- **Temporal stability**: Stable for ~1 day (as of 2026-08-30); no pressure to change yet, but early enough to reconsider if requirements shift.
- **Long-term consequences**: This decision locks in Python-only frontend development; any future need for client-side interactivity (real-time dashboards, collaborative editing, etc.) will have friction.

## Evidence Found in Codebase

### Key Files
- [`zettel/web.py`](../../../zettel/web.py) - Lines 1-30: Framework setup, Jinja2 template directory configuration
  - `templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))`
  - 23 HTTP endpoints (all returning `HTMLResponse`, `RedirectResponse`, `JSONResponse` for SSE only)
- [`zettel/web_app.py`](../../../zettel/web_app.py) - Application layer (no HTTP concerns, pure business logic)
- [`zettel/templates/`](../../../zettel/templates/) - 14 Jinja2 templates (base.html, dashboard.html, documents.html, pipeline.html, review.html, notes.html, mocs.html, runs.html, settings.html, etc.)
- [`zettel/static/`](../../../zettel/static/) - 3 CSS files (style.css + utility stylesheets)

### Code Evidence

**Framework initialization (web.py)**:
```python
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from zettel.web_app import WebApplication

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    service = WebApplication(...)
    yield
    service.stop()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
```

**Form submission pattern (web.py, ~line 400)**:
```python
@app.post("/harvest/submit", response_class=RedirectResponse)
def submit_harvest(request: Request, file: UploadFile = File(...), form_data: str = Form(...)):
    # Validate, enqueue job, return redirect with job_id
    return RedirectResponse(url=f"/runs/{job_id}", status_code=303)
```

**Template context rendering (web.py, ~line 150)**:
```python
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    service = getattr(request.app.state, "service", None)
    if not service:
        return templates.TemplateResponse("login.html", {"request": request})
    dashboard_data = service.db.get_web_dashboard()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": dashboard_data["stats"],
        "recent_runs": dashboard_data["recent_runs"],
    })
```

**No JavaScript framework usage**:
- `grep -r "import.*react\|from.*react\|import.*vue\|import.*angular" zettel/` → No results
- `grep -r "export\|import.*from" zettel/static/` → No results (CSS only)
- No `package.json`, `webpack.config.js`, `tsconfig.json`, or other JS build files

### Impact Analysis

- Introduced: 2026-08-29 (commit 5d9b504)
- Modified: ~5 commits since introduction (mostly bug fixes and test coverage, not architectural)
- Scope: All 23 web endpoints + 14 templates (19+ files, ~1,000+ lines of Python)
- Affects: Every web feature implementation, form handling, page navigation, job monitoring dashboard
- Cost to change: 6+ months (complete SPA migration or API-first refactoring)
- Team knowledge requirement: Essential for anyone working on web UI

## Questions to Address in ADR (if created)

1. **Why reject SPA architecture?** What are the trade-offs between server-render simplicity vs. client-side interactivity?
2. **Is this a permanent decision or a v1 convenience?** Should the project plan for eventual SPA migration if real-time features (collaborative editing, live search, etc.) become required?
3. **How does this constraint interact with the monolithic Python-first architecture?** Is keeping everything in Python more important than separation of concerns?
4. **What are the performance/latency implications?** Every interaction requires a full page reload + server round-trip.
5. **How will this evolve if the web UI becomes the primary interface** (instead of the CLI)?

## Related Potential ADRs

- **WEB: SQLite-Backed Persistent Job Queue** — Complements this decision; no need for real-time client-side job polling since jobs are durably persisted
- **WEB: HMAC-Signed Session Cookies + CSRF** — Security layer built on server-render assumption (session state managed server-side)
- **INFRA: Repository Pattern for Data Access** — Web routes delegate to service layer (`WebApplication`), maintaining clean separation

## Additional Notes

- **No JavaScript tooling overhead**: Simplifies deployment (no separate CI/CD for frontend builds, no npm dependencies to manage)
- **Python skill alignment**: Team can implement web features without learning JavaScript/TypeScript, keeping skill set focused on Python
- **Markdown rendering**: Server-side markdown → HTML conversion via `markdown_it` + `bleach` sanitization (no client-side rendering)
- **Real-time updates**: Relies on SSE (Server-Sent Events) for job progress streaming, not WebSockets (simpler, unidirectional)
- **Form validation**: Client-side HTML5 validation + server-side re-validation (defensive programming pattern)
- **CSS-in-HTML**: Inline styles + utility classes, no Tailwind/CSS-in-JS frameworks

---

## Temporal Context

**Git timeline**:
- 2026-08-29: Initial introduction (5d9b504)
- 2026-08-30: Markdown link rendering refinement (4c321c0)
- 2026-08-31: Dependency updates + test expansion (fe7bf5c, 9689075)
- 2026-09-02: Bug fixes + harvest feedback (9a16045)

**Stability**: Stable for ~4-5 days; no major reworks or rollbacks; core pattern intact despite incremental improvements.
