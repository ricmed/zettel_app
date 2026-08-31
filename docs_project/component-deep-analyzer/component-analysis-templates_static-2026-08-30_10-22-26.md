# Component Deep Analysis Report — `templates/` + `static/` (Web UI Presentation Layer)

## 1. Executive Summary

`zettel/templates/` (14 Jinja2 files) and `zettel/static/` (3 CSS files) form the entire presentation layer of the server-rendered FastAPI web UI defined in `zettel/web.py`. There is no client-side framework, no bundler, and no JavaScript file in `static/` — the only script in the component is a ~25-line inline `<script>` block inside `zettel/templates/job_detail.html` that polls a JSON endpoint. The component owns zero business logic of its own: every conditional rendered in a template (button disabled states, confidence bands, prerequisite gates) mirrors a decision already made — and re-validated — server-side in `zettel/web.py` / `zettel/web_app.py`. Its responsibilities are strictly: (1) HTML structure and Jinja2 template inheritance, (2) a single shared visual language (CSS custom properties, typography, responsive breakpoints), (3) embedding CSRF tokens into every mutating form, and (4) rendering server-sanitized Markdown (`rendered_body|safe`) for notes and MOCs.

Key findings:
- **Single inheritance root**: all 13 page templates extend `base.html`; only `job_rows.html` is a plain `{% include %}` partial (used by `dashboard.html` and `jobs.html`).
- **Two deliberate autoescape bypasses**: `note_detail.html` and `moc_detail.html` use `{{ rendered_body|safe }}` — the only two `|safe` usages in the entire component — and both consume output already passed through `zettel/markdown.py`'s `bleach.clean()` sanitizer, never raw user/LLM text.
- **CSRF is a pure rendering convention here**: every `<form method="post">` embeds a hidden `csrf` (or `login_csrf`) input sourced from `_context()` in `web.py`; the templates have no way to validate it — validation is entirely server-side (`_csrf_ok`).
- **No CSS framework, no CDN dependency**: all three stylesheets are hand-written, self-contained, and referenced via `/static/...` (served by Starlette's `StaticFiles` mount) — no external network calls, no Google Fonts, no JS libraries.
- **Client-side "business rules" are advisory only**: HTML5 attributes (`required`, `type="number" min="1"`, `accept=".pdf,.md,.markdown,.txt"`, `confirm()` dialogs) improve UX but every constraint they encode is independently re-enforced in `web.py` route handlers — a defense-in-depth pattern, not a validation boundary.
- **Minor styling debt**: `.back` and `.pagination` classes are referenced in 4 templates but have no corresponding rule in any of the 3 CSS files (see § 10).

---

## 2. Data Flow Analysis

### 2.1 Standard page render (GET)

```
1. Browser requests a page (e.g. GET /notes/{note_id})
2. zettel/web.py route handler checks _auth(request) — redirects to /login if no valid session
3. Handler opens StateDB, fetches domain data (e.g. db.get_note(note_id)), closes StateDB
4. For note/MOC bodies only: zettel/markdown.py render_markdown() converts Markdown -> sanitized HTML
5. Handler calls _render() -> _context() merges {request, authenticated, csrf} with route-specific data
6. templates.TemplateResponse renders the named template against zettel/templates/
7. Jinja2 resolves {% extends "base.html" %} -> base.html supplies <head> (CSS links), header/nav, csrf logout form
8. Child template fills {% block content %}; base.html wraps it in <main class="shell">
9. Rendered HTML returned as HTMLResponse to the browser
10. Browser requests linked static assets: /static/app.css, /static/mobile.css, /static/markdown.css
    (served directly by fastapi.staticfiles.StaticFiles, bypassing Jinja2 entirely)
```

### 2.2 Mutating action (POST form submit)

```
1. Template renders a <form method="post" action="..."> with hidden csrf/login_csrf input (value from context)
2. User submits (some forms gated client-side by onsubmit="return confirm(...)")
3. Browser POSTs form-encoded body including the csrf token back to the same-origin route
4. web.py handler re-derives the expected token from the signed session cookie and compares
   via hmac.compare_digest — the template's embedded value is never trusted, only echoed
5. On success: RedirectResponse (303) to a follow-up page (e.g. /jobs/{job_id})
6. On CSRF/validation failure: HTMLResponse with a Portuguese error string, no template re-render
   in most cases (plain text response), except upload/harvest-adjacent errors which re-render
   documents.html with an `error` value surfaced through base.html's {% if error %} alert block
```

### 2.3 Client-side polling (job_detail.html only)

```
1. job_detail.html initial render embeds job.job_id as a JS string literal in an inline <script>
2. setInterval(refresh, 1000) fires every second
3. refresh() calls fetch("/api/jobs/" + id + "?after=" + after) — a JSON endpoint, not a template
4. Response {job, events} is used to mutate DOM text content / classes directly (no re-render)
5. Progress bar width, job-state class, and event list are updated via direct DOM manipulation
6. Once job.state is one of succeeded/failed/interrupted, clearInterval(timer) stops polling
```

### 2.4 Markdown/XSS-sensitive path

```
1. LLM-or-manually-authored note/MOC body (Markdown, PT-BR) stored in StateDB as plain text
2. zettel/markdown.py: MarkdownIt("commonmark", {"html": False}) parses it — raw HTML in the
   source is never interpreted as markup at this stage
3. A custom inline rule rewrites [[ZTL - <id> - label]] wikilinks into <a href="/notes/{id}">
4. bleach.clean() runs a second, allow-listed sanitization pass (tags/attributes/protocols)
5. The resulting HTML string is passed to the template as rendered_body
6. note_detail.html / moc_detail.html render it with {{ rendered_body|safe }} — the ONLY place
   in the component where Jinja2's autoescaping is deliberately bypassed
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Security / Rendering | Only `rendered_body` (pre-sanitized server output) may use `\|safe`; no other Jinja variable is unescaped | templates/note_detail.html:2, templates/moc_detail.html:2 |
| Security / Convention | Every mutating `<form method="post">` embeds a hidden CSRF token sourced from session context | templates/base.html:15, documents.html:3-5, pipeline.html:3, review.html:4, login.html:4 |
| UX / Confirmation gate | Irreversible or batch pipeline actions require a native `confirm()` dialog before submit | templates/documents.html:5 (run-all), templates/review.html:4 (batch approve/reject) |
| UX / Availability mirroring | Pipeline phase buttons are visually disabled when a server-computed prerequisite is unmet | templates/pipeline.html:3-4 |
| UX / Availability mirroring | "Run full pipeline" button disabled when no LLM credential is configured | templates/documents.html:5 |
| Presentation / Truncation | Source excerpt text is truncated to 550 characters (review) / 180 characters (source detail), with a trailing ellipsis only when truncation actually occurred | templates/review.html:5, templates/source_detail.html:2 |
| Presentation / Formatting | Confidence scores are always rendered with exactly 2 decimal places | templates/review.html:5, templates/source_detail.html:2 |
| Presentation / Formatting | Costs are rendered in USD with 4 decimals (aggregate/dashboard) or 6 decimals (per-source detail) | templates/dashboard.html:8, templates/source_detail.html:2 |
| Presentation / Formatting | Timestamps are truncated to the first 10/19 characters of an ISO string (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`) rather than locale-formatted | templates/dashboard.html (via job_rows.html), notes.html, moc_detail.html, note_detail.html |
| Navigation | The active top-nav link is the one whose route `key` equals the `page` variable passed by the calling route | templates/base.html:13-14 |
| Access control (UI reflection) | The navigation bar, logout form, and all page content are rendered only when `authenticated` is true; unauthenticated visits see only the header brand | templates/base.html:11-16 |
| Client-side pre-validation | File upload restricted to `.pdf,.md,.markdown,.txt` and requires a non-empty selection before submit | templates/documents.html:3 |
| Client-side pre-validation | Harvest paging fields (`content_start_file`, `content_start_book`) accept only integers ≥ 1 | templates/documents.html:4 |
| Client-side pre-validation | Login form always submits a fixed, hidden `username=zettel` value alongside the instance secret | templates/login.html:4 |
| Live progress protocol | The job detail page polls `/api/jobs/{id}?after=N` every 1000ms and stops once the job reaches a terminal state | templates/job_detail.html:3-5 |
| Data integrity display | A connection edge whose target note no longer exists in the vault is rendered as plain (unlinked) muted text instead of a hyperlink | templates/note_detail.html:2 |
| Pagination | Review queue "Próxima" link is shown only when the server-computed `has_next` flag is true; "Anterior" only when `review_page > 1` | templates/review.html:5 |

