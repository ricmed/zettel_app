"""Contract of the ``zettel.state`` package layout (ADR-054)."""

import ast
from pathlib import Path

from zettel import state
from zettel.state import StateDB
from zettel.state.base import StateBase
from zettel.state.db import MIXINS

PACKAGE = Path(state.__file__).resolve().parent

PUBLIC_METHODS = {
    # sources
    "upsert_file",
    "get_file",
    "get_file_by_checksum",
    "upsert_source",
    "update_source_texts",
    "update_source_paging",
    "update_source_summary",
    "add_source_usage",
    "delete_source_cascade",
    "get_source",
    "get_source_by_citekey",
    "get_source_by_extraction_checksum",
    "get_source_by_doi",
    "get_source_by_isbn",
    "list_sources_with_authors",
    "list_sources",
    "search_sources",
    # chapters
    "upsert_chapter",
    "get_chapters_for_source",
    "get_chapter",
    "update_chapter_summary",
    "get_chapters_with_summaries",
    "get_chapters_needing_summary",
    "get_chapter_summary_texts",
    "get_chapter_note_counts",
    "get_notes_for_chapter",
    "get_chapters_for_note",
    "get_chapter_page_ranges",
    # chunks
    "upsert_chunk",
    "get_chunk",
    "get_chunks_for_source",
    "get_chunks_for_chapter",
    "get_chunks_by_status",
    "update_chunk_status",
    "update_chunk_review",
    "update_chunk_pages",
    "reset_chunks_to_pending",
    "reset_chunk_to_pending",
    "delete_chunks",
    "delete_chunks_for_chapter",
    "delete_chapter",
    "search_literature_chunks",
    "search_literature_chunks_fts",
    "next_manual_chunk_index",
    # concepts
    "upsert_concept",
    "get_concept",
    "get_concepts_for_chunk",
    "get_concepts_for_source",
    "get_concepts_by_status",
    "get_concepts_for_notes",
    "update_concept_status",
    "update_concepts_status_for_chunk",
    "set_concept_dedupe",
    # notes
    "upsert_note",
    "update_note_embedding",
    "delete_note",
    "clear_source_id_on_notes",
    "get_note",
    "list_notes",
    "get_notes_for_source",
    "get_note_ids_for_source",
    "get_notes_for_chunk",
    "get_notes_by_ids",
    "get_note_texts",
    "list_permanent_note_ids",
    "count_permanent_notes",
    "catalog_facets",
    "search_catalog",
    # connections
    "upsert_note_connection",
    "get_note_connections",
    "get_connections_for_notes",
    "count_note_connections",
    "get_weighted_note_degrees",
    # mocs + topic index
    "upsert_moc",
    "get_moc",
    "get_moc_by_signature",
    "list_mocs",
    "find_moc_by_topic",
    "find_moc_by_hub_note_id",
    "list_hub_anchor_note_ids",
    "delete_pipeline_mocs",
    "delete_hub_pipeline_mocs",
    "replace_topic_index_terms",
    "delete_topic_index_scope",
    "match_topic_index_scope",
    "match_topic_index",
    # assets
    "upsert_asset",
    "get_asset",
    "get_assets_for_source",
    "get_pending_assets",
    "update_asset_chapter",
    "update_asset_description",
    "reset_failed_assets",
    # llm cache
    "get_cached_llm_response",
    "cache_llm_response",
    "delete_llm_cache",
    # runs
    "start_run",
    "finish_run",
    "record_duplicate",
    "get_run",
    "get_last_run",
    "get_recent_runs",
    # fts
    "search_notes_fts",
    "search_chapter_summaries_fts",
    "rebuild_fts",
    # web + aggregates
    "recover_web_jobs",
    "create_web_job",
    "has_active_web_job",
    "next_queued_web_job",
    "requeue_parked_job",
    "claim_web_job",
    "get_web_job",
    "list_web_jobs",
    "update_web_job",
    "add_web_job_event",
    "list_web_job_events",
    "create_web_harvest_review",
    "get_web_harvest_review",
    "cancel_web_harvest_review",
    "discard_unavailable_web_harvest_reviews",
    "queue_web_harvest_review",
    # decisions (ADR-055 shadow)
    "get_decision_shadow",
    "record_decision_shadow",
    "set_decision_shadow_human",
    "list_decision_shadow",
    "get_stats",
    "get_web_dashboard",
    # connection lifecycle (db.py)
    "close",
    "vacuum",
}


def _own_attributes(cls: type) -> set[str]:
    return {name for name in vars(cls) if not name.startswith("__")}


def test_public_api_is_exactly_the_expected_set():
    """A method lost (or added) in a refactor shows up here, not at a call site."""
    public = {name for name in dir(StateDB) if not name.startswith("_")}
    assert public == PUBLIC_METHODS


def test_no_two_mixins_define_the_same_attribute():
    """The MRO would silently shadow one of them."""
    seen: dict[str, str] = dict.fromkeys(_own_attributes(StateBase), "StateBase")
    for mixin in (*MIXINS, StateDB):
        for name in _own_attributes(mixin):
            assert name not in seen, f"{mixin.__name__}.{name} ja definido em {seen[name]}"
            seen[name] = mixin.__name__


def test_every_mixin_builds_on_state_base():
    assert all(issubclass(mixin, StateBase) for mixin in MIXINS)
    assert set(StateDB.__bases__) == set(MIXINS)


def test_submodules_import_siblings_never_the_package_namespace():
    """``from zettel.state import X`` inside the package would close an import cycle."""
    for path in PACKAGE.glob("*.py"):
        if path.stem == "__init__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "zettel.state", f"{path.name} importa do __init__"
                assert not node.level, f"{path.name}: use imports absolutos entre irmaos"


def test_no_stale_state_module_beside_the_package():
    """A ``zettel/state.py`` next to the package is never imported (the package
    wins), so anything added to it is silently unreachable. A merge from a branch
    cut before ADR-054 resurrected it once and lost the web harvest-review API."""
    assert not (PACKAGE.parent / "state.py").exists()
