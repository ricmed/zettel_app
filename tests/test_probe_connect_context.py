"""Tests for the #212/#231 connect probe: pure pieces, export, scoring, revision. Offline."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_connect_context import (
    NO_EDGE,
    build_pairs,
    labelled_conditions,
    parse_answer,
    pick_concepts,
    rag_context_for,
    result_ids,
    sample_pairs,
    stratum_of,
    summarize_runs,
    write_export,
    write_revision,
)
from score_decision_gold import read_sheet, relations_labels, score_relations

A, B, C, D = (f"01ARZ3NDEKTSV4RRFFQ69G5FA{c}" for c in "VWXY")


def _item(key, similar, distant=()):
    def note(nid, hop=0):
        return {
            "note_id": nid,
            "title": f"Nota {nid[-1]}",
            "document": "Tese: " + "t" * 300 + "\n\nDefinição: d",
            "hop": hop,
            "via": [],
            "tags": "x",
            "path": None,
        }

    return {
        "item_key": key,
        "source_id": "@S",
        "candidate": {"thesis": f"tese {key}", "definition": "def"},
        "literature_ref": "[[LIT]]",
        "images_context": "",
        "similar": [note(n) for n in similar],
        "distant": [note(n) for n in distant],
    }


def _accepted(**edges):
    return {"status": "accepted", "edges": edges}


def test_pick_concepts_round_robins_sources_and_is_seeded():
    rows = [{"concept_id": f"c{i}", "source_id": "@big"} for i in range(10)]
    rows += [{"concept_id": "s1", "source_id": "@small"}]
    picked = pick_concepts(rows, 4, seed=1)
    assert len(picked) == 4
    assert sum(r["source_id"] == "@small" for r in picked) == 1
    assert picked == pick_concepts(rows, 4, seed=1)


def test_parse_answer_keeps_offered_targets_and_demotes_corroborates():
    text = json.dumps(
        {
            "status": "accepted",
            "title": "T",
            "thesis": "t",
            "definition": "d",
            "connections": [
                {"related_note_id": f"[[ZTL - {A}]]", "relation_type": "extends"},
                {"related_note_id": A, "relation_type": "supports"},
                {"related_note_id": B, "relation_type": "corroborates"},
                {"related_note_id": C, "relation_type": "related"},
            ],
        }
    )
    entry = parse_answer(text, {A, B})
    assert entry == {"status": "accepted", "edges": {A: "extends", B: "supports"}}
    assert parse_answer('{"status": "rejected", "reason": "vago"}', {A})["edges"] == {}
    assert "error" in parse_answer("nao e json", {A})


def test_rag_context_differs_only_in_the_note_chars():
    item = _item("k", [A], [B])
    trunc, full = rag_context_for(item, 150), rag_context_for(item, 6000)
    assert "Definição: d" in full and "Definição" not in trunc
    assert trunc.count("note_id:") == full.count("note_id:") == 2


def test_pairs_strata_exclude_rejected_concepts_and_sample_is_blind():
    snapshot = [_item("k1", [A, B, C]), _item("k2", [D])]
    runs = {
        "trunc-a": {"k1": _accepted(**{A: "supports", B: "extends"}), "k2": _accepted()},
        "full-a": {"k1": _accepted(**{A: "extends", C: "related"}), "k2": {"status": "rejected"}},
        "trunc-b": {"k1": _accepted()},
        "full-b": {},
    }
    pairs, excluded = build_pairs(snapshot, runs)
    assert excluded == ["k2"]
    strata = {p["note_id"]: p["sampling_stratum"] for p in pairs}
    assert strata == {A: "both", B: "trunc_only", C: "full_only"}
    assert next(p for p in pairs if p["note_id"] == A)["answers"]["full-b"] == NO_EDGE
    assert stratum_of(NO_EDGE, NO_EDGE) == "neither"

    sampled = sample_pairs(pairs, per_stratum=1, seed=0)
    assert [p["item_id"] for p in sampled] == ["R001", "R002", "R003"]


def test_summary_reports_validity_edges_and_stability():
    snapshot = [_item("k1", [A, B]), _item("k2", [C])]
    runs = {
        "trunc-a": {"k1": _accepted(**{A: "supports"}), "k2": _accepted()},
        "trunc-b": {"k1": _accepted(**{A: "supports"}), "k2": _accepted(**{C: "related"})},
        "full-a": {"k1": _accepted(**{A: "extends", B: "related"})},
        "full-b": {},
    }
    summary = summarize_runs(snapshot, runs, [("trunc-a", "full-a")])
    assert summary["runs"]["full-a"]["invalid_share"] == 0.5
    assert summary["runs"]["trunc-a"]["edges_per_note"] == 0.5
    assert summary["stability"]["trunc"] == {"pairs": 3, "agreement": 0.6667}
    assert summary["agreement"]["trunc-a x full-a"] == {"pairs": 2, "agreement": 0.0}
    assert summary["stability"]["full"] == {"pairs": 0, "agreement": None}  # full-b is empty


def test_export_then_score_relations(tmp_path):
    snapshot = [_item("k1", [A, B]), _item("k2", [C])]
    runs = {
        "trunc-a": {"k1": _accepted(**{A: "supports"}), "k2": _accepted()},
        "full-a": {"k1": _accepted(**{A: "extends", B: "related"}), "k2": _accepted()},
        "trunc-b": {},
        "full-b": {},
    }
    pairs, excluded = build_pairs(snapshot, runs)
    sampled = sample_pairs(pairs, per_stratum=5, seed=0)
    _sheet, reading, key_path = write_export(
        sampled,
        snapshot,
        out_dir=tmp_path,
        population={"both": 1, "full_only": 1, "neither": 1},
        seed=0,
        run_meta={},
        excluded=excluded,
    )
    text = reading.read_text(encoding="utf-8")
    assert "supports" in text and "trunc" not in text and A not in text
    key = json.loads(key_path.read_text(encoding="utf-8"))
    by_pair = {i["subject_id"].split("|")[-1]: i["item_id"] for i in key["items"]}

    human = {A: "extends", B: "nenhuma", C: "?"}
    rows = "\n".join(f"{by_pair[n]};{rel};" for n, rel in human.items())
    labels, problems = relations_labels(read_sheet(f"item_id;relacao;nota\n{rows}\n".encode()), key)
    assert not problems
    result = score_relations(labels, key)
    assert result["conditions"]["full-a"]["correct"] == 1  # A right, B wrong edge
    assert result["conditions"]["trunc-a"]["correct"] == 1  # A wrong type, B right
    assert result["detail"]["trunc-a"]["edge_presence"]["accuracy"] == 1.0
    assert result["detail"]["trunc-a"]["type_on_shared_edges"]["accuracy"] == 0.0
    assert result["preregistered_rule_212"]["full_not_worse"]


def test_result_ids_keep_bare_runs_for_one_prompt():
    assert result_ids(["current"], ["trunc-a"]) == ["trunc-a"]
    assert result_ids(["current", "new"], ["trunc-a"]) == ["current:trunc-a", "new:trunc-a"]


def test_labelled_conditions_read_each_run_for_the_sheet_pairs():
    key = {
        "items": [
            {"item_id": "R001", "subject_id": f"c1|n1|{A}"},
            {"item_id": "R002", "subject_id": f"c1|n1|{B}"},
        ]
    }
    runs = {"new:trunc-a": {"c1|n1": _accepted(**{A: "extends"})}, "current:trunc-a": {}}
    conditions = labelled_conditions(key, runs)
    assert conditions["new:trunc-a"] == {
        "R001": {"decision": "extends"},
        "R002": {"decision": NO_EDGE},
    }
    assert conditions["current:trunc-a"]["R001"] == {"decision": NO_EDGE}


def test_revision_sheet_shows_the_previous_label_and_the_new_rules(tmp_path):
    snapshot = [_item("c1|n1", [A])]
    key = {"items": [{"item_id": "R001", "subject_id": f"c1|n1|{A}"}]}
    labels = [{"item_id": "R001", "human_decision": "related"}]
    sheet, reading = write_revision(key, labels, snapshot, tmp_path)
    rows = read_sheet(sheet.read_bytes())
    assert rows["R001"]["relacao_anterior"] == "related" and rows["R001"]["relacao"] == ""
    text = reading.read_text(encoding="utf-8")
    assert "R001 — antes: `related`" in text and "tema em comum não basta" in text