### Detailed breakdown of the business rules

---

### Business Rule: Autoescape bypass is restricted to server-sanitized Markdown output

**Overview**:
Jinja2 templates rendered through `fastapi.templating.Jinja2Templates` autoescape all variable interpolations by default for `.html` files. This component overrides that protection with the `|safe` filter in exactly two locations, both for the same purpose: displaying rendered Markdown bodies of permanent notes and MOCs.

**Detailed description**:
`note_detail.html` and `moc_detail.html` are the only templates in the vault-facing UI that need to display long-form, richly formatted content that originated as Markdown (either LLM-generated during `connect`/`garden`, or manually authored via `zettel new-note`). Because that content can legitimately contain HTML-equivalent structures — headings, lists, blockquotes, tables, code blocks, and internal `[[ZTL - id - label]]` wikilinks that must become real `<a href>` elements — plain Jinja2 escaping would either double-escape already-safe markup or defeat the purpose of rendering Markdown as Markdown. The component's answer is to never hand raw text to `|safe`: the `rendered_body` variable arriving in the template context is always the output of `zettel/markdown.py:render_markdown()`, which first parses the Markdown with HTML disabled (`MarkdownIt("commonmark", {"html": False})`, so any literal `<script>` in the source note is treated as inert text, not markup) and then runs a second, independent `bleach.clean()` pass with an explicit tag/attribute/protocol allow-list (`_ALLOWED_TAGS`, `_ALLOWED_ATTRIBUTES`, `_ALLOWED_PROTOCOLS` in `zettel/markdown.py:44-56`) before the string ever reaches the template layer.

The consequence for this component's boundary is that the template files themselves carry no sanitization logic and must not be trusted to add any — the safety property is established entirely upstream in `zettel/web.py:522` (`rendered_body=render_markdown(note.get("body"))`) and `zettel/web.py:539` for MOCs. `tests/test_web.py::test_note_and_moc_details_render_sanitized_markdown` (lines 277-345) is the executable specification of this rule: it seeds a note body containing a literal `<script>alert('xss')</script>` and a `javascript:` URL, then asserts both are absent from the rendered response (`assert "<script>" not in note.text`, `assert 'href="javascript:' not in note.text`) while confirming that legitimate structures (headings, lists, blockquotes, safe `https://` links, and internal `[[ZTL ...]]` wikilinks rewritten to `/notes/{id}`) do render correctly.

