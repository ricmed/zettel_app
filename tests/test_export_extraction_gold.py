"""Tests for the blind labeling sheet exporter (issue #175).

The property that matters most here is negative: the sheet must not tell the labeler
what the model decided. A sheet that leaks the verdict turns the whole exercise from a
measurement into a confirmation, and the leak would be invisible in the resulting
numbers -- so it is pinned by test rather than by care.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from export_extraction_gold import (
    CATEGORY_HINTS,
    FORBIDDEN_IN_SHEET,
    SHEET_COLUMNS,
    VERDICT_VALUES,
    population_by_stratum,
    sample_items,
    text_features,
    write_key,
    write_reading,
    write_sheet,
)


def _rec(chunk_id, verdict, *, category="", source="@Src", text="uma passagem qualquer"):
    return {
        "chunk_id": chunk_id,
        "source_id": source,
        "text": text,
        "section_path": "Cap 1",
        "page_in_book": 10,
        "page_in_file": 12,
        "verdict": verdict,
        "category": category,
    }


def _corpus():
    recs = []
    for i in range(6):
        recs.append(_rec(f"n{i}", "rejected", category="narrative", source="@A"))
    for i in range(4):
        recs.append(_rec(f"f{i}", "rejected", category="fragmented", source="@B"))
    for i in range(20):
        recs.append(
            _rec(f"s{i}", "rejected", category="structural", source="@A" if i % 2 else "@B")
        )
    for i in range(30):
        recs.append(_rec(f"a{i}", "accepted", source="@A" if i % 3 else "@B"))
    return recs


# -- blindness -----------------------------------------------------------


def test_sheet_has_no_column_that_reveals_the_verdict():
    for forbidden in FORBIDDEN_IN_SHEET:
        assert forbidden not in SHEET_COLUMNS


def test_written_sheet_never_contains_the_model_verdict(tmp_path):
    items = sample_items(_corpus(), seed=1, n_structural=5, n_accepted=8)
    sheet = tmp_path / "planilha.csv"
    write_sheet(items, sheet)

    raw = sheet.read_text(encoding="utf-8")
    for leak in ("accepted", "rejected", "structural", "narrative", "fragmented"):
        assert leak not in raw

    with sheet.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(items)
    assert all(row["veredito"] == "" for row in rows)  # nothing pre-filled
    assert {r["item_id"] for r in rows} == {it.item_id for it in items}


def test_export_order_does_not_group_by_verdict():
    """Listing rejections first would leak the verdict through position alone."""
    recs = [_rec(f"r{i}", "rejected", category="narrative") for i in range(20)]
    recs += [_rec(f"a{i}", "accepted") for i in range(20)]
    items = sample_items(recs, seed=3, n_structural=0, n_accepted=20)
    verdicts = [it.hidden_verdict for it in items]
    first_half = verdicts[: len(verdicts) // 2]
    assert len(set(first_half)) == 2  # both kinds appear early


def test_key_file_keeps_the_join(tmp_path):
    import json

    items = sample_items(_corpus(), seed=1, n_structural=5, n_accepted=8)
    key = tmp_path / "gabarito.json"
    write_key(items, key, seed=1, population={"accepted": 30, "contested": 10, "structural": 20})
    payload = json.loads(key.read_text(encoding="utf-8"))

    assert payload["seed"] == 1
    assert payload["n_items"] == len(items)
    assert payload["population"] == {"accepted": 30, "contested": 10, "structural": 20}
    by_item = {e["item_id"]: e for e in payload["items"]}
    for it in items:
        assert by_item[it.item_id]["chunk_id"] == it.chunk_id
        assert by_item[it.item_id]["llm_verdict"] == it.hidden_verdict


# -- sampling ------------------------------------------------------------


def test_contested_rejections_are_a_census():
    """Every non-structural rejection is included -- that stratum is not sampled."""
    items = sample_items(_corpus(), seed=0, n_structural=5, n_accepted=5)
    contested = [
        it for it in items if it.hidden_verdict == "rejected" and it.hidden_category != "structural"
    ]
    assert len(contested) == 10  # 6 narrative + 4 fragmented


def test_structural_and_accepted_honour_their_sizes():
    items = sample_items(_corpus(), seed=0, n_structural=7, n_accepted=9)
    structural = [it for it in items if it.hidden_category == "structural"]
    accepted = [it for it in items if it.hidden_verdict == "accepted"]
    assert len(structural) == 7
    assert len(accepted) == 9


def test_sampling_is_deterministic_under_seed():
    a = sample_items(_corpus(), seed=42, n_structural=6, n_accepted=6)
    b = sample_items(_corpus(), seed=42, n_structural=6, n_accepted=6)
    assert [(it.item_id, it.chunk_id) for it in a] == [(it.item_id, it.chunk_id) for it in b]


def test_different_seeds_draw_differently():
    a = sample_items(_corpus(), seed=1, n_structural=6, n_accepted=6)
    b = sample_items(_corpus(), seed=2, n_structural=6, n_accepted=6)
    assert {it.chunk_id for it in a} != {it.chunk_id for it in b}


def test_sample_spreads_across_sources():
    """One dominant document must not supply the whole sampled stratum."""
    recs = [_rec(f"s{i}", "rejected", category="structural", source="@Big") for i in range(90)]
    recs += [_rec(f"t{i}", "rejected", category="structural", source="@Small") for i in range(10)]
    items = sample_items(recs, seed=0, n_structural=20, n_accepted=0)
    sources = {it.source_id for it in items}
    assert sources == {"@Big", "@Small"}


def test_requesting_more_than_available_returns_all():
    items = sample_items(_corpus(), seed=0, n_structural=999, n_accepted=999)
    assert len([it for it in items if it.hidden_category == "structural"]) == 20
    assert len([it for it in items if it.hidden_verdict == "accepted"]) == 30


def test_item_ids_are_sequential_and_unique():
    items = sample_items(_corpus(), seed=5, n_structural=5, n_accepted=5)
    assert [it.item_id for it in items] == [f"G{i:03d}" for i in range(1, len(items) + 1)]


# -- features ------------------------------------------------------------


def test_text_features_flag_a_table():
    table = "\n".join("| a | b | c |" for _ in range(10))
    table_ratio, _ = text_features(table)
    assert table_ratio == 1.0


def test_text_features_on_prose():
    table_ratio, alnum = text_features("Uma frase comum em portugues, com pontuacao.")
    assert table_ratio == 0.0
    assert alnum > 0.7


def test_text_features_empty_is_safe():
    assert text_features("   ") == (0.0, 0.0)


# -- labeling instructions -----------------------------------------------


def test_reading_companion_documents_every_verdict_value(tmp_path):
    """The instructions and the constant must not drift apart."""
    items = sample_items(_corpus(), seed=1, n_structural=3, n_accepted=3)
    reading = tmp_path / "leitura.md"
    write_reading(items, reading)
    header = reading.read_text(encoding="utf-8").split("## G", 1)[0]

    for value in VERDICT_VALUES:
        assert f"`{value}`" in header
    for hint in CATEGORY_HINTS:
        assert hint in header


def test_unjudgeable_is_offered_as_an_escape_not_a_default(tmp_path):
    """`?` must be documented as allowed, and no cell may come pre-filled with it."""
    items = sample_items(_corpus(), seed=1, n_structural=3, n_accepted=3)
    sheet = tmp_path / "planilha.csv"
    write_sheet(items, sheet)

    with sheet.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert all(row["veredito"] == "" for row in rows)
    assert all(row["categoria"] == "" and row["nota"] == "" for row in rows)


def test_reading_companion_still_hides_the_verdict(tmp_path):
    items = sample_items(_corpus(), seed=2, n_structural=4, n_accepted=4)
    reading = tmp_path / "leitura.md"
    write_reading(items, reading)
    body = reading.read_text(encoding="utf-8").split("## G", 1)[1]

    for leak in ("accepted", "rejected", "chunk_status", "review_confidence"):
        assert leak not in body


def test_population_uses_the_scorers_stratum_definition():
    pop = population_by_stratum(_corpus())
    assert pop == {"contested": 10, "structural": 20, "accepted": 30}
