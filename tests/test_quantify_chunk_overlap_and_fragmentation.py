"""Tests for the overlap/fragmentation quantification spike (issue #65).

Only the pure aggregation functions (`fragmented_rejection_stats`,
`overlap_cost_stats`) are covered -- the `_load_*`/`print_*` functions touch a real
state.db and are meant to be run manually against the actual corpus.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from quantify_chunk_overlap_and_fragmentation import (
    fragmented_rejection_stats,
    overlap_cost_stats,
)


def _summary(status: str, category: str = "") -> str:
    return json.dumps({"chunk_status": status, "rejection_category": category})


def test_fragmented_rejection_stats_no_category_data():
    """The exact situation on the local corpus today: rejected, but no category."""
    summaries = [_summary("rejected"), _summary("rejected"), _summary("accepted")]
    stats = fragmented_rejection_stats(summaries)
    assert stats["rejected"] == 2
    assert stats["with_category"] == 0
    assert stats["fragmented_pct_of_labeled"] is None


def test_fragmented_rejection_stats_with_category_data():
    summaries = [
        _summary("rejected", "fragmented"),
        _summary("rejected", "fragmented"),
        _summary("rejected", "trivial"),
        _summary("rejected", "structural"),
        _summary("accepted"),
    ]
    stats = fragmented_rejection_stats(summaries)
    assert stats["rejected"] == 4
    assert stats["with_category"] == 4
    assert stats["fragmented"] == 2
    assert stats["fragmented_pct_of_labeled"] == 50.0


def test_fragmented_rejection_stats_ignores_malformed_json():
    summaries = ["not json", None, "", _summary("rejected", "fragmented")]
    stats = fragmented_rejection_stats(summaries)
    assert stats["rejected"] == 1
    assert stats["fragmented"] == 1


def test_overlap_cost_stats_no_overlap_when_sections_fit():
    chunks_by_chapter = {"ch1": ["primeiro pedaco", "segundo pedaco sem relacao nenhuma"]}
    stats = overlap_cost_stats(chunks_by_chapter, overlap_cap=400)
    assert stats["total_chunks"] == 2
    assert stats["chunks_with_overlap"] == 0
    assert stats["overlap_chars_pct"] == 0.0


def test_overlap_cost_stats_detects_shared_boundary():
    # "curr" starts with the last 10 chars of "prev".
    prev = "x" * 90 + "sharedtail"
    curr = "sharedtail" + "y" * 90
    chunks_by_chapter = {"ch1": [prev, curr]}
    stats = overlap_cost_stats(chunks_by_chapter, overlap_cap=400)
    assert stats["chunks_with_overlap"] == 1
    assert stats["total_overlap_chars"] == 10
    assert stats["total_chars"] == len(prev) + len(curr)


def test_overlap_cost_stats_does_not_cross_chapter_boundaries():
    """Overlap only ever applies within one chapter's own splitter run."""
    shared = "x" * 50
    chunks_by_chapter = {
        "ch1": [f"{shared}aaa"],
        "ch2": [f"{shared}bbb"],  # would "overlap" with ch1's chunk if chapters were merged
    }
    stats = overlap_cost_stats(chunks_by_chapter, overlap_cap=400)
    assert stats["chunks_with_overlap"] == 0


def test_overlap_cost_stats_empty_corpus_is_safe():
    stats = overlap_cost_stats({}, overlap_cap=400)
    assert stats["total_chunks"] == 0
    assert stats["overlap_chars_pct"] == 0.0
