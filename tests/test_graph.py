"""Tests for graph expansion over note_connections."""

import pytest
from zettel.graph import expand_notes
from zettel.state import StateDB


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "graph.db")
    yield db
    db.close()


def _edges(db, edges):
    for src, tgt, rel in edges:
        db.upsert_note_connection(src, tgt, rel)


def test_one_hop_neighbors(db):
    _edges(db, [("a", "b", "supports"), ("a", "c", "contradicts")])
    result = expand_notes(db, ["a"], max_hops=1)
    assert set(result.keys()) == {"b", "c"}
    # contradicts (1.0) outweighs supports (0.8) at hop 1.
    assert result["c"].weight > result["b"].weight
    assert result["c"].hop == 1


def test_two_hops_with_decay(db):
    _edges(db, [("a", "b", "related"), ("b", "c", "related")])
    result = expand_notes(db, ["a"], max_hops=2, decay=0.5)
    assert "b" in result and "c" in result
    # c is one hop further -> its weight carries an extra decay factor.
    assert result["c"].weight < result["b"].weight
    assert result["c"].hop == 2


def test_max_hops_one_excludes_distant(db):
    _edges(db, [("a", "b", "related"), ("b", "c", "related")])
    result = expand_notes(db, ["a"], max_hops=1)
    assert "b" in result
    assert "c" not in result


def test_undirected_reverse_reachable(db):
    # Edge stored as c -> a; expanding from a must still reach c.
    _edges(db, [("c", "a", "extends")])
    result = expand_notes(db, ["a"], max_hops=1)
    assert "c" in result


def test_cycle_does_not_loop(db):
    _edges(db, [("a", "b", "related"), ("b", "a", "related")])
    result = expand_notes(db, ["a"], max_hops=5)
    # a is a seed and must never reappear as its own neighbour.
    assert "a" not in result
    assert "b" in result


def test_seeds_excluded_from_result(db):
    _edges(db, [("a", "b", "related")])
    result = expand_notes(db, ["a", "b"], max_hops=1)
    assert "a" not in result and "b" not in result


def test_max_neighbors_cap(db):
    _edges(db, [("a", f"n{i}", "related") for i in range(10)])
    result = expand_notes(db, ["a"], max_hops=1, max_neighbors=3)
    assert len(result) == 3


def test_relation_weight_override(db):
    _edges(db, [("a", "b", "related")])
    result = expand_notes(db, ["a"], relation_weights={"related": 0.1})
    assert result["b"].weight == pytest.approx(0.1)


def test_seed_weights_scale_neighbors(db):
    _edges(db, [("a", "b", "supports")])
    result = expand_notes(db, ["a"], seed_weights={"a": 0.5})
    # weight = seed_weight(0.5) * supports(0.8) * decay^0(1) = 0.4
    assert result["b"].weight == pytest.approx(0.4)


def test_via_records_path(db):
    _edges(db, [("a", "b", "contradicts")])
    result = expand_notes(db, ["a"], max_hops=1)
    via = result["b"].via
    assert via and via[-1]["relation_type"] == "contradicts"
    assert via[-1]["from"] == "a"


def test_empty_seeds(db):
    assert expand_notes(db, [], max_hops=1) == {}
    assert expand_notes(db, ["x"], max_hops=0) == {}