**Rule workflow**:
1. Route handler loads `note.body` / `moc.body` (raw Markdown text) from `StateDB`.
2. `render_markdown()` parses with `html: False` (raw HTML in the source becomes literal text, not tags).
3. A custom inline rule rewrites internal `[[ZTL - id - label]]` tokens into `<a href="/notes/{id}">`.
4. `bleach.clean()` strips any tag/attribute/protocol not on the allow-list (defense in depth against parser bugs or future HTML-enable regressions).
5. The resulting HTML string is passed into the template context as `rendered_body`.
6. `note_detail.html:2` / `moc_detail.html:2` interpolate it with `{{ rendered_body|safe }}` — the single sanctioned bypass of Jinja2 autoescaping in the whole component.
7. No other template variable in the codebase uses `|safe`; a `grep` across `zettel/templates/` confirms this is exhaustive.

---

### Business Rule: CSRF tokens are embedded, never validated, by the template layer

**Overview**:
Every state-changing form in the UI carries a hidden CSRF field, but the templates have no capability (and no responsibility) to check it — they are a pure carrier for a value the server already computed and signed.

**Detailed description**:
`zettel/web.py:_context()` (lines 127-134) computes `csrf` from the current signed session (`session.get("csrf")` — an opaque random token minted at login via `secrets.token_urlsafe(24)` and stored inside the HMAC-signed `zettel_session` cookie) and merges it into every template's context, authenticated or not (unauthenticated requests get an empty string). Templates then simply echo this value back inside `<input type="hidden" name="csrf" value="{{ csrf }}">` — present in `base.html:15` (logout), `documents.html:3` (upload), `documents.html:4` (harvest), `documents.html:5` (run-all), `pipeline.html:3` (every phase form), and `review.html:4` (batch action). The login page uses a parallel but distinct mechanism (`login_csrf`, signed independently via `_sign("login")`, since no session exists yet at that point) in `login.html:4`.

Because the token's authenticity is verified server-side with `hmac.compare_digest(session.get("csrf", ""), token)` in `_csrf_ok()`, the template's only obligation is fidelity: it must not accidentally omit the field, rename it, or place it outside the `<form>` boundary, any of which would silently make the corresponding POST route return "CSRF inválido" (403) for every legitimate user. This is a structural convention rather than logic the template enacts — but it is a convention every one of the 6 POST-capable templates upholds identically, and it is exercised directly by `tests/test_web.py::test_authentication_and_csrf_protect_mutations` (lines 50-66), which posts with a wrong token and asserts a 403.

**Rule workflow**:
1. `_context(request, **extra)` in `web.py` resolves the active session and extracts its `csrf` value (or `""` if unauthenticated).
2. `_render()` merges this into every template's render context automatically — individual routes never pass `csrf` explicitly.
3. Every POST-capable template embeds `<input type="hidden" name="csrf" value="{{ csrf }}">` inside its `<form>`.
4. On submission, the browser POSTs the field back verbatim as part of the form body.
5. The target route declares `csrf: str = Form("")` and calls `_csrf_ok(request, csrf)`, which re-derives the session's token and compares with `hmac.compare_digest`.
6. A mismatch (wrong/missing/stale token, e.g. after logout or session expiry) yields `HTMLResponse("CSRF inválido", status_code=403)` before any mutation occurs.

---

### Business Rule: Server-computed prerequisite gates are mirrored, not decided, in the UI

**Overview**:
Buttons that trigger pipeline phases (`extract`, `connect`, `garden`, `garden_hubs`, `retry_chunks`) or the full-pipeline shortcut appear visually disabled when their prerequisites are unmet, but this is advisory: the authoritative gate is re-checked in the POST handler regardless of what the client sends.

**Detailed description**:
`pipeline.html:3` computes, per phase, an `unavailable` boolean purely from data the route already fetched via `db.get_stats()` and `_llm_ready(cfg)` — e.g. `(op == 'extract' and not stats.chunks_pending)` or `(op in ['extract','connect','garden','garden_hubs'] and not llm_ready)`. When true, the `<button>` gets both a `disabled` attribute and a `title="Verifique os pré-requisitos abaixo"` tooltip, and a `<small>Pré-requisito indisponível</small>` hint is shown in the card body. `documents.html:5` applies the identical pattern for the "Executar pipeline completo" button, disabling it when `not llm_ready`.

This is presentation-layer UX guidance only. `zettel/web.py:pipeline_action()` (lines 401-436) independently re-derives `stats = db.get_stats()` and re-checks every one of the same conditions before dispatching the job, returning a `409 Conflict` with a Portuguese explanation (e.g. `"Não há chunks pendentes. Execute um harvest válido primeiro."`) if a request arrives despite the disabled state — which is exactly what happens if a user bypasses the disabled attribute via devtools, or if state changes between page load and submit (a TOCTOU window inherent to any server-rendered, non-live-updating page). `tests/test_web.py::test_pipeline_blocks_extract_without_harvest_output` (lines 136-141) exercises this server-side gate directly, independent of what the template rendered.

**Rule workflow**:
1. Route handler (`GET /pipeline` or `GET /documents`) computes prerequisite booleans from live `StateDB` state and `_llm_ready(cfg)`.
2. Template receives these as `stats`/`llm_ready` and computes `unavailable` per-button using Jinja2 expressions embedded directly in the markup (no shared macro; each condition is duplicated inline per phase in the `{% for %}` loop).
3. Unmet prerequisites render `disabled` + explanatory `title` + inline hint text; met prerequisites render a normal actionable button.
4. On submit, the corresponding POST route recomputes the same booleans from fresh `StateDB` reads and independently enforces them, returning `409` if violated — the disabled attribute is never trusted as the source of truth.

