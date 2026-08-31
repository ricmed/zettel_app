# Component Deep Analysis Report — `markdown`

**Module analyzed**: `zettel/markdown.py` (73 lines)
**Analysis date**: 2026-08-30
**Scope note**: The component under analysis is the single module `zettel/markdown.py`. Because its entire reason to exist is what happens to the string it is handed, this report also traces (read-only) into its one consumer (`zettel/web.py`) and the modules that *produce* the Markdown it renders (`zettel/vault.py`, `zettel/assets.py`) wherever needed to substantiate a finding — no files were modified in the course of this analysis.

---

## 1. Executive Summary

`zettel/markdown.py` is a small, stateless utility module with a single public entry point, `render_markdown(text)`, that turns vault-authored Markdown (literature notes, permanent/ZTL notes, MOCs) into HTML safe to embed in the web UI. It sits at the very end of the read path: `StateDB` → `web.py` route → `render_markdown()` → Jinja2 template rendered with `|safe`. Nothing downstream re-sanitizes its output, so this module is the *only* XSS boundary between whatever text ended up in `notes.body` / `mocs.body` (LLM-generated, harvested, or hand-edited) and the browser.

The module combines two independent defenses in series: (1) a `markdown-it-py` parser configured with `html: False` (raw HTML in the source is never parsed as tags) plus a custom inline rule that turns the project's proprietary `[[ZTL - <id> - <slug>]]` wikilink syntax into internal `<a href="/notes/{id}">` links; and (2) a `bleach.clean()` allow-list pass over the resulting HTML, restricting tags, attributes, and URL protocols regardless of what the parser produced. The module's own docstring is explicit that the second pass exists specifically as a hedge against the first ("protects the rendered output if the parser or its configuration changes later").

