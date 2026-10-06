"""Tests for the blind decision-gold exporter (#206). No model, no network."""

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from export_decision_gold import (
    FORBIDDEN_IN_SHEET,
    build_key,
    eligible,
    main,
    population,
    sample,
    stratum_of,
    write_reading,
    write_sheet,
)
from zettel.state import StateDB

CATEGORIES = [("Pilar", "Cat A", ["t1"]), ("Pilar", "Cat B", [])]


def _dedupe_row(subject, decision, *, jev="ignore", error=None, human=None):
    return {
        "site": "dedupe",
        "subject_id": f"@Src::concept::{subject}",
        "state_checksum": f"s{subject}",
        "model": "typesafe/jev-test",
        "state": {
            "candidate": {"thesis": f"Tese {subject}", "definition": "Def"},
            "existing_notes": [
                {"id": "01NOTEA", "title": "Nota A", "text": "Texto A"},
                {"id": "01NOTEB", "title": "Nota B", "text": "Texto B"},
            ],
        },
        "baseline": {"decision": decision, "llm_decision": decision, "target": "01NOTEA"},
        "jev": None
        if error
        else {
            "decision": {"choice": jev, "confidence": 0.91, "probabilities": {}},
            "target": {"choice": "01NOTEA", "confidence": 0.8, "probabilities": {}},
        },
        "human": human,
        "error": error,
    }


def _category_row(subject, baseline, jev_choice):
    return {
        "site": "moc_category",
        "subject_id": subject,
        "state_checksum": f"s{subject}",
        "model": "typesafe/jev-test",
        "state": {"notes": [{"title": "N1", "text": "corpo"}], "frequent_terms": ["x", "y"]},
        "baseline": {"category": baseline},
        "jev": {"category": {"choice": jev_choice, "confidence": 0.7, "probabilities": {}}},
        "human": None,
        "error": None,
    }


def test_eligible_drops_errors_and_other_sites():
    rows = [_dedupe_row("1", "ignore"), _dedupe_row("2", "ignore", error="429")]
    rows.append(_category_row("c", "Cat A", "Cat A"))
    assert [r["subject_id"] for r in eligible(rows, "dedupe")] == ["@Src::concept::1"]


def test_strata():
    assert stratum_of(_dedupe_row("1", "link")) == "link"
    assert stratum_of(_category_row("c", "Cat A", "Cat A")) == "agree"
    assert stratum_of(_category_row("c", "Cat A", "Cat B")) == "disagree"
    assert stratum_of(_category_row("c", "_unassigned", "Cat B")) == "unassigned"


def test_sample_is_census_up_to_the_cap_and_deterministic():
    rows = [_dedupe_row(str(i), "create_new") for i in range(10)] + [_dedupe_row("x", "ignore")]
    items = sample(rows, per_stratum=4, seed=1)
    strata = [i.stratum for i in items]
    assert strata.count("create_new") == 4
    assert strata.count("ignore") == 1
    again = sample(rows, per_stratum=4, seed=1)
    assert [i.row["subject_id"] for i in items] == [i.row["subject_id"] for i in again]
    assert [i.item_id for i in items] == ["D001", "D002", "D003", "D004", "D005"]
    assert population(rows) == {"create_new": 10, "ignore": 1}


def test_dedupe_notes_are_shown_by_letter_and_the_key_maps_them_back(tmp_path):
    items = sample([_dedupe_row("1", "ignore")], per_stratum=5, seed=0)
    assert items[0].letters == {"A": "01NOTEA", "B": "01NOTEB"}
    key = build_key(items, "dedupe", seed=0, population_counts={"ignore": 1}, categories=[])
    assert key["items"][0]["letters"] == {"A": "01NOTEA", "B": "01NOTEB"}
    assert key["answers"]["decisao"]["repete"] == "ignore"


def _sheet_and_reading(tmp_path, rows, site, categories=()):
    items = sample(rows, per_stratum=5, seed=0)
    sheet, reading = tmp_path / "planilha.csv", tmp_path / "leitura.md"
    write_sheet(items, site, sheet)
    write_reading(items, site, reading, list(categories))
    return sheet.read_text(encoding="utf-8-sig"), reading.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("rows", "site"),
    [
        (
            [
                _dedupe_row("1", "ignore", human={"verdict": "ignore"}),
                _dedupe_row("2", "create_new"),
            ],
            "dedupe",
        ),
        (
            [_category_row("c", "_unassigned", "none"), _category_row("d", "Cat A", "Cat B")],
            "moc_category",
        ),
    ],
)
def test_sheet_and_reading_are_blind(tmp_path, rows, site):
    sheet, reading = _sheet_and_reading(tmp_path, rows, site, CATEGORIES)
    for text in (sheet, reading):
        lowered = text.lower()
        for word in FORBIDDEN_IN_SHEET:
            assert word not in lowered, word
        assert "01NOTEA" not in text  # ids hidden behind letters