---

### Business Rule: Confirmation dialogs gate destructive or broad-blast-radius actions

**Overview**:
Two forms — "Executar pipeline completo" and the review queue's batch approve/reject — require the user to confirm a native browser dialog before the POST fires, because both actions affect many records at once and (for run-all) cannot be interactively supervised.

**Detailed description**:
`documents.html:5` attaches `onsubmit="return confirm('Executar o pipeline completo agora? A revisão automática aprovará apenas drafts dentro do limiar configurado.');"` to the run-all form. The confirmation text itself communicates a real domain rule to the user — that automatic review inside run-all only auto-approves drafts within the configured confidence threshold (`literature_review.auto_approve_min_confidence`), leaving the rest untouched — so the dialog doubles as inline documentation of server behavior implemented in `zettel/web_app.py:_dispatch()` (`run_review(cfg, db, idx, auto_approve=True, interactive=False)`, line ~250-252). `review.html:4` attaches a generic `onclick="return confirm('Confirma a ação nos drafts selecionados?')"` to the batch approve/reject submit button, since that single POST can approve or reject an arbitrary number of checked drafts at once (`chunk_ids: list[str]`).

Both are purely client-side JavaScript `confirm()` calls with no corresponding server-side "was this confirmed" flag — a determined client (curl, a modified form) can submit the POST without ever seeing the dialog. The rule's purpose is accidental-click prevention for the primary web UI, not a security boundary; the real authorization boundary for both routes remains session authentication plus CSRF validation.

**Rule workflow**:
1. User fills a form that would trigger a broad or automatic action (all-phase run, or a batch of checked drafts).
2. Clicking submit triggers the inline `onsubmit`/`onclick` handler, which calls the browser's native `confirm(message)`.
3. If the user cancels, `confirm()` returns `false` and the handler's `return false` prevents form submission entirely — no network request is made.
4. If confirmed, the form submits normally through the standard POST/CSRF/redirect flow described in § 2.2.

---

### Business Rule: Truncation and ellipsis rules for source excerpts

**Overview**:
Long source text is never fully displayed inline in list/table views; it is truncated to a fixed character budget, with the ellipsis character (`…`) appended conditionally — only when the text actually exceeds the budget — to avoid a misleading trailing ellipsis on already-short text.

**Detailed description**:
`review.html:5` truncates each draft's source excerpt to the first 550 characters: `{{ chunk.text[:550] }}{% if chunk.text|length > 550 %}…{% endif %}`. `source_detail.html:2` applies the same pattern at a tighter 180-character budget for the chunk table's "Trecho" column, reflecting that this view is a dense table of many chunks rather than a small set of review cards. Both budgets are hardcoded literals in the template markup (not configuration-driven, not passed from `web.py`), meaning a change to either requires editing the template file directly — there is no shared macro or filter that unifies the two truncation points despite them being conceptually the same operation at two different scales.

The conditional-ellipsis form (`{% if chunk.text|length > 550 %}…{% endif %}`) is deliberate: naively appending `…` unconditionally to a slice would show `…` even on a chunk whose full text is, say, 40 characters — the length check is a small but real correctness rule that a naive implementation would miss.

**Rule workflow**:
1. Template receives the full `chunk.text` string as fetched from `StateDB` (never truncated at the database or route layer).
2. Jinja2 slice syntax `chunk.text[:N]` renders the first N characters.
3. A separate `{% if chunk.text|length > N %}` check decides whether to append the ellipsis glyph.
4. The two independent literals (550 in `review.html`, 180 in `source_detail.html`) are never reconciled or extracted to a shared constant within the component.

---

### Business Rule: Navigation active-state resolution

**Overview**:
The top navigation bar highlights exactly one link — the one matching the current page — using a single Jinja2 loop with an inline tuple list rather than per-route markup duplication.

**Detailed description**:
`base.html:13-14` defines the entire navigation structure as a single Jinja2 `{% for href,label,key in [...] %}` loop over a literal list of `(href, label, key)` tuples (Visão geral/overview, Documentos/documents, Pipeline/pipeline, Revisão/review, Notas·MOCs/notes, Execuções/runs, Configuração·saúde/settings). Every route handler in `web.py` that renders an authenticated page passes a `page=<key>` value into its template context (e.g. `page="documents"`, `page="pipeline"`); some routes intentionally reuse another page's key for shared views — `source_detail` renders with `page="documents"` and both `note_detail`/`moc_detail` render with `page="notes"`, so drilling into a source or note keeps the parent section highlighted rather than showing no active tab or a mismatched one. The comparison itself is a simple string equality, `{% if page == key %}active{% endif %}`, applying the CSS `.active` class defined in `app.css`.

**Rule workflow**:
1. Route handler decides which top-level section the current page conceptually belongs to and passes it as `page=...` in the render call.
2. `base.html`'s single nav loop iterates its fixed 7-tuple list once per request.
3. For each tuple, `page == key` decides whether `class="active"` is applied to that `<a>`.
4. Detail/drill-down pages (source, note, MOC) deliberately borrow their parent list page's key so navigation state stays coherent across the drill-down.

---

### Business Rule: Unauthenticated shell vs. authenticated shell

**Overview**:
`base.html` renders a materially different page shell depending on `authenticated`, gating not just page content (handled per-route) but the navigation bar and logout control themselves.