**Key findings**:
- The component correctly neutralizes the two attack vectors it is explicitly tested against — raw `<script>` HTML and `javascript:` URLs (`tests/test_web.py::test_note_and_moc_details_render_sanitized_markdown`) — via the html-disabled parser and the bleach allow-list, respectively.
- Despite the surrounding pipeline actively producing Markdown image content (`![Imagem](90_Assets/...)` from `assets.py`, `![[90_Assets/...]]` embeds from `vault.py`'s `build_permanent_note_body`/`build_literature_index_note`), **no image is ever rendered as a visible `<img>` in the web UI**: standard Markdown images are parsed into `<img>` tags that bleach then strips outright (`img` is absent from `_ALLOWED_TAGS`), and Obsidian's `![[...]]` embed syntax is not CommonMark and is not specially handled, so it survives as literal bracket text — a behavior the test suite explicitly locks in (`assert "![[figura-local.png]]" in note.text`). There is also no static route serving `90_Assets/` files, so even a surviving `<img src="90_Assets/...">` would 404. This is a real functional gap versus the component's apparent intended scope, not merely an untested edge case.
- The custom `_render_ztl_wikilink` inline rule does not update `state.pos` on its `silent` (validation-only) code path, unlike every stock `markdown-it-py` inline rule (e.g. `backtick`), which is a deviation from the library's rule contract with a plausible (but unverified, since execution was out of scope) mis-parse/hang risk when a ZTL wikilink is nested inside another link's label or an image's alt text.
- The project has at least three independently-maintained regular expressions for the same `[[ZTL - <id> - ...]]` construct (`markdown.py`, `article.py`, `sync.py`), with different strictness (`markdown.py`'s `note_id` group accepts any `[A-Za-z0-9_-]+`, the other two require an exact 26-character Crockford-base32 ULID) — a duplication/consistency risk rather than a shared parsing primitive.
- Test coverage for this module is thin: there is no dedicated `tests/test_markdown.py`; all coverage is incidental, funneled through one integration test in `tests/test_web.py` that exercises the note/MOC detail routes.

---

## 2. Data Flow Analysis

`markdown.py` has one entry point and no internal state beyond module-level singletons built once at import time. Two representative flows:

**Flow A — Permanent note detail page (`GET /notes/{note_id}`)**
```
1. web.py:note_detail() authenticates the session and loads the note row from StateDB (db.get_note)
2. web.py calls zettel.markdown.render_markdown(note["body"])
3. render_markdown() feeds text into the shared MarkdownIt instance (_MARKDOWN.render)
   3a. CommonMark block/inline parsing runs (headings, lists, blockquotes, tables, code, emphasis, links)
   3b. Custom inline rule "ztl_wikilink" (registered before the stock "link" rule) intercepts
       [[ZTL - <id> - <slug>]] / [[ZTL - <id>]] (optionally |alias) and emits <a href="/notes/{id}">
   3c. "linkify" auto-links bare http(s) URLs found in text
4. The resulting raw HTML string is passed to bleach.clean() with the module's tag/attribute/protocol allow-lists (strip=True)
5. render_markdown() returns the sanitized HTML string to web.py
6. web.py passes it into note_detail.html as rendered_body, rendered with the Jinja2 `|safe` filter
   (i.e. no further escaping happens downstream — markdown.py's output is trusted as final)
```

**Flow B — MOC detail page (`GET /mocs/{moc_id}`)**
```
1. web.py:moc_detail() loads the MOC row (db.get_moc)
2. Identical call: render_markdown(moc["body"])
3-5. Same parse -> sanitize pipeline as Flow A
6. Injected into moc_detail.html as rendered_body via |safe
```

Both flows are read-only and side-effect-free from `markdown.py`'s perspective — it never touches `StateDB`, the filesystem, or the vault; it is a pure `str -> str` transform (module-level parser/sanitizer configuration aside). `web.py`'s `source_detail` route (`GET /sources/{source_id}`) does **not** call `render_markdown` at all — source detail pages render chunk/source fields as plain template text, not as rendered Markdown, so that page is outside this component's data flow despite superficially being a similar "detail view."

---

## 3. Business Rules & Logic

## Overview of the business rules:

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Configuration | Parser base is CommonMark with raw HTML parsing disabled | markdown.py:12 |
| Configuration | GFM `table` and `linkify` (auto-link bare URLs) extensions enabled on top of CommonMark | markdown.py:12-14 |
| Business Logic | `[[ZTL - <id> - <slug>]]` / `[[ZTL - <id>]]` (optional `\|alias`) becomes an internal link to `/notes/{id}` | markdown.py:15-40 |
| Validation | Image-embed prefix `!` immediately before `[[` disables the ZTL wikilink rule | markdown.py:25-26 |
| Business Logic | Link text defaults to the raw label when no `\|alias` is supplied (no live title lookup) | markdown.py:37 |
| Security | Output HTML is passed through a bleach allow-list of tags, second and final line of defense | markdown.py:59-73 |
| Security | Allowed tags = bleach defaults (`a`, `abbr`, `acronym`, `b`, `blockquote`, `code`, `em`, `i`, `li`, `ol`, `strong`, `ul`) plus headings, `p`, `pre`, `table` family, `hr`, `br` | markdown.py:44-49 |
| Security | Allowed attributes restricted per tag (`a`: href/title, `code`: class, `th`/`td`: align); everything else stripped, replacing bleach's own defaults rather than extending them | markdown.py:50-55 |
| Security | Allowed URL protocols restricted to `http`, `https`, `mailto` | markdown.py:56 |
| Validation | `None`/falsy input is coerced to an empty string before parsing | markdown.py:67 |
| Implicit / Gap | Standard Markdown images (`![alt](path)`) are parsed but their `<img>` tag is stripped by the allow-list — no image is ever visible | markdown.py:44-49 (absence of `img`) |
| Implicit / Gap | Obsidian embed syntax `![[path]]` is not recognized by the CommonMark parser at all and survives as literal bracket text | markdown.py (no rule intercepts `![[`) |

## Detailed breakdown of the business rules:

---

### Business Rule: CommonMark base with raw HTML disabled

**Overview**:
The `MarkdownIt` instance is constructed as `MarkdownIt("commonmark", {"html": False, "linkify": True})`. The `"commonmark"` preset selects the strict, spec-compliant subset of Markdown-it's ruleset (as opposed to the more permissive `"default"`/`"gfm-like"` presets that ship more inline HTML tolerance and extra autolinking behavior), and `"html": False` is the single most important security-relevant parser option in the file: it tells the parser to treat any literal `<...>` sequence in the source text as plain text rather than as an HTML tag to pass through verbatim.

This matters because CommonMark, by design, allows raw HTML blocks and raw inline HTML to be embedded in Markdown and reproduced unchanged in the output — that is the intended, spec-correct behavior of a Markdown renderer, and it is precisely how a `<script>alert('xss')</script>` payload sitting in a note body would otherwise reach the browser unescaped. By disabling `html_block` and `html_inline` parsing at the source, `markdown-it-py` instead renders any such sequence as literal, HTML-escaped text (e.g. `&lt;script&gt;`), so the payload is inert well before bleach ever sees it. The project's own test (`tests/test_web.py:330`, `assert "<script>" not in note.text`) locks this behavior in directly against a raw `<script>` tag placed in a note body sourced from an LLM or a manual edit.

The consequence for the rest of the pipeline is that any content author — the LLM (Prompt 1/2 outputs), a human editing a vault file by hand via `zettel new-note`/`sync-manual`, or a corrupted/malicious `.md` file dropped into the vault and picked up by `sync.py` — cannot inject executable markup through the note/MOC `body` field, no matter how that body was produced. This is a foundational, load-bearing rule for the module's entire security posture, and it is intentionally documented as the *first* of two layers, with bleach as the second (see the "Second-pass sanitization" rule below), rather than being relied upon alone.

**Rule workflow**:
```
Markdown source contains "<script>...</script>" or any other literal tag-like text
  -> _MARKDOWN.render() parses it with html_block/html_inline rules disabled
  -> the sequence is treated as ordinary text, not as a tag
  -> HTML-escaping during text-token rendering turns "<" into "&lt;" etc.
  -> bleach.clean() sees only escaped entities, nothing to strip
  -> final HTML contains no executable tag
```

---

### Business Rule: GFM extensions enabled (`table`, `linkify`)

**Overview**:
On top of the CommonMark base, the module explicitly re-enables two rules that the `"commonmark"` preset disables by default: `table` (GitHub-Flavored-Markdown pipe tables) and `linkify` (automatic hyperlinking of bare URLs that are not wrapped in `[text](url)` syntax). This is a deliberate widening of the base preset's feature surface to match what the pipeline's own generated content actually uses.

Enabling `table` matters because MOC and permanent-note bodies produced by `gardener.py`/`connector.py` (and any hand-authored vault content) may use pipe-table syntax for structured comparisons; without this rule such tables would render as a literal, un-parsed paragraph of pipe characters and dashes rather than an HTML `<table>`. The project's allow-list explicitly adds the full table tag family (`table`, `thead`, `tbody`, `tr`, `th`, `td`) to survive the bleach pass specifically to support this — the two settings are a matched pair, and removing one without the other would either produce dead HTML tags or an unstyled table fallback.

`linkify` matters for a different reason: it lets bare URLs typed as plain text (e.g., a citation URL copied into a note without Markdown link brackets) become clickable without the author needing to know Markdown link syntax. The constructor already passes `{"linkify": True}` as an option, and the `.enable(["table", "linkify"])` call re-asserts `linkify` again — this is redundant (the option and the explicit enable both turn on the same rule) but harmless; it is a minor internal inconsistency rather than a functional bug. The test suite confirms the behavior end-to-end: `"URL direta: https://docs.example.com/guia"` in a note body produces `'href="https://docs.example.com/guia"'` in the rendered output (`tests/test_web.py:286,327`).

**Rule workflow**:
```
Note body contains a Markdown pipe table
  -> "table" rule (re-enabled) parses rows/columns into table/thead/tbody/tr/th/td tokens
  -> bleach allow-list (which explicitly includes this tag family) preserves them
  -> rendered as a real HTML <table>

Note body contains "https://example.com/x" with no surrounding [ ]( ) syntax
  -> "linkify" rule (re-enabled + option flag) detects the bare URL during inline parsing
  -> emits a link_open/text/link_close token triple, exactly as if [text](url) had been written
  -> bleach allows the resulting <a href="..."> (href protocol is http/https, so it survives)
```

---

### Business Rule: ZTL wikilink → internal note link translation

**Overview**:
This is the module's only piece of genuinely custom parsing logic. A regular expression, `_ZTL_WIKILINK`, recognizes the project's proprietary wikilink syntax for permanent notes — `[[ZTL - <note_id> - <slug>]]`, the shorter `[[ZTL - <note_id>]]`, and either form with a `|<alias>` suffix — and a custom `markdown-it-py` inline rule, `_render_ztl_wikilink`, is registered with `ruler.before("link", "ztl_wikilink", ...)` so it gets first refusal on any `[[` sequence, ahead of CommonMark's own (irrelevant, since `[[...]]` is not valid CommonMark link syntax anyway) link rule.

This syntax is not incidental: it is the exact string format that `zettel/vault.py`'s `permanent_wikilink()` and `build_permanent_note_body()` (line ~757: `f"[[ZTL - {note_id} - {_slug(title)}]]"`) generate when the connector pipeline (`connector.py`) writes the "## Conexões" section of a new permanent note, linking it to other permanent notes it relates to. In other words, this module's custom rule exists specifically to make the connector's own output clickable in the web UI — it is the rendering half of a feature whose writing half lives in a different component (`vault.py`). Without this rule, every cross-reference between permanent notes displayed in the web UI would show as inert bracket-and-hyphen text instead of a navigable link.

The rule resolves to a URL of the fixed shape `/notes/{note_id}` — it performs no existence check against `StateDB` and no live title lookup; if no `|alias` is present, the visible link text is the *raw label* (e.g., literally "ZTL - 01ABCDEF... - minha-nota"), not the target note's current title. This is confirmed by the test fixture: the body under test uses `[[ZTL - note-target - nota-relacionada]]` with no alias, and the resulting page shows the literal label as link text, while the *actual* title "Nota relacionada" that appears on the page comes from an entirely separate part of the template (the connections panel, populated from `_decorate_connections()`, not from this rule). A link can therefore point at a note ID that no longer exists (renamed or deleted) and will still render as a clickable, garden-variety `<a>` tag; visiting it is what surfaces the 404, not the rendering step.

**Rule workflow**:
```
Inline parser reaches position of "[["
  -> _render_ztl_wikilink checks: does src[pos:pos+2] == "[[" ?
  -> guard: if the character before "[[" is "!", bail out (do not intercept image-embed syntax)
  -> regex match against "ZTL\s*-\s*(note_id)(\s*-\s*slug)?(\|alias)?\]\]"
  -> no match -> return False, fall through to stock CommonMark rules (renders as literal text)
  -> match found, silent=True (validation-only lookahead) -> return True without emitting tokens
       or advancing state.pos (see Technical Debt: deviates from library convention)
  -> match found, silent=False -> push link_open<a href="/notes/{note_id}">,
       text = alias if present else the raw "ZTL - id - slug" label, push link_close
  -> state.pos advances past the full matched "[[...]]" span
  -> downstream: bleach allows the <a> tag (href protocol is a relative path, not
       one of http/https/mailto explicitly, but bleach's protocol check only applies
       to absolute-scheme URLs — a scheme-relative path like "/notes/x" passes through)
```

---

### Business Rule: Sanitization allow-list is bleach's own defaults *plus* a fixed extra set — not a superset merge for attributes

**Overview**:
`render_markdown()` always runs the parser's raw HTML output through `bleach.clean(rendered, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES, protocols=_ALLOWED_PROTOCOLS, strip=True)`. `_ALLOWED_TAGS` is explicitly built as `set(bleach.sanitizer.ALLOWED_TAGS) | {...}` — a genuine union of bleach's built-in default tag list (`a`, `abbr`, `acronym`, `b`, `blockquote`, `code`, `em`, `i`, `li`, `ol`, `strong`, `ul`) with the project's additions (headings `h1`-`h6`, `p`, `pre`, `table`/`thead`/`tbody`/`tr`/`th`/`td`, `hr`, `br`). This produces the full tag vocabulary CommonMark rendering can realistically emit for the content types this pipeline generates: headings, paragraphs, lists, blockquotes, code blocks/spans, tables, links, emphasis, and thematic breaks.

The attributes list, however, is *not* built the same way: `_ALLOWED_ATTRIBUTES = {"a": [...], "code": [...], "th": [...], "td": [...]}` is a fresh dict, not unioned with `bleach.sanitizer.ALLOWED_ATTRIBUTES` (which separately defines `title` for `abbr` and `acronym`). Because `bleach.clean()`'s `attributes` parameter, when supplied, replaces the default rather than merging with it, this means `abbr` and `acronym` — both present in the *tags* allow-list via the bleach-default union — are allowed as tags but permitted **zero** attributes, so a `title` on either would be silently stripped. In practice this is inert: nothing in this module's `MarkdownIt` configuration (no abbreviation plugin is enabled) ever produces an `<abbr>` or `<acronym>` tag from Markdown source, so the gap is currently unreachable, but it is a latent inconsistency that would surface immediately if a future change enabled an abbreviation-producing extension without also touching `_ALLOWED_ATTRIBUTES`.

For every attribute genuinely reachable from this pipeline's actual output — `href`/`title` on links, `class` on code spans (used by syntax-highlighting conventions, e.g. `language-python`), and `align` on table cells — the allow-list is precise and matches exactly what CommonMark/GFM table parsing can produce. Every other attribute markdown-it-py could theoretically be coaxed into emitting (there are none, given `html: False`, but any future config drift) or that a raw-HTML-smuggling attempt might carry (`onerror`, `onclick`, `style`, `src` on non-image tags, arbitrary `data-*`) is stripped unconditionally, because `bleach.clean()`'s attribute filtering is a strict allow-list per tag with no fallback to "allow if not otherwise specified."

**Rule workflow**:
```
bleach.clean(html, tags=ALLOWED, attributes=ALLOWED_ATTRS, protocols=ALLOWED_PROTOCOLS, strip=True)
  for each tag in the parsed HTML tree:
    if tag not in ALLOWED_TAGS: remove the tag (strip=True keeps inner text unless the
                                  tag is a void element with no text content, e.g. <img>)
    else:
      for each attribute on the tag:
        if tag not in ALLOWED_ATTRIBUTES or attribute not in ALLOWED_ATTRIBUTES[tag]:
          drop the attribute
        elif attribute produces a URL (href) and its scheme is not in ALLOWED_PROTOCOLS:
          drop the attribute (see next rule)
```

---

### Business Rule: URL protocol allow-list blocks script-executing schemes

**Overview**:
`_ALLOWED_PROTOCOLS = {"http", "https", "mailto"}` is passed to `bleach.clean()`'s `protocols` argument, which governs every attribute bleach recognizes as carrying a URL (principally `href` on `<a>`, given this module's tag/attribute allow-lists). Any `href` whose scheme is not in this set — most importantly `javascript:`, but also `data:`, `vbscript:`, or a bare unrecognized scheme — is stripped from the tag entirely (the `<a>` tag itself survives if otherwise valid, but loses its `href`, becoming an inert wrapper around its text).

This closes the second classic web-note-rendering XSS vector after raw `<script>` injection: a Markdown link whose destination is a `javascript:` URI, e.g. `[perigoso](javascript:alert('xss'))`. Because `html: False` only prevents *raw HTML* tags from being parsed — it has no bearing on `[text](url)` link syntax, which is native CommonMark and is parsed and rendered into a real `<a href="...">` regardless — this protocol allow-list is the layer that actually neutralizes malicious link destinations. Removing it (or passing `protocols=None`, which falls back to bleach's own default protocol set, which does *not* include `mailto`) would change both the security posture and the mailto-link feature simultaneously, since the two are governed by the same list.

The project's test directly exercises this: `[perigoso](javascript:alert('xss'))"` in a note body is asserted to produce no `'href="javascript:'` in the response (`tests/test_web.py:290,331`). The rule is intentionally scoped to exactly the three schemes the pipeline's legitimate content needs — external citations and cross-references (`http`/`https`) and, via `mailto`, contact-style links an author might hand-write — with no allowance for anything broader (no `ftp`, no `tel`, no relative-scheme flexibility beyond what bleach treats as scheme-less/relative paths, which the internal ZTL links rely on).

**Rule workflow**:
```
<a href="javascript:alert('xss')">perigoso</a>  (post-parse, pre-sanitize HTML)
  -> bleach.clean() inspects the href attribute
  -> extracts the scheme "javascript"
  -> "javascript" not in {"http", "https", "mailto"}
  -> href attribute is dropped
  -> resulting tag: <a>perigoso</a> (link text preserved, destination neutralized)
```

---

### Business Rule: Images are effectively unrenderable through this pipeline (implicit / gap)

**Overview**:
This is a documented *behavior*, not a documented *intent* — it is not stated anywhere in the module's docstrings, but it is deterministic, reproducible from the code, and explicitly pinned by a passing test, so it is reported here as an implicit business rule with high confidence in the mechanism and lower confidence in whether it is the intended end-state. Two independent gaps compound into "no image the pipeline generates is ever visible on a note/MOC detail page": standard Markdown images are parsed but then stripped by the sanitizer, and Obsidian's non-standard embed syntax is never parsed as an image at all.

First, `_ALLOWED_TAGS` does not include `img`. `assets.py` (harvest-time image extraction) rewrites both PDF- and Markdown-sourced images into standard CommonMark image syntax, `![Imagem](90_Assets/img-<hash>.png)` (see `extract_markdown_images`/`extract_docling_images`, which both call `f"![Imagem]({relpath})"`). `_MARKDOWN.render()` parses this correctly into `<img src="90_Assets/img-....png" alt="Imagem">`. But because `img` is absent from the tag allow-list, `bleach.clean(..., strip=True)` removes the tag outright; since `<img>` is a void element with no inner text, nothing is left in its place — the image reference simply vanishes from the rendered page with no residual text, no broken-image placeholder, and no visual indication that content was dropped.

Second, and independently of the first: `vault.py`'s `build_permanent_note_body()` (the "## Figuras" section for images "deemed essential to the concept") and `build_literature_index_note()`/related builders (the "## Imagens Relacionadas" section) both emit Obsidian's *embed* syntax, `![[90_Assets/img-....png]]`, not standard CommonMark image syntax. `![[...]]` is not CommonMark and has no special handling anywhere in `markdown.py` — no custom inline rule intercepts it, unlike the `[[ZTL - ...]]` case. The `!` immediately preceding `[[` is CommonMark's own image-marker character, so the parser attempts to treat what follows as an image whose "label" is `[ZTL...` — but since there is no matching `](url)` destination, CommonMark's link/image grammar fails to complete the construct, and the entire sequence falls through to plain text. This is exactly what the project's own regression test asserts: a note body containing `"![[figura-local.png]]"` renders with `assert "![[figura-local.png]]" in note.text` — i.e., the test encodes "this stays literal text" as the expected, correct behavior, not as an accepted defect.

Third, even in the hypothetical where both of the above were fixed (an `img` tag allow-listed and an Obsidian-embed inline rule added, mirroring the ZTL wikilink rule), the image would still fail to load in a browser: `zettel/web.py` mounts only `/static` via `StaticFiles` (its own CSS/JS assets, `zettel/static/`), and no route serves files from the vault's `90_Assets/` directory. An `<img src="90_Assets/img-....png">` emitted into a page served from the web app has no corresponding HTTP resource to resolve against. The `markdown-body` CSS (`zettel/static/markdown.css`) — which styles every other element this module can produce, down to table cell alignment — has no rule for `img` at all, consistent with images never having been a rendering target reached by this stylesheet.

**Rule workflow**:
```
Case 1 — standard Markdown image, e.g. from assets.py extraction:
  "![Imagem](90_Assets/img-abcd1234.png)"
    -> parsed into <img src="90_Assets/img-abcd1234.png" alt="Imagem">
    -> bleach.clean(): "img" not in _ALLOWED_TAGS -> tag removed entirely
    -> rendered output: nothing (no text, no placeholder)

Case 2 — Obsidian embed syntax, e.g. from vault.py's "## Figuras"/"## Imagens Relacionadas":
  "![[90_Assets/img-abcd1234.png]]"
    -> CommonMark image grammar looks for "![label](destination)" or "![label][ref]"
    -> "[[90_Assets/img-abcd1234.png]]" does not close as a valid image label + destination
    -> the whole "![[...]]" sequence falls through to plain text
    -> bleach.clean(): plain text is untouched
    -> rendered output: the literal string "![[90_Assets/img-abcd1234.png]]" as visible text

Case 3 (hypothetical, would still fail today) — even a surviving <img src="90_Assets/...">:
    -> browser requests GET /90_Assets/img-abcd1234.png (or whatever relative resolution applies)
    -> no FastAPI route/StaticFiles mount serves 90_Assets/ -> 404
```

---

### Business Rule: `None`/falsy body input is tolerated, not rejected

**Overview**:
`render_markdown(text: str | None)` opens with `rendered = _MARKDOWN.render(text or "")`. Both `web.py` call sites pass a dict-lookup result (`note.get("body")`, `moc.get("body")`) directly, without a prior null check, so this single `or ""` is the only guard standing between a note/MOC row with a `NULL`/missing `body` column and an exception inside the parser. It is a narrow but load-bearing defensive rule: it makes `render_markdown` total over its declared input type (`str | None`) rather than partial, at the cost of silently rendering "nothing" for missing content rather than surfacing an error to the caller or the page.

The practical scenario this guards against is a note or MOC record that exists in `StateDB` (so the detail route's existence check passes and the page proceeds to render) but whose `body` was never populated — for example, a manually-scaffolded note (`new_note.py`) that a user has not yet filled in, or a row reached mid-migration/mid-write. Rather than a 500 error on the detail page, the user sees an empty content panel, which is a reasonable degrade for a read-only viewer.

The rule only special-cases falsy values coerced by Python's `or` (`None`, `""`, and any other falsy value, though `body` is typed as a string field throughout the schema so `None`/`""` are the only realistic cases) — it does not attempt to distinguish "genuinely empty note" from "missing body due to a bug upstream," and it does not log or surface that distinction anywhere in this module.

**Rule workflow**:
```
render_markdown(None)   -> text or "" evaluates to ""  -> _MARKDOWN.render("") -> "" -> bleach.clean("") -> ""
render_markdown("")     -> same path as above
render_markdown("# Hi") -> "# Hi" is truthy -> parsed/sanitized normally
```

---

## 4. Component Structure

```
zettel/
└── markdown.py                       # Entire component: single file, no package/subfolder
    ├── _MARKDOWN                     # Module-level MarkdownIt singleton (CommonMark + table + linkify)
    ├── _ZTL_WIKILINK                 # Compiled regex for [[ZTL - <id>[ - <slug>]][|<alias>]]
    ├── _render_ztl_wikilink()        # Custom inline rule: ZTL wikilink -> internal <a> link
    │                                 #   (registered via _MARKDOWN.inline.ruler.before("link", ...))
    ├── _ALLOWED_TAGS                 # bleach default tags ∪ project extras (headings/p/pre/table/hr/br)
    ├── _ALLOWED_ATTRIBUTES           # Per-tag attribute allow-list (a, code, th, td) — replaces bleach defaults
    ├── _ALLOWED_PROTOCOLS            # {"http", "https", "mailto"}
    └── render_markdown(text)         # Public API: parse -> sanitize -> return HTML string
```

There is no accompanying test file (`tests/test_markdown.py` does not exist), no `__init__.py` re-export beyond the package's normal module visibility, and no configuration file — every tunable value in this component is a hardcoded module-level constant evaluated once at import time.

---

## 5. Dependency Analysis

```
Internal Dependencies (zettel package):
  web.py --calls--> markdown.render_markdown()
  (no other zettel module imports zettel.markdown; markdown.py imports nothing from zettel)

  Content producers whose output markdown.py must correctly render (data coupling, not import coupling):
    vault.py.build_permanent_note_body()      -> emits "[[ZTL - id - slug]]" wikilinks + "![[path]]" embeds
    vault.py.build_literature_index_note()    -> emits "![[path]]" embeds ("Imagens Relacionadas")
    assets.py.extract_markdown_images()       -> emits "![Imagem](path)" standard images
    assets.py.extract_docling_images()        -> emits "![Imagem](path)" standard images
    connector.py / gardener.py / LLM outputs  -> free-form Markdown body text (headings, lists, tables, prose)

External Dependencies:
  - bleach (>=6.4.0, pinned in pyproject.toml/uv.lock at 6.4.0) - HTML sanitization allow-list engine
  - markdown-it-py (>=4.2.0, pinned at 4.2.0) - CommonMark/GFM parser, extensible inline rule engine
      - markdown_it.rules_inline.StateInline - imported directly for the custom rule's type/API surface
  - re (Python standard library) - the _ZTL_WIKILINK pattern
```

`markdown.py` is a leaf/utility module in the dependency graph: it has exactly one internal consumer (`web.py`) and zero internal dependencies of its own, which gives it very low afferent breadth but means its correctness is entirely dictated by two third-party libraries' behavior plus the shape of text produced by five-plus other modules it has no compile-time relationship with (see the "data coupling" list above) — a classic implicit-contract risk: those producer modules can change the Markdown they emit without this module's tests failing, and vice versa.

---

## 6. Afferent and Efferent Coupling

Since `zettel/markdown.py` is a small procedural module (no classes), "components" here are its top-level callables and the module itself as a unit, consistent with Python's function-level granularity for a file this size.

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| `render_markdown` (public function) | 2 (web.py: `note_detail`, `moc_detail` call sites) | 2 (`_MARKDOWN.render`, `bleach.clean`) | High |
| `_render_ztl_wikilink` (internal rule callback) | 1 (registered once with `markdown-it-py`'s inline ruler; invoked internally by the parser's `tokenize`/`skipToken` loop, not by other zettel code) | 3 (`_ZTL_WIKILINK.match`, `StateInline.push`/`attrSet` API, `re` module) | Medium |
| `_MARKDOWN` (module-level parser singleton) | 2 (`render_markdown`, the rule-registration line at import time) | 1 (`markdown_it.MarkdownIt`) | High |
| `markdown.py` (module, as a unit) | 1 (only `web.py` imports it anywhere in the codebase) | 2 (`bleach`, `markdown-it-py`) | High |

Interpretation: afferent coupling is deliberately minimal — a single consumer module — which keeps the blast radius of a signature change small (only `web.py` would need updating). Efferent coupling is concentrated on two external libraries; the module's criticality is "High" not because many things call it, but because it is the sole security control between arbitrary stored text and an HTML response rendered with `|safe` — a defect here has an outsized, security-relevant impact disproportionate to its small caller count.

---

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|--------------|-----------------|
| `zettel/web.py` (`note_detail`, `moc_detail` routes) | Internal module (in-process call) | Convert stored Markdown body to sanitized HTML for template rendering | Direct Python function call | `str \| None` in, `str` (HTML) out | None at the call site — `render_markdown` cannot raise for its declared input type (falsy-input guard); no try/except wraps the call in web.py |
| `bleach` library | External library (in-process) | Second-pass HTML sanitization (tag/attribute/protocol allow-list) | Direct Python call (`bleach.clean`) | HTML string in, HTML string out | No error handling; bleach.clean does not raise on malformed HTML, it repairs/strips it via its underlying html5lib-based parser |
| `markdown-it-py` library | External library (in-process) | CommonMark + GFM table/linkify parsing and HTML rendering, extended with a custom inline rule | Direct Python call (`MarkdownIt.render`) | Markdown string in, raw (unsanitized) HTML string out | No error handling; not expected to raise on arbitrary text input given CommonMark's error-tolerant grammar |
| Jinja2 templates (`note_detail.html`, `moc_detail.html`) | Downstream consumer (not called by this module, but trusts its output) | Injects `rendered_body` via the `|safe` filter, i.e. explicitly opts out of Jinja2's autoescaping for this value | Template variable substitution | Pre-sanitized HTML string | None — by using `\|safe`, the template layer places 100% of the sanitization responsibility on this module; a bypass here reaches the DOM directly |

---

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Defense in Depth | Two independent, differently-mechanized sanitization layers (parser `html: False` + bleach allow-list) applied in series to the same content | markdown.py:12 (parser option) + markdown.py:59-73 (`bleach.clean` call) | Module docstring states explicitly this is intentional redundancy: bleach "protects the rendered output if the parser or its configuration changes later" |
| Allow-list (Positive Security Model) | `_ALLOWED_TAGS`/`_ALLOWED_ATTRIBUTES`/`_ALLOWED_PROTOCOLS` enumerate exactly what is permitted; everything else is stripped by default (`strip=True`) rather than attempting to detect/block specific known-bad patterns | markdown.py:44-56 | Standard, recommended approach for sanitizing untrusted-origin HTML (LLM output and hand-edited vault files are both effectively untrusted from the web layer's point of view) |
| Extension point via plugin/rule registration | `_MARKDOWN.inline.ruler.before("link", "ztl_wikilink", _render_ztl_wikilink)` — the project's proprietary syntax is added as a first-class parser rule rather than as a regex post-process over the rendered HTML | markdown.py:43 | Lets the custom syntax participate correctly in the parser's own precedence/nesting rules (e.g. it naturally coexists with emphasis, code spans, etc. inside the same line), rather than a fragile find-and-replace over already-rendered markup |
| Singleton / module-level configuration object | `_MARKDOWN` and the three `_ALLOWED_*` constants are built once at import time and reused for every `render_markdown()` call | markdown.py:12-56 | Avoids re-constructing the parser and rebuilding the allow-lists on every request; `MarkdownIt` instances are safe to reuse across calls since parsing is stateless per `.render()` invocation |
| Pure function / stateless transform | `render_markdown(text) -> str` has no side effects, no I/O, and depends only on its argument plus immutable module-level configuration | markdown.py:59-74 | Makes the function trivially testable and safe to call concurrently (relevant given the web app's single-worker-but-multi-request-in-flight FastAPI model) |

---

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | Image rendering (whole feature gap) | `img` is not in `_ALLOWED_TAGS`; Obsidian `![[...]]` embed syntax is not parsed as an image; no route serves `90_Assets/` from the web app | Every image the harvest/connector pipeline explicitly extracts, describes, and embeds ("## Figuras", "## Imagens Relacionadas") is invisible on note/MOC detail pages — for image-heavy sources this silently degrades the web UI's usefulness relative to opening the vault directly in Obsidian, with no error or placeholder to signal the gap |
| Medium | `_render_ztl_wikilink` silent-mode contract | The rule returns `True` on a `silent=True` match without advancing `state.pos` (markdown.py:31-32), unlike the library's own rules (e.g. `backtick.py`, which always sets `state.pos` regardless of `silent`) which `markdown-it-py`'s `skipToken`/`parseLinkLabel` nested-bracket-balancing logic appears to assume | Could not be exercised at runtime (out of scope — analysis is static/read-only), but by inspection this creates a plausible mis-parse or stuck-cursor risk specifically when a `[[ZTL - ...]]` wikilink is nested inside another link's label or an image's alt text; recommend a targeted runtime test of that specific input shape to confirm/rule out actual impact |
| Medium | Duplicated wikilink regex across modules | `markdown.py`, `article.py`, and `sync.py` each define their own `_ZTL_WIKILINK`-style pattern independently, with different strictness (`markdown.py`: any `[A-Za-z0-9_-]+` for the ID; `article.py`/`sync.py`: exactly 26-character Crockford-base32) | No shared source of truth for "what does a ZTL wikilink look like" — a future change to the note-ID format (or the wikilink label format) requires updating three call sites by hand, with no test that would fail if one were missed; currently benign only because real IDs are always ULIDs (`new_note.py`, `connector.py` both use `ulid.ULID()`) |
| Low | `_ALLOWED_ATTRIBUTES` silently drops bleach's own `abbr`/`acronym` attribute defaults | `_ALLOWED_ATTRIBUTES` is a fresh dict, not merged with `bleach.sanitizer.ALLOWED_ATTRIBUTES`, so `abbr`/`acronym` (allowed as *tags* via the bleach-default union in `_ALLOWED_TAGS`) get zero allowed attributes, silently dropping `title` | Currently unreachable/inert since nothing in this parser configuration produces `<abbr>`/`<acronym>` tags from Markdown source; would surface as a silent regression only if a future change enabled an abbreviation-producing rule |
| Low | Redundant `linkify` enablement | `MarkdownIt(..., {"linkify": True})` and `.enable([..., "linkify"])` both turn on the same feature | No functional impact, but signals the two toggles were not understood to be equivalent/overlapping when written, a minor maintainability smell |
| Low | No module-level docstring caveat about image behavior | The module docstring ("Safe Markdown rendering for content shown in the web interface") and the `render_markdown` docstring describe the security posture in detail but say nothing about the image-rendering gap documented above | A future maintainer reading only the docstrings (not the test suite) would reasonably assume image embeds work, given the surrounding pipeline's investment in image extraction/description |

---

## 10. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|---------------------|----------|----------------|
| `render_markdown` (headings, lists, blockquote, tables, code) | 0 dedicated unit tests | 1 (`tests/test_web.py::test_note_and_moc_details_render_sanitized_markdown`, via `/notes/{id}` and `/mocs/{id}`) | Partial — covers `<h1>`, `<li>`, `<blockquote>`, `<h2>` (MOC), `<code>` (MOC); does not directly assert `<table>`/`<pre>`/`<hr>`/`<br>` rendering or the `code` tag's `class` attribute pass-through | Good assertions for what it covers (exact substring checks against real rendered HTML, not mocked); gaps are in breadth (table/pre/hr/br paths, and the `_ALLOWED_ATTRIBUTES` `class`/`align` attribute pass-through) rather than depth |
| `_render_ztl_wikilink` (ZTL wikilink -> link translation) | 0 dedicated unit tests | 1 (same test as above: `[[ZTL - note-target - nota-relacionada]]` -> asserts `href="/notes/note-target"`) | Partial — covers the base `[[ZTL - id - slug]]` (no alias) happy path only | Does not test: the `\|alias` form, the id-only `[[ZTL - id]]` form (no slug), the `!` image-embed guard (i.e. `![[ZTL - ...]]` should *not* become a link — untested), a malformed/no-match `[[...]]` falling through to literal text, or nesting inside another link/emphasis/image (the exact scenario flagged as a risk in §9) |
| Security sanitization (`<script>`, `javascript:` href, disallowed tags/attributes) | 0 dedicated unit tests | 1 (same test: `assert "<script>" not in note.text`, `assert 'href="javascript:' not in note.text`) | Partial — covers the two textbook XSS vectors explicitly, but does not test other disallowed schemes (`data:`, `vbscript:`), disallowed attributes on allowed tags (e.g. `<a onclick=...>`, `<code style=...>`), or the `_ALLOWED_ATTRIBUTES` replace-not-merge gap noted in §9 | The two covered cases are asserted precisely and against real end-to-end HTTP responses (not unit-level calls to `render_markdown` directly), which is a strong signal for those specific vectors but leaves the module's broader attack surface unverified by any test |
| Image handling (`![alt](path)`, `![[path]]`) | 0 dedicated unit tests | 1 (same test, for the `![[...]]` embed form only: `assert "![[figura-local.png]]" in note.text`) | Partial — the Obsidian-embed-stays-literal behavior is explicitly pinned; the standard-Markdown-image-gets-stripped behavior (`![alt](path)` producing and then losing an `<img>`) has **no test at all**, despite being independently reachable via `assets.py`'s real output | This is the most consequential coverage gap in the module: the code path most likely to be "fixed" by a future contributor (adding `img` to `_ALLOWED_TAGS`) has no regression test that would catch an incomplete fix (e.g. forgetting the missing `90_Assets` static route) |
| `None`/falsy input handling | 0 dedicated unit tests | 0 (not exercised even indirectly — both web.py call sites always pass a `dict.get("body")` result, and the test fixtures always populate `body`) | None | Untested: `render_markdown(None)` and `render_markdown("")` are never invoked from any test, unit or integration, despite being explicitly type-annotated as supported input (`text: str | None`) |

**Overall assessment**: this component has no dedicated test file; its entire test coverage is a single, well-written integration test embedded in `tests/test_web.py` that happens to exercise several of the module's behaviors as a side effect of testing the `/notes/{id}` and `/mocs/{id}` routes. That test is high-quality for what it checks (real HTTP responses, exact substring assertions, both a "should render" and a "should not render" side for each vector), but the module's public surface — one function, one custom parsing rule, three sanitization allow-lists — has several untested branches (alias wikilinks, the image-embed guard, standard image stripping, `None` input, additional disallowed schemes/attributes) that a dedicated `tests/test_markdown.py` calling `render_markdown()` directly could cover far more cheaply than routing everything through the web test client.

---
