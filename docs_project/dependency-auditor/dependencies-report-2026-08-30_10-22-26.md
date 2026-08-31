# Dependency Audit Report

**Project:** zettel_app
**Audit date:** 2026-08-30 10:22:26
**Scope:** Entire project root (`D:/projetos/zettel_app`)
**Excluded folders:** `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, `.pytest_cache`
**Ecosystem:** Python 3.12, managed with UV (`pyproject.toml` + `uv.lock`)

---

## 1. Summary

zettel_app is a single-ecosystem Python 3.12 project: a CLI pipeline (harvest → extract → review → connect → garden) that converts PDF/Markdown into Obsidian Zettelkasten notes, plus a FastAPI web UI. Dependencies are declared in `pyproject.toml` (34 direct dependencies) and resolved/pinned in `uv.lock` (~230 packages including transitives). A second file, `requirements.txt`, also exists in the repo root with looser, older version constraints.

Two dependency manifests were found:

- **`pyproject.toml` + `uv.lock`** — the authoritative source per `CLAUDE.md` ("This project uses UV for management dependencies"). Used for the audit's version comparison.
- **`requirements.txt`** — a legacy/parallel manifest with stale lower bounds (e.g. `langchain-openai>=0.2.0`, `chromadb>=0.5.0`, `python-ulid>=2.0.0`, `rich>=13.0.0`, `typer[all]>=0.9.0`) that no longer match what `pyproject.toml`/`uv.lock` actually install. It is not used by any command in `CLAUDE.md` (all commands go through `.venv/Scripts/python.exe` built from `uv`/`pyproject.toml`).

No Node/JS manifest exists — the web UI is server-rendered Jinja2/FastAPI with no bundler, consistent with `CLAUDE.md`.

**Main findings:**

- One **Critical** severity, actively-exploited-in-the-wild CVE affects the pinned vector-store dependency (`chromadb` 1.5.9, CVE-2026-45829, CVSS 10.0). Exposure in this codebase is reduced (see Risk Analysis) but the dependency itself remains unpatched as of the versions available at audit time.
- `bleach` 6.4.0, the project's only HTML-sanitization library (used to sanitize rendered Markdown before it reaches the browser), reached permanent end-of-life on 2026-06-05 — it will receive **no further releases, including for future security vulnerabilities**.
- `PyMuPDF` (imported as `fitz`) is licensed **AGPL-3.0**, with no commercial license purchased per project files found — a legal-risk item if the project is ever distributed, hosted as SaaS, or has its source kept closed.
- Two redundant/legacy packaging artifacts were found: `typer-slim==0.21.1` (deprecated by upstream, superseded by `typer` itself since 0.22.0) and `ulid==1.1` (an abandoned 2016 package duplicating `python-ulid`, which the project also depends on and actually uses per `CLAUDE.md`'s "notes/mocs use ULID").
- `openai` is pinned one major version behind the current SDK release (2.54.0 installed vs. 3.3.0 latest), most likely held back by an unresolved/未-refreshed lock rather than an explicit ceiling.
- No CVEs currently affect `fastapi`, `starlette`, `uvicorn`, `jinja2`, `python-multipart`, `pydantic`, or `torch`/`torchvision` at the versions actually resolved in `uv.lock` — each sits well past the fixed version for every CVE found in research for that package.

---

## 2. Critical Issues

### 2.1 CVE-2026-45829 — ChromaDB pre-authentication RCE (CVSS 10.0)

- **Package / version:** `chromadb==1.5.9` (pinned exactly in `pyproject.toml` and `uv.lock`)
- **Affected range:** `>=1.0.0, <=1.5.9` — the vulnerability was still unpatched in the newest available release at the time of this audit.
- **Mechanism:** the ChromaDB Python FastAPI server processes user-controlled embedding-function configuration (including a HuggingFace model reference with `trust_remote_code`) *before* authentication is checked, allowing an unauthenticated attacker to force the server to download and execute arbitrary code via the collection-creation endpoint.
- **Exposure in this codebase:** `zettel/index.py` instantiates ChromaDB exclusively via `chromadb.PersistentClient(...)` (in-process embedded mode) — it does not start or expose the ChromaDB FastAPI server (`chroma run` / `HttpClient`) anywhere in `zettel/` or in `.replit`. Per the vulnerability's own disclosure, only the standalone FastAPI server component is affected; the embedded `PersistentClient` code path does not open the vulnerable HTTP endpoint. **This meaningfully lowers — but does not eliminate — real-world exposure**: the vulnerable code ships inside the installed package regardless, and any future change that runs `chroma run`, exposes an `HttpClient` endpoint, or spins up the server for multi-process/network access would immediately be exposed.
- **Recommendation (reporting only, no fix applied):** track the ChromaDB security advisory (GHSA-f4j7-r4q5-qw2c) for a patched release; do not add any network-facing ChromaDB server mode to this deployment; if `SentenceTransformerEmbeddingFunction`/HF-hub embedding paths in `zettel/index.py` ever accept `trust_remote_code=True` from user/config input, treat that as equally critical even in embedded mode.

### 2.2 `bleach` 6.4.0 — permanent end-of-life, security-relevant sanitizer

- **Package / version:** `bleach==6.4.0` (direct dependency, no version ceiling in `pyproject.toml`)
- **Status:** Bleach entered minimum-maintenance mode in 2023 and was formally end-of-lifed on 2026-06-05 with 6.4.0 as its **final** release — no further releases will be made, including for security vulnerabilities. The maintainers cite it sitting on top of the also largely unmaintained `html5lib` as the reason it could not continue.
- **Usage in codebase:** `zettel/markdown.py` imports `bleach` directly and is the sanitizer used by `render_markdown()`, which `zettel/web.py` calls to render **user-authored note and MOC Markdown bodies directly into HTML served to the browser** (`rendered_body=render_markdown(note.get("body"))`, `zettel/web.py:522,539`). Any future HTML-sanitizer bypass in bleach/html5lib will never be patched upstream.
- **Recommendation (reporting only):** evaluate a maintained successor (e.g. `nh3`, the Rust-backed `ammonia` binding, or `justhtml`, built by a former Bleach maintainer as a migration path) at the project's own pace; this is a "no future patches" risk, not an active exploit today.

### 2.3 `PyMuPDF` — AGPL-3.0 licensing, no commercial license on file

- **Package / version:** `pymupdf==1.28.2` (imported as `fitz`; used directly in `zettel/harvester.py`, `zettel/cli.py`, `zettel/extraction_dump.py`, `zettel/paging.py`)
- **Issue:** PyMuPDF/MuPDF is dual-licensed — free only under AGPL-3.0 (which requires the *complete* application that links against it, including any network-accessible service, to also be released under an AGPL-compatible copyleft license), or under a paid commercial license from Artifex. No `LICENSE` file was found anywhere in the project root, and no evidence of a purchased Artifex commercial license was found in project files.
- **Risk:** currently low practical risk for a private/local tool, but this becomes a real legal-compliance question the moment the FastAPI web UI (`zettel/web.py`, run with `--host 0.0.0.0`) is exposed to any user outside the license holder, or if the project or the vault it produces is ever redistributed as closed-source software.
- **Recommendation (reporting only):** decide and document the project's own license; if it stays private/personal-use this is moot, but if distribution or hosting-for-others is ever planned, either publish under AGPL-3.0-compatible terms or acquire a commercial PyMuPDF/MuPDF license from Artifex.

---

## 3. Dependencies

Direct dependencies declared in `pyproject.toml`, compared against the version actually resolved in `uv.lock` and the latest version found via external research at audit time (2026-08-30).

| Dependency | Current Version (uv.lock) | Latest Version (verified) | Status |
|---|---|---|---|
| bleach | 6.4.0 | 6.4.0 (final release — project EOL) | **Legacy / Deprecated** |
| chromadb | 1.5.9 (pinned `==`) | 1.5.9 (CVE-2026-45829 unpatched at this version) | **Outdated / Vulnerable** |
| docling | 2.123.1 | 2.123.1 (actively released; org-level rebrand to LF AI & Data Agentic AI Foundation in 2026) | Up to Date |
| hdbscan | 0.8.44 (pinned `==`) | 0.8.44 (low release cadence; niche/slow-moving, not abandoned) | Up to Date (Low Cadence) |
| hf-xet | 1.2.0 (pinned `==`) | 1.2.0 | Up to Date |
| langchain-core | 1.6.1 | 1.6.1 (search-indexed "latest" of 1.4.8 appears stale/lagging vs. what `uv.lock` resolved same week) | Up to Date |
| langchain-openai | 1.6.0 | 1.6.0 (1.5.0 added openai-3.x support per LangChain changelog) | Up to Date |
| langchain-text-splitters | 1.1.2 | 1.1.2 | Up to Date |
| langgraph | 1.2.11 | 1.2.11 | Up to Date |
| openai | 2.54.0 | 3.3.0 (released 2026-08-18) | **Outdated (one major version behind)** |
| pydantic | 2.13.5 | 2.13.5 | Up to Date |
| pytest | 9.1.1 | 9.1.1 | Up to Date |
| python-dotenv | 1.2.3 | 1.2.3 | Up to Date |
| python-ulid | 4.0.1 | 4.0.1 | Up to Date |
| pyyaml | 6.0.3 | 6.0.3 | Up to Date |
| rich | 15.0.0 | 15.0.0 | Up to Date |
| fastapi | 0.141.1 | 0.141.1 (confirmed, released 2026-07-29) | Up to Date |
| uvicorn | 0.52.4 | 0.52.4 | Up to Date |
| jinja2 | 3.1.6 | 3.1.6 | Up to Date |
| python-multipart | 0.0.32 | 0.0.32 (well past CVE-2024-53981 fix in 0.0.18) | Up to Date |
| PyMuPDF | 1.28.2 | 1.28.2 | Up to Date (see License risk, §2.3) |
| scikit-learn | 1.9.0 | 1.9.0 | Up to Date |
| typer | 0.26.8 | 0.26.8 | Up to Date |
| typer-slim | 0.21.1 (pinned `==`) | n/a — package deprecated by upstream | **Deprecated / Redundant** |
| ulid | 1.1 | 1.1 (package effectively abandoned since ~2016) | **Legacy / Unmaintained (>1 year)** |
| umap-learn | 0.5.12 (pinned `==`) | 0.5.12 (actively released, healthy per Snyk maintenance signal) | Up to Date |
| torch | 2.13.0+cu126 | 2.13.0+cu126 (past CVE-2025-32434 fix in 2.6.0 and CVE-2026-24747 fix in 2.10.0) | Up to Date |
| torchvision | 0.28.0+cu126 | 0.28.0+cu126 | Up to Date |
| langchain-ollama | 1.1.0 | 1.1.0 | Up to Date |
| litellm | 1.98.0 | ~1.97–1.98 (search results conflicting/stale; resolved version is the most current data point available) | Up to Date |
| linkify-it-py | 2.2.0 | 2.2.0 | Up to Date |
| markdown-it-py | 4.2.0 | 4.2.0 | Up to Date |
| ruff | 0.16.5 | 0.16.5 | Up to Date |
| langchain-chroma | 1.1.0 | 1.1.0 | Up to Date |

**Note on `requirements.txt`:** this second manifest is not evaluated row-by-row above because it is not the authoritative install path (`CLAUDE.md` routes every command through the UV-managed venv). It is flagged as a structural risk in §4 because its loose/stale bounds (e.g. `chromadb>=0.5.0`, `langchain-openai>=0.2.0`, `docling>=2.0.0`, `python-ulid>=2.0.0`) would resolve to very different — and in some cases pre-CVE-fix — versions if anyone ever ran `pip install -r requirements.txt` directly instead of `uv sync`.

---

## 4. Risk Analysis

| Severity | Dependency | Issue | Details |
|---|---|---|---|
| Critical | chromadb 1.5.9 | CVE-2026-45829 (CVSS 10.0) | Pre-authentication RCE in ChromaDB's FastAPI server via unauthenticated collection-creation + `trust_remote_code`; unpatched at the pinned/latest-available version. Exposure reduced because `zettel/index.py` only ever uses embedded `PersistentClient`, never the network server — but the vulnerable code ships in the installed package. |
| High | bleach 6.4.0 | Permanent end-of-life (security-relevant package) | Sole HTML sanitizer for Markdown rendered to the browser (`zettel/markdown.py` → `zettel/web.py`); final release 2026-06-05, no future security patches will ever be issued for it or its `html5lib` dependency. |
| High | PyMuPDF 1.28.2 | AGPL-3.0 license / no commercial license found | Copyleft license attaches to any network service linking it; project has no `LICENSE` file and the web UI can bind `0.0.0.0`. Legal risk activates only if distributed, hosted for others, or kept closed-source. |
| Medium | requirements.txt (whole file) | Stale/duplicate manifest, maintenance burden | Diverges sharply from `pyproject.toml`/`uv.lock` (e.g. `chromadb>=0.5.0` vs. pinned `1.5.9`, `python-ulid>=2.0.0` vs. resolved `4.0.1`). A `pip install -r requirements.txt` would not reproduce the audited, CVE-checked environment and could silently reintroduce already-fixed vulnerabilities (e.g. pre-0.0.18 `python-multipart`, CVE-2024-53981). |
| Medium | openai 2.54.0 | One major version behind (latest 3.3.0, released 2026-08-18) | `langchain-openai` 1.5.0+ already supports the openai 3.x SDK per its changelog, so the gap is not an obvious hard incompatibility; likely just an unrefreshed lock. Upgrading across a major SDK version can carry breaking changes to request/response shapes used throughout `zettel/llm.py`. |
| Medium | typer-slim 0.21.1 | Deprecated, redundant with `typer` | Upstream confirms `typer-slim` has been a no-op alias installing full `typer` since `typer` 0.22.0; the project separately pins `typer-slim==0.21.1` (pre-dating that change) alongside `typer>=0.21.1` (resolved 0.26.8) — two overlapping CLI-framework declarations with an inconsistent, pinned-older sub-package. |
| Low | ulid 1.1 | Unmaintained >1 year (effectively abandoned since 2016) | Coexists with `python-ulid` (resolved 4.0.1, actively maintained, "sustainable" per Snyk), which `CLAUDE.md` confirms is the one actually used for note/MOC IDs. `ulid` package's purpose in the dependency tree is unclear and adds an unmaintained, functionally-overlapping package to the supply chain. |
| Low | hdbscan 0.8.44 | Low release cadence | Actively used for clustering in `zettel/gardener.py`; not abandoned, but release cadence is slow relative to the rest of the stack — worth periodic re-checking rather than an immediate concern. |
| Informational | torch/torchvision 2.13.0/0.28.0 | Historical CVEs already fixed | CVE-2025-32434 (fixed 2.6.0) and CVE-2026-24747 (fixed 2.10.0) both predate the currently resolved 2.13.0 — no action needed, listed for completeness since `torch.load` usage patterns are a recurring PyTorch CVE class worth re-checking on every future upgrade. |

---

## 5. Unverified Dependencies

| Dependency | Current Version | Reason Not Verified |
|---|---|---|
| litellm | 1.98.0 | External search results for "latest litellm version" returned conflicting/stale numbers (1.89.2 and 1.97.0 from different sources), both lower than the version actually resolved in `uv.lock`. Could not confirm a single authoritative "latest" figure to compare against; treated the resolved version as the most current available data point. |
| langchain-core | 1.6.1 | One search source reported "latest" as 1.4.8, lower than the version `uv.lock` actually resolved (1.6.1) as of the day before this audit — the search index appears to lag actual PyPI releases for this fast-moving package. Direct PyPI page confirmation was not independently re-fetched. |
| hf-xet, ulid, ruff, langchain-ollama | 1.2.0 / 1.1 / 0.16.5 / 1.1.0 | No CVE-specific or dedicated release-history search was run for these (lower-impact/utility packages); versions are reported as resolved by `uv.lock` without independent latest-version confirmation beyond general package-index snapshots. |

No MCP servers for external validation (Context7, Firecrawl) were available in this environment; all external verification was performed via web search against PyPI, GitHub advisories, CVE databases, and vendor/security blogs.

---

## 6. Critical File Analysis (Top 10)

Ranked by concentration of risky/flagged dependencies and blast radius if that dependency fails or is compromised.

1. **`zettel/index.py`** (766 lines) — The sole integration point with `chromadb` (`PersistentClient`, 5 collections: sources, chunks, permanent_notes, mocs, literature_notes). Carries the CVE-2026-45829-affected package; also the only file wiring in `OpenAIEmbeddingFunction`/`SentenceTransformerEmbeddingFunction`, so any future change toward `trust_remote_code=True` or a network `HttpClient` would surface here first. Every phase of the pipeline (harvest, extract, connect, garden, sync, ask, article) depends on this file.

2. **`zettel/markdown.py`** — Small file, but the single choke-point for `bleach`-based HTML sanitization of every note/MOC body the web UI renders. A future sanitizer bypass (never to be patched upstream) has its entire blast radius here.

3. **`zettel/web.py`** (622 lines) — Consumes `zettel/markdown.py`'s `render_markdown()` to inject sanitized HTML into server-rendered pages, and is the FastAPI entry point bound to `0.0.0.0:5000`. Combines the bleach EOL risk with direct exposure of the AGPL-licensed PyMuPDF-derived pipeline output to external users, and owns session/CSRF handling (`SESSION_SECRET`).

4. **`zettel/harvester.py`** (1,894 lines, largest module) — Direct `fitz`/`pymupdf` usage (AGPL license, §2.3) for page-mapping, plus the primary `docling` integration for PDF extraction, plus the three-layer duplicate-detection logic that queries ChromaDB. Concentrates three flagged/audited dependencies (PyMuPDF, docling, chromadb) in one file.

5. **`zettel/cli.py`** (1,934 lines, largest file in the project) — The single entry point for every subcommand; imports `fitz`/PyMuPDF directly alongside `typer` (and transitively `typer-slim`'s redundant pin). Any CLI-level regression from the `typer`/`typer-slim` version mismatch (§4) would manifest here first, and it is the widest-blast-radius file for any dependency import error since every command routes through it.

6. **`zettel/llm.py`** (419 lines) — The shared LLM provider abstraction (`get_llm`/`call_llm`) used by every LLM-calling phase (harvest's chunking guidance, extract, connect, garden, review, ask, article). Directly wires in `openai`, `litellm`, and LangChain provider classes — the file most exposed to the `openai` 2.x→3.x major-version gap (§4), since a future SDK upgrade would need every call site here re-validated.

7. **`zettel/article_graph.py`** (715 lines) — The LangGraph `StateGraph` implementation for the `article` command; the heaviest direct consumer of `langgraph`'s API surface (which has moved from a `>=0.2.0` floor in `pyproject.toml` to a resolved `1.2.11` — a large jump across LangGraph's own breaking 1.0 rewrite). Most likely file to break on a future LangGraph upgrade.

8. **`zettel/gardener.py`** (892 lines) — Only consumer of `umap-learn` and `hdbscan` (both niche, slower-release-cadence clustering libraries, §4) combined with `scikit-learn`. A version or API drift in either niche library surfaces exclusively here, with no fallback path described in `CLAUDE.md` beyond "HDBSCAN noise stays out of MOCs."

9. **`zettel/connector.py`** (635 lines) — Bridges `Retriever` (hybrid ChromaDB + FTS5) with `llm.py`'s LLM abstraction for Phase 3 note generation; a dependency fault in either `chromadb` or the LangChain/OpenAI stack propagates directly into permanent-note creation, the pipeline's core output.

10. **`zettel/extractor.py`** (639 lines) — Phase 2's LLM-driven literature-note drafting; alongside `connector.py`, the other primary consumer of the `llm.py` abstraction and thus the `openai`/`litellm`/LangChain dependency cluster, gating whether any harvested chunk ever becomes reviewable content.

---

## 7. Integration Notes

- **chromadb** — embedded only, via `PersistentClient` in `zettel/index.py`; 5 named collections; never run as a standalone server in this codebase.
- **docling / PyMuPDF (fitz)** — `docling` is the primary PDF-to-Markdown extractor; `PyMuPDF` is used specifically for page-number mapping (`paging.py`) and low-level PDF page inspection in `harvester.py`, `cli.py`, and `extraction_dump.py` — not as a competing extractor.
- **bleach** — used only inside `zettel/markdown.py`'s `render_markdown()`, called exclusively from `zettel/web.py` to sanitize note/MOC Markdown before HTML is served.
- **openai / litellm / LangChain family (`langchain-core`, `langchain-openai`, `langchain-text-splitters`, `langchain-ollama`, `langchain-chroma`)** — unified behind `zettel/llm.py`'s `get_llm`/`call_llm` helpers; `litellm` is explicitly used only as a pricing/cost-estimation library (`cost_per_token`), never as an LLM client, per `CLAUDE.md`.
- **langgraph** — isolated to the `article` command's `article_graph.py` StateGraph (query enrich → search → HITL → catalog → outline → draft → assemble → rewrite → judge loop) and is not used by the core harvest/extract/review/connect/garden pipeline.
- **umap-learn / hdbscan / scikit-learn** — used together only in `zettel/gardener.py` for MOC clustering (UMAP dimensionality reduction + HDBSCAN density clustering, with KMeans/scikit-learn fallback).
- **typer / typer-slim / rich** — `typer[all]` (via `rich` + `shellingham`) powers the entire CLI surface in `cli.py`; `typer-slim`'s separate pin appears vestigial given typer-slim has been a no-op since typer 0.22.0.
- **python-ulid / ulid** — `python-ulid` generates the ULIDs used for note/MOC identifiers per `CLAUDE.md`; the separate `ulid` package's actual call site was not identified in `zettel/` during this audit and its presence appears redundant.
- **fastapi / uvicorn / jinja2 / python-multipart** — the web UI stack (`zettel/web.py`, `zettel/web_app.py`), templated via Jinja2, running as a single Uvicorn worker with a SQLite-backed job queue; no separate Node/JS build tooling exists.
- **pydantic** — schema layer for all LLM structured outputs and config (`schemas.py`, `config.py`), v2 throughout.

---

## 8. Report Saved

The complete report has been saved to:

`D:\projetos\zettel_app\docs_project\dependency-auditor\dependencies-report-2026-08-30_10-22-26.md`