**Detailed description**:
`base.html:12-16` wraps the entire `<nav>` element and the logout `<form>` in `{% if authenticated %}...{% endif %}`. `authenticated` is computed once, centrally, in `_context()` (`"authenticated": session is not None`), so every template — including `login.html` itself, which also extends `base.html` — receives a consistent value. In practice `login.html` is only ever rendered when `_auth(request)` is false (the login route redirects authenticated users to `/`), so in this codebase the unauthenticated shell is exclusively the login page's chrome: brand header, no nav, no logout button. This is a presentation guard, not an access-control mechanism — actual authorization for every protected route is the `if not _auth(request): return _redirect_login()` check duplicated at the top of each handler in `web.py`, not the template's conditional rendering.

**Rule workflow**:
1. `_context()` resolves `authenticated` from the signed session cookie once per request.
2. `base.html` conditionally renders `<nav>` and the logout form only when `authenticated` is true.
3. Route handlers independently enforce access control before ever reaching the render step — the template conditional and the route guard are two separate, non-redundant layers (template = what's shown; route = what's allowed).

---

### Business Rule: Isolated-note and dead-link connection rendering

**Overview**:
A permanent note's connection list distinguishes between a genuinely isolated note (zero edges) and an edge pointing at a note that no longer exists in the vault (a dangling reference), rendering each state differently.

**Detailed description**:
`note_detail.html:2` first checks `{% if connections %}`; when the list is empty, it shows `<div class="empty">Nota isolada — ainda não há conexões.</div>`, a distinct visual/semantic state from "connections exist." When connections do exist, each edge is decorated server-side by `_decorate_connections()` in `web.py:72-95` with `related_note_exists` (a boolean from `db.get_note(related_id) is not None`) before the template ever sees it. The template then branches per edge: `{% if edge.related_note_exists %}<a href="/notes/{{ edge.related_note_id }}">...</a>{% else %}<span class="muted">{{ edge.related_title }}</span>{% endif %}` — a live note renders as a clickable link using its real title, while a dangling reference renders as plain muted text using the fallback label `"Nota não encontrada"` computed in `_decorate_connections()`. This prevents the UI from ever offering a link that would 404.

**Rule workflow**:
1. Route handler calls `_decorate_connections(db, note_id)`, which iterates `db.get_note_connections(note_id)` and resolves each edge's "other side" (`related_note_id`) relative to the current note.
2. For each edge, `db.get_note(related_id)` determines existence; the result seeds both `related_title` (real title, or `"Nota sem título"`/`"Nota não encontrada"`) and the boolean `related_note_exists`.
3. Template iterates the decorated list; empty list -> single empty-state message; non-empty -> renders `edge.relation_type`, a conditional link vs. muted span, the raw `related_note_id` in a `<code>`, and an optional `description`.

---

## 4. Component Structure

```
zettel/templates/
├── base.html            # Root layout: <head> + CSS links, topbar/nav, alert blocks, {% block content/scripts %}
├── login.html            # Unauthenticated entry point; posts instance secret + login_csrf to /login
├── dashboard.html         # "/" overview: KPI grid, pipeline funnel, quality metrics, cost table, includes job_rows.html
├── documents.html         # "/documents": upload form, inbox picker + harvest options, run-all launcher, sources table
├── pipeline.html          # "/pipeline": one phase-card per operation with server-mirrored prerequisite gating
├── review.html            # "/review": filterable HITL queue, batch approve/reject, per-draft candidate details, pagination
├── notes.html             # "/notes": two compact lists (permanent notes, MOCs)
├── note_detail.html       # "/notes/{id}": sanitized Markdown body (|safe) + decorated connection list
├── moc_detail.html        # "/mocs/{id}": sanitized Markdown body (|safe) only
├── source_detail.html     # "/sources/{id}": source metadata + chunk/draft table with truncated excerpts
├── jobs.html              # "/runs": full job history via job_rows.html include
├── job_rows.html          # Shared partial: job table rows (included by dashboard.html and jobs.html)
├── job_detail.html        # "/jobs/{id}": live progress UI + inline <script> polling loop (the only JS in the component)
└── settings.html          # "/settings": read-only health/availability grid and embedding identity report

zettel/static/
├── app.css                # Primary stylesheet: CSS custom properties (--ink/--muted/--accent/...), layout grid,
│                           # component classes (.kpi, .panel, .review-card, .status, .login-card), one @media(max-width:800px) block
├── mobile.css              # Secondary responsive override: collapses topbar nav into a 3-col grid under 800px
└── markdown.css            # Typography for `.markdown-body` (rendered note/MOC content) + `.connection-*` classes
                            # + a second, narrower @media(max-width:800px) block for `.pipeline-launch`
```

Composition notes:
- `{% extends "base.html" %}` appears in all 13 page templates except `job_rows.html`, which is a bodiless partial included via `{% include "job_rows.html" %}` from `dashboard.html:10` and `jobs.html:2`.
- No template extends or includes any file other than `base.html`/`job_rows.html` — there are no nested layout levels (e.g. no shared "list page" or "detail page" intermediate template), so structurally similar pages (`note_detail.html`/`moc_detail.html`, or `review.html`/`source_detail.html`) duplicate markup patterns (truncation, `<dl class="details">`, table shells) rather than sharing a macro.
- The three CSS files are loaded unconditionally by every page (linked in `base.html:6-8`), regardless of whether a given page uses `.markdown-body` or `.review-card` classes — there is no per-page stylesheet splitting.

---

## 5. Dependency Analysis