def test_dedupe_sheet_is_semicolon_utf8_with_answer_columns_empty(tmp_path):
    sheet, reading = _sheet_and_reading(tmp_path, [_dedupe_row("1", "ignore")], "dedupe")
    (header, row) = list(csv.reader(sheet.splitlines(keepends=True), delimiter=";"))[:2]
    assert header[:4] == ["item_id", "decisao", "alvo", "nota"]
    assert row[1:4] == ["", "", ""]
    assert "A) Nota A: Texto A" in row[-1]
    assert "`repete`" in reading


def test_category_reading_numbers_the_taxonomy(tmp_path):
    _, reading = _sheet_and_reading(
        tmp_path, [_category_row("c", "Cat A", "Cat A")], "moc_category", CATEGORIES
    )
    assert "1. **Cat A** (Pilar) — t1" in reading
    assert "2. **Cat B** (Pilar)" in reading


def test_category_key_holds_the_numbering():
    items = sample([_category_row("c", "Cat A", "Cat B")], per_stratum=5, seed=0)
    key = build_key(items, "moc_category", seed=0, population_counts={}, categories=CATEGORIES)
    assert key["categories"] == {"1": "Cat A", "2": "Cat B"}
    assert key["items"][0]["jev"]["category"] == "Cat B"
    assert key["items"][0]["baseline"] == {"category": "Cat A"}


def _db_with_one_dedupe_row(path):
    db = StateDB(path)
    row = _dedupe_row("1", "ignore")
    db.record_decision_shadow(
        site="dedupe",
        subject_id=row["subject_id"],
        state_checksum=row["state_checksum"],
        model=row["model"],
        state=row["state"],
        baseline=row["baseline"],
        jev=row["jev"],
        latency_ms=5,
        input_tokens=10,
    )
    db.close()


def test_main_writes_three_files_and_refuses_to_overwrite_labels(tmp_path):
    db_path = tmp_path / "state.db"
    _db_with_one_dedupe_row(db_path)
    out = tmp_path / "gold"
    args = ["--site", "dedupe", "--state-db", str(db_path), "--out-dir", str(out)]
    assert main(args) == 0
    assert {p.name for p in out.iterdir()} == {
        "dedupe-planilha.csv",
        "dedupe-leitura.md",
        "dedupe-GABARITO-NAO-ABRIR.json",
    }
    key = json.loads((out / "dedupe-GABARITO-NAO-ABRIR.json").read_text(encoding="utf-8"))
    assert "Tese" not in json.dumps(key)  # the key carries ids, not text
    assert main(args) == 1
    assert main([*args, "--force"]) == 0


def test_main_without_shadow_rows_explains_what_to_run(tmp_path, capsys):
    db_path = tmp_path / "state.db"
    StateDB(db_path).close()
    code = main(["--site", "moc_category", "--state-db", str(db_path), "--out-dir", str(tmp_path)])
    assert code == 1
    assert "zettel garden" in capsys.readouterr().out


def _corr_row(pair, order, similarity, score=1.0):
    return {
        "site": "corroborates",
        "subject_id": f"{pair}:{order}",
        "state_checksum": f"s{pair}{order}",
        "model": "typesafe/jev-test",
        "state": {
            "note_a": {"thesis": f"Tese A {order}", "definition": "Def A"},
            "note_b": {"thesis": f"Tese B {order}", "definition": "Def B"},
        },
        "baseline": {"similarity": similarity, "threshold": 0.85, "edge": similarity >= 0.85},
        "jev": {
            "same_idea": {"score": score, "confidence": 0.8},
            "converge": {"noul": 0.6},
        },
        "human": None,
        "error": None,
    }


def test_corroborates_pairs_become_one_item_with_both_orders_in_the_key():
    rows = [_corr_row("x|y", "ab", 0.9, 2.0), _corr_row("x|y", "ba", 0.9, 1.0)]
    pairs = eligible(rows, "corroborates")
    assert len(pairs) == 1
    items = sample(pairs, per_stratum=5, seed=0)
    assert items[0].item_id == "P001"
    assert items[0].stratum == "above_threshold"
    key = build_key(items, "corroborates", seed=0, population_counts={}, categories=[])
    assert key["items"][0]["jev"] == {"ab_score": 2.0, "ba_score": 1.0, "ab_converge": 0.6}
    assert key["answers"]["decisao"]["mesma-ideia"] == 2


def test_corroborates_sheet_is_blind(tmp_path):
    rows = eligible([_corr_row("x|y", "ab", 0.9), _corr_row("x|y", "ba", 0.9)], "corroborates")
    sheet, reading = _sheet_and_reading(tmp_path, rows, "corroborates")
    for text in (sheet, reading):
        for word in FORBIDDEN_IN_SHEET:
            assert word not in text.lower(), word
        assert "0.9" not in text
    assert "Tese A ab" in sheet and "`mesma-ideia`" in reading
