"""DDL for ``state.db``: relational tables, indexes and FTS5 virtual tables.

There is no migration layer. The database belongs to a development vault and
is recreated with ``zettel init --reset`` when the schema changes (ADR-054).
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    path            TEXT PRIMARY KEY,
    file_checksum   TEXT NOT NULL,
    origin_type     TEXT NOT NULL,
    source_id       TEXT,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id            TEXT PRIMARY KEY,
    citekey              TEXT NOT NULL UNIQUE,
    title                TEXT NOT NULL DEFAULT '',
    authors              TEXT NOT NULL DEFAULT '[]',
    year                 INTEGER,
    file_checksum        TEXT NOT NULL,
    extraction_checksum  TEXT,
    origin_path          TEXT NOT NULL,
    origin_type          TEXT NOT NULL,
    extracted_text       TEXT,
    lit_body             TEXT,
    origin               TEXT NOT NULL DEFAULT 'pipeline',
    document_type        TEXT,
    bibliography_json    TEXT,
    -- Chaves bibliograficas de alta precisao, promovidas para fora do blob
    -- bibliography_json para poderem ser indexadas. Normalizadas (minusculas,
    -- sem pontuacao/hifens) por harvester.biblio_dedupe.
    doi                  TEXT,
    isbn                 TEXT,
    abnt_reference       TEXT,
    -- Resumo geral da fonte (ADR-047), reduzido a partir dos resumos de
    -- capitulo. Nao e embarcado: a unidade de busca e o capitulo.
    -- `summary_checksum` e o hash sobre os `summary_checksum` dos capitulos
    -- em ordem, entao qualquer capitulo que mude o invalida.
    summary              TEXT,
    summary_topics       TEXT,
    summary_checksum     TEXT,
    summary_model        TEXT,
    summary_updated_at   TEXT,
    total_pages_file     INTEGER,
    total_pages_book     INTEGER,
    page_offset          INTEGER,
    page_offset_confidence TEXT,
    content_start_file_page INTEGER,
    content_start_book_page INTEGER,
    processing_status    TEXT NOT NULL DEFAULT 'completed',
    last_chunk_processed INTEGER,
    total_chunks         INTEGER,
    docling_config_hash  TEXT,
    cost_usd_total       REAL NOT NULL DEFAULT 0,
    cost_usd_llm         REAL NOT NULL DEFAULT 0,
    cost_usd_embedding   REAL NOT NULL DEFAULT 0,
    tokens_prompt        INTEGER NOT NULL DEFAULT 0,
    tokens_completion    INTEGER NOT NULL DEFAULT 0,
    tokens_embedding     INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    chapter_id       TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    chapter_checksum TEXT NOT NULL,
    locator          TEXT NOT NULL DEFAULT '',
    -- Resumo de capitulo (ADR-047). `summary_checksum` guarda o
    -- `chapter_checksum` vigente quando o resumo foi gerado: quando os dois
    -- divergem o resumo esta defasado. Nunca listadas no DO UPDATE SET de
    -- `upsert_chapter`, para que um re-harvest preserve o resumo.
    summary          TEXT,
    summary_topics   TEXT,
    summary_checksum TEXT,
    summary_model    TEXT,
    summary_updated_at TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id                  TEXT PRIMARY KEY,
    source_id                 TEXT NOT NULL,
    chapter_id                TEXT NOT NULL,
    text                      TEXT NOT NULL,
    chunk_checksum            TEXT NOT NULL,
    locator                   TEXT NOT NULL DEFAULT '',
    section_path              TEXT NOT NULL DEFAULT '',
    status                    TEXT NOT NULL DEFAULT 'pending',
    chunk_index               INTEGER,
    page_in_file              INTEGER,
    page_in_book              INTEGER,
    page_confidence           TEXT NOT NULL DEFAULT 'unknown',
    literature_note_path      TEXT,
    literature_id             TEXT,
    review_confidence         REAL,
    summary_json              TEXT,
    llm_prompt1_hash          TEXT,
    llm_call_checksum_prompt1 TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    FOREIGN KEY (chapter_id) REFERENCES chapters(chapter_id)
);

CREATE TABLE IF NOT EXISTS concepts (
    concept_id     TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL,
    chunk_id       TEXT NOT NULL,
    anchor_hash    TEXT NOT NULL DEFAULT '',
    thesis_hash    TEXT NOT NULL DEFAULT '',
    note_id        TEXT,
    candidate_json TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    dedupe_json    TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id)
);

CREATE TABLE IF NOT EXISTS notes (
    note_id                TEXT PRIMARY KEY,
    source_id              TEXT,
    path                   TEXT,
    title                  TEXT NOT NULL DEFAULT '',
    body                   TEXT,
    frontmatter_json       TEXT,
    provenance_json        TEXT,
    origin                 TEXT NOT NULL DEFAULT 'pipeline',
    note_semantic_checksum TEXT,
    embedding_input_hash   TEXT,
    embedding_model        TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mocs (
    moc_id              TEXT PRIMARY KEY,
    topic               TEXT NOT NULL DEFAULT '',
    path                TEXT,
    body                TEXT,
    frontmatter_json    TEXT,
    origin              TEXT NOT NULL DEFAULT 'pipeline',
    cluster_signature   TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id        TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    chapter_id      TEXT,
    path            TEXT NOT NULL,
    image_checksum  TEXT NOT NULL,
    context_snippet TEXT NOT NULL DEFAULT '',
    description     TEXT,
    description_call_checksum TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    page_in_file    INTEGER,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    call_checksum TEXT PRIMARY KEY,
    request_json  TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_connections (
    source_note_id TEXT NOT NULL,
    target_note_id TEXT NOT NULL,
    relation_type  TEXT NOT NULL,
    description    TEXT DEFAULT '',
    origin         TEXT NOT NULL DEFAULT 'llm',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (source_note_id, target_note_id, relation_type)
);

-- Cheap term -> note routing index (ADR-036), rebuilt from each MOC's notes so
-- `ask` can look a term up. SQLite is its only surface.
-- `note_id` is set only when the target is a permanent note; a literature target
-- routes a human/agent but is not something the Retriever can score.
CREATE TABLE IF NOT EXISTS topic_index_terms (
    scope_kind  TEXT NOT NULL,
    scope_id    TEXT NOT NULL,
    term        TEXT NOT NULL,
    term_folded TEXT NOT NULL,
    target      TEXT NOT NULL,
    note_id     TEXT,
    PRIMARY KEY (scope_kind, scope_id, term_folded, target)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_signature  TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    duplicate_file_count     INTEGER NOT NULL DEFAULT 0,
    duplicate_content_count  INTEGER NOT NULL DEFAULT 0,
    duplicate_semantic_count INTEGER NOT NULL DEFAULT 0,
    duplicate_biblio_count   INTEGER NOT NULL DEFAULT 0,
    cost_usd_total      REAL NOT NULL DEFAULT 0,
    cost_usd_llm        REAL NOT NULL DEFAULT 0,
    cost_usd_embedding  REAL NOT NULL DEFAULT 0,
    tokens_prompt       INTEGER NOT NULL DEFAULT 0,
    tokens_completion   INTEGER NOT NULL DEFAULT 0,
    tokens_embedding    INTEGER NOT NULL DEFAULT 0,
    llm_calls           INTEGER NOT NULL DEFAULT 0,
    cache_hits          INTEGER NOT NULL DEFAULT 0,
    prompt_cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    prompt_cache_write_tokens  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS web_jobs (
    job_id          TEXT PRIMARY KEY,
    operation       TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL DEFAULT 'queued',
    phase           TEXT NOT NULL DEFAULT 'queued',
    current_item    TEXT,
    current_index   INTEGER,
    total_items     INTEGER,
    message         TEXT NOT NULL DEFAULT '',
    result_json     TEXT,
    error_message   TEXT,
    run_id          INTEGER,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS web_job_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    phase           TEXT NOT NULL,
    current_item    TEXT,
    current_index   INTEGER,
    total_items     INTEGER,
    message         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES web_jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_status    ON chunks(status);
CREATE INDEX IF NOT EXISTS idx_chunks_source_id ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_concepts_note_id ON concepts(note_id);
CREATE INDEX IF NOT EXISTS idx_concepts_status  ON concepts(status);
CREATE INDEX IF NOT EXISTS idx_nc_source        ON note_connections(source_note_id);
CREATE INDEX IF NOT EXISTS idx_nc_target        ON note_connections(target_note_id);
CREATE INDEX IF NOT EXISTS idx_topic_terms_folded ON topic_index_terms(term_folded);
CREATE INDEX IF NOT EXISTS idx_assets_source    ON assets(source_id);
CREATE INDEX IF NOT EXISTS idx_assets_status    ON assets(status);
CREATE INDEX IF NOT EXISTS idx_sources_doi      ON sources(doi);
CREATE INDEX IF NOT EXISTS idx_sources_isbn     ON sources(isbn);
CREATE INDEX IF NOT EXISTS idx_chapters_source  ON chapters(source_id);
"""

# FTS5 virtual tables for BM25 lexical search, kept in sync with notes/chunks.
# `remove_diacritics 2` matters for PT-BR: "conexao" matches "conexão".
# Executed separately from SCHEMA_SQL so an fts5-less SQLite build degrades
# gracefully (see ``StateDB._init_fts``) instead of aborting all schema creation.
FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5(
    note_id UNINDEXED, title, body,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    chunk_id UNINDEXED, text,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chapter_summaries USING fts5(
    chapter_id UNINDEXED, title, summary,
    tokenize='unicode61 remove_diacritics 2'
);
"""