```
Internal Dependencies (template composition):
base.html                     <── extended by ── {dashboard, documents, pipeline, review, notes,
                                                    note_detail, moc_detail, source_detail, jobs,
                                                    job_detail, settings, login}.html  (13 templates)
job_rows.html                 <── included by ── dashboard.html, jobs.html

Internal Dependencies (data contract, template <- Python):
base.html            <- web.py:_context()         { request, authenticated, csrf, page, error, message }
login.html           <- web.py:login_page()       { login_csrf, error? }
dashboard.html       <- web.py:overview()          { dashboard (StateDB.get_web_dashboard()), jobs[:5] }
documents.html       <- web.py:documents()         { sources, inbox, llm_ready }
pipeline.html        <- web.py:pipeline()          { stats (StateDB.get_stats()), llm_ready }
review.html          <- web.py:review()            { chunks (enriched w/ summary/candidates JSON), sources,
                                                       selected_source, selected_confidence, review_page, has_next }
notes.html           <- web.py:notes()             { notes, mocs }
note_detail.html     <- web.py:note_detail()       { note, rendered_body (markdown.render_markdown), connections
                                                       (web.py:_decorate_connections) }
moc_detail.html      <- web.py:moc_detail()        { moc, rendered_body (markdown.render_markdown) }
source_detail.html   <- web.py:source_detail()     { source, chunks }
jobs.html            <- web.py:runs()              { jobs }
job_detail.html      <- web.py:job_detail()        { job }; live-refreshed via GET /api/jobs/{id}
settings.html        <- web.py:settings()          { cfg, health, embedding (zettel/index.py identity helpers) }

External Dependencies:
- Jinja2 (>=3.1.0)            - Template engine, wrapped by fastapi.templating.Jinja2Templates
- FastAPI (>=0.115.0) /
  Starlette (transitive)      - Jinja2Templates configuration (default autoescape for .html), TemplateResponse,
                                 StaticFiles mount serving /static/* directly from disk
- python-multipart (>=0.0.9)  - Required by FastAPI/Starlette to parse multipart form data (file upload form)
- bleach                      - Consumed indirectly: templates render its output (`rendered_body`) but never call it
- markdown-it-py /
  linkify-it-py                - Consumed indirectly via zettel/markdown.py; templates never invoke them directly
- Browser Fetch API /
  EventSource-shaped polling  - job_detail.html's inline script calls fetch() against a same-origin JSON API;
                                 no external CDN, no bundler, no framework runtime
```

No template or stylesheet references any external network resource (no CDN script tags, no external font `@import`/`<link>`, no analytics beacons) — confirmed by search across all 14 templates.

---

## 6. Afferent and Efferent Coupling

Coupling is measured over the Jinja2 composition graph (`{% extends %}` / `{% include %}`), the only structural relationship between files in this component (there are no classes/structs — the paradigm here is template inheritance, not OOP).

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| base.html | 13 (every page extends it) | 0 | High |
| job_rows.html | 2 (dashboard.html, jobs.html) | 0 | Medium |
| dashboard.html | 0 | 2 (extends base.html; includes job_rows.html) | Low |
| jobs.html | 0 | 2 (extends base.html; includes job_rows.html) | Low |
| documents.html | 0 | 1 (extends base.html) | Low |
| pipeline.html | 0 | 1 (extends base.html) | Low |
| review.html | 0 | 1 (extends base.html) | Low |
| notes.html | 0 | 1 (extends base.html) | Low |
| note_detail.html | 0 | 1 (extends base.html) | Low |
| moc_detail.html | 0 | 1 (extends base.html) | Low |
| source_detail.html | 0 | 1 (extends base.html) | Low |
| job_detail.html | 0 | 1 (extends base.html) | Low |
| settings.html | 0 | 1 (extends base.html) | Low |
| login.html | 0 | 1 (extends base.html) | Low |
| app.css | 14 (linked by base.html, transitively loaded by every page) | 0 | High |
| mobile.css | 14 (linked by base.html, transitively loaded by every page) | 0 | Medium |
| markdown.css | 14 (linked by base.html, transitively loaded by every page) | 0 | Medium |

`base.html` and the three CSS files are the component's structural bottleneck: any breaking change to `base.html`'s block names (`content`, `scripts`, `title`) or to a widely-used CSS class (`.panel`, `.button`, `.status`) propagates to every page in the UI. This is an accepted, intentional trade-off for a small, hand-authored design system rather than an emergent risk — but it does mean `base.html` and `app.css` are the two files that most warrant review before any structural change.

---

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|--------------|-----------------|
| zettel/web.py route handlers | Internal (server) | Supplies every template's render context (auth state, CSRF token, domain data) | In-process function call -> `Jinja2Templates.TemplateResponse` | Python dict -> Jinja2 context | Handlers pre-empt template rendering entirely on auth/CSRF failure (redirect or plain-text HTTPResponse), so templates never need to render an error state for those cases; domain-level errors (e.g. upload validation) are surfaced via the shared `{% if error %}` alert block in base.html |
| zettel/markdown.py | Internal (server) | Converts note/MOC Markdown bodies into sanitized HTML before the template ever sees them | In-process function call | Plain text in, HTML string out | Sanitization failure is not surfaced to the template layer; `bleach.clean(strip=True)` silently drops disallowed markup rather than raising |
| `GET /api/jobs/{id}` (JSON API in web.py) | Internal (client-side fetch) | Live progress data consumed by job_detail.html's inline script | HTTP/JSON (fetch) | JSON (`{job, events}`) | `if(!r.ok)return;` — a failed poll is silently skipped and retried on the next 1s tick; no user-visible error state, no backoff |
| `fastapi.staticfiles.StaticFiles` mount at `/static` | Internal (server) | Serves the 3 CSS files directly from disk, bypassing Jinja2/templates entirely | HTTP/static file serving | text/css | Standard Starlette 404 for missing assets; no custom handling in this component |
| Browser `confirm()` dialog | Client-side only | UX gate before submitting broad/irreversible-feeling actions (run-all, batch review) | N/A (synchronous JS) | N/A | Cancelling prevents form submission client-side only; server enforces no equivalent flag |

---

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Template Method / Inheritance | `{% extends "base.html" %}` + `{% block title/content/scripts %}` | base.html + all 13 page templates | Single shared page shell (head, nav, alerts) with per-page content injected at fixed extension points |
| Partial / Include | `{% include "job_rows.html" %}` | dashboard.html:10, jobs.html:2 | DRY reuse of the job-history table markup across the dashboard's "recent activity" panel and the full "/runs" page |
| Server-Driven UI (progressive enhancement gate) | `{% if unavailable %}disabled{% endif %}` mirroring server-computed booleans | pipeline.html:3, documents.html:5 | Communicates server-side preconditions in the UI without duplicating business logic — the template only reads a decision, never makes one |
| Composition-root context injection | `_context()` merges `{request, authenticated, csrf}` into every render call | web.py:127-134 (consumed by base.html) | Guarantees every template has a consistent, non-omittable auth/CSRF context without each route repeating boilerplate |
| Whitelist sanitization (allow-list) | `bleach.clean(tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES, protocols=_ALLOWED_PROTOCOLS)` | zettel/markdown.py:44-56 (consumed via `|safe` in note_detail.html / moc_detail.html) | Defense-in-depth against XSS in LLM/manually-authored Markdown content, independent of and in addition to Markdown-parser-level HTML disabling |
| Polling (client-server sync) | `setInterval(refresh, 1000)` against a JSON endpoint | job_detail.html:3-5 | Simple, dependency-free live progress UI without WebSockets/SSE on the client (note: the server does expose an SSE endpoint at `/api/jobs/{id}/events`, but no template consumes it — see § 10) |
| CSS custom properties (design tokens) | `:root{--ink:...;--muted:...;--accent:...}` | app.css:1 | Centralizes the color palette so every component class (`.kpi`, `.status.*`, `.alert`) references tokens rather than hardcoded hex values |

---

## 9. Test Coverage Analysis

| Component Area | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------------|------------|--------------------|----------|----------------|
| Auth/session/CSRF rendering (login.html, base.html logout form) | 0 | 3 (`test_authentication_and_csrf_protect_mutations`, embedded assertions in `_login()` helper, `test_navigation_and_retry_job_flow`) | Good — covers wrong secret, wrong CSRF, missing token | Regex-based token extraction from rendered HTML (`re.search(r'name="csrf" value="([^"]+)"', ...)`) doubles as an implicit structural test that the hidden input exists with the expected `name` attribute |
| Navigation / page rendering (all 13 extends-base templates) | 0 | 1 (`test_navigation_and_retry_job_flow`, lines 101-115) | Good breadth, shallow depth — asserts each of 7 routes returns 200 and contains one expected substring; does not assert `.active` class placement, disabled-button states, or CSS asset loading | Substring assertions only; no HTML structure/DOM assertions, no snapshot testing |
| Markdown sanitization / `\|safe` usage (note_detail.html, moc_detail.html) | 0 | 1 (`test_note_and_moc_details_render_sanitized_markdown`, lines 277-345) | Very good — exercises headings, lists, blockquotes, safe links, bare-URL linkify, internal wikilinks, dangling-reference rendering, `<script>` and `javascript:` injection attempts, and 3 connection edges (`connection-row` count assertion) | Strong negative-case coverage (XSS attempts); could be extended with an `<img>`/`<iframe>` injection attempt or a malformed wikilink to further stress the allow-list |
| Document upload / harvest form (documents.html) | 0 | 4 (`test_upload_rejects_traversal_and_collisions`, `test_nested_inbox_file_can_be_selected_for_harvest`, `test_harvest_rejects_absolute_and_parent_paths`, `test_documents_hide_completed_file_but_show_changed_copy`) | Good — covers path traversal, absolute paths, name collisions, nested-folder selection, completed-vs-changed file visibility | Tests assert on rendered `value="..."` attributes in `<option>` elements (`'value="pending.md"' in page.text`), which is a reasonable proxy for "this file is listed" without full DOM parsing |
| Pipeline phase gating (pipeline.html disabled-button mirroring) | 0 | 1 (`test_pipeline_blocks_extract_without_harvest_output`) | Partial — only tests the server-side 409 response text, never asserts the template actually renders the button as `disabled` for the same precondition | Gap: no test parses `pipeline.html` output to confirm the `disabled`/`title` attributes appear when `stats.chunks_pending == 0` |
| Job detail live progress (job_detail.html + inline script) | 0 | 1 (`test_navigation_and_retry_job_flow`, lines 129-133) | Weak — only asserts static server-rendered markers (`id="job-state"`, "succeeded", `id="job-result"`, "Concluído") after the job has already completed; the inline JS polling loop itself (fetch/refresh/DOM mutation logic) is never executed or asserted, since `TestClient` performs no JS evaluation | Gap: the entire client-side script in job_detail.html (the only JS in the component) has zero test coverage — a regression there (e.g. a typo in a `document.querySelector` selector) would not be caught by the Python test suite |
| Review queue (review.html: filters, pagination, batch actions) | 0 | 0 direct | None found | No test in `tests/test_web.py` navigates `/review` with query params, submits `/review/action`, or asserts the pagination `Anterior`/`Próxima` link visibility rules; `/review` is only visited generically in `test_navigation_and_retry_job_flow`'s smoke loop |
| Notes/MOCs listing (notes.html) | 0 | 0 direct | None found | Only visited generically in the navigation smoke test; empty-state (`"Ainda não há notas permanentes."`) and populated-state rendering are both untested |
| Settings page (settings.html) | 0 | 1 (smoke test only) | Shallow | Health-grid dot coloring (`ok`/`bad` class per key) and embedding drift warning (`<span class="status failed">divergente</span>`) are never exercised with a drifted-embedding fixture |
| Static assets (app.css, mobile.css, markdown.css) | 0 | 0 | None | No test asserts `/static/app.css` (or the other two files) is served, returns `text/css`, or is even reachable — `tests/test_web.py` and `tests/test_web_state.py` contain no reference to `/static` at all |
| Dashboard (dashboard.html KPIs, funnel, hubs, cost table) | 0 | 1 (smoke, `"Bom trabalho"` substring only) | Shallow | `tests/test_web_state.py::test_progress_events_and_dashboard_are_persisted` tests `StateDB.get_web_dashboard()` data shape directly (not through this component), but no test asserts dashboard.html correctly renders populated KPI/funnel/hub/cost sections — only the empty-state smoke path is covered end-to-end through HTTP |

Test files located: `tests/test_web.py` (375 lines, 12 test functions, the primary integration-test suite for this component, using `fastapi.testclient.TestClient` against `zettel.web.create_app`) and `tests/test_web_state.py` (135 lines, unit-level tests for `WebWorker`/`_idx_kwargs`/`safe_error` in `zettel/web_app.py` — these validate the data the templates will eventually render but do not exercise the templates themselves). No template-specific unit-testing tool (e.g. a Jinja2 template renderer invoked in isolation, or a snapshot test of rendered HTML) is used anywhere in the repository; all template coverage is incidental to HTTP-level integration tests in `test_web.py`.

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|------------------|-------|--------|
| Low | app.css / mobile.css / markdown.css | `.back` (used in `moc_detail.html`, `note_detail.html`, `source_detail.html`) and `.pagination` (used in `review.html`) classes have no corresponding CSS rule in any of the 3 stylesheets | These elements fall back to unstyled browser defaults (plain anchor color/underline for `.back`, block-stacked children with no flex/gap for `.pagination`), visually inconsistent with the rest of the hand-built design system |
| Low | job_detail.html | The job's `job_id` (a `uuid4().hex` string) is interpolated directly into an inline `<script>` as `const id="{{ job.job_id }}";` with only Jinja2's default HTML-context autoescaping, not a JS-string-context-aware escape | Currently safe only because `job_id` is always server-generated hex (never user input); if a future change ever sourced this value from user-controlled data, this would be a script-injection vector — no template-level safeguard exists to catch that regression |
| Low | pipeline.html | The `unavailable` boolean expression for each phase card is a single, fairly long inline Jinja2 conditional repeated per-iteration inside a `{% for %}` loop rather than being computed once in `web.py` and passed as a precomputed dict | Harder to keep the "prerequisite" business rule synchronized between this template's inline conditional and the equivalent (separately written) check in `web.py:pipeline_action()` — the two currently agree but there is no shared source of truth enforcing that they always will |
| Low | review.html / source_detail.html | Truncation length is a bare literal (`550`, `180`) duplicated independently in two templates with no shared constant or Jinja filter | A future change to the design's excerpt-length convention requires editing two files and manually keeping them conceptually distinct (review cards vs. dense table) but consistent within each |
| Low | Test coverage (§ 9) | `/static/*` assets, the review queue's filter/pagination/batch-action flow, the job_detail.html inline script's actual polling behavior, and populated (non-empty-state) dashboard/notes rendering all have zero or near-zero direct test coverage | Regressions in these specific areas (a broken CSS mount, a pagination off-by-one, a JS selector typo, or a dashboard template error only triggered when data is present) would not be caught by the current `pytest` suite |
| Low | job_detail.html vs. `/api/jobs/{id}/events` | `web.py` implements a working Server-Sent Events endpoint (`GET /api/jobs/{id}/events`, lines 570-586) but `job_detail.html` uses 1-second `setInterval` polling instead of consuming it | Dead/unused server capability from this component's perspective — not a defect, but a missed opportunity noted here because it means the SSE endpoint's behavior is currently exercised by no template and, per § 9, no test either |
| Informational | Overall | The presentation layer intentionally contains no business logic, no client-side state management, and no build step — this is a design choice appropriate to the project's "server-rendered, no Node/bundler" convention (see `web.py` module docstring and `CLAUDE.md`), not a defect | Keeps the component's surface area small and easy to audit, at the cost of the duplicated-literal and duplicated-conditional issues noted above |

---

## 11. Ambiguity Notes

- The exact bleach/markdown-it/linkify-it-py *pinned* versions could not be determined from `requirements.txt` (only lower-bound or unpinned entries: `bleach`, `markdown-it-py`, `linkify-it-py` with no version specifier) — the installed versions in `.venv` were not inspected, per the instruction to exclude `.venv` from analysis.
- Jinja2Templates' autoescape configuration is not explicitly set in `zettel/web.py:28` (`Jinja2Templates(directory=...)`); this report relies on Starlette's documented default (`select_autoescape(["html", "xml"])`, which autoescapes given the directory contains only `.html` files) rather than inspecting the installed Starlette source, since `.venv` is out of scope.
- No dedicated favicon asset exists in `zettel/static/`; `GET /favicon.ico` is handled by a route in `web.py` returning `204 No Content` rather than serving a static file — noted for completeness but this route lives in `web.py`, not in the `templates/static` component boundary itself.
