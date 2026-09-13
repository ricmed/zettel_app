"""Tests for the extraction gold-set scorer (issue #175).

The scorer compares a human's verdict with the LLM's. The properties pinned here are the
ones that would otherwise corrupt the result without raising anything: a stratified
sample read as if it were the corpus, a spreadsheet's re-encoding read as garbage, and a
`?` quietly counted as agreement.
"""

import json

import pytest
from zettel.evals.extraction import (
    DISCARD,
    KEEP,
    STRATUM_ACCEPTED,
    STRATUM_CONTESTED,
    STRATUM_STRUCTURAL,
    UNJUDGEABLE,
    HumanLabel,
    KeyItem,
    decode_sheet,
    load_key,
    read_sheet,
    sampling_stratum,
    score,
    wilson,
)

HEADER = ["item_id", "veredito", "categoria", "nota", "fonte", "locator", "texto"]


def _sheet(tmp_path, rows, *, delimiter=",", encoding="utf-8"):
    lines = [delimiter.join(HEADER)]
    for r in rows:
        lines.append(delimiter.join(r))
    path = tmp_path / "planilha.csv"
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode(encoding))
    return path


def _key(item_id, verdict, category="", source="@S", drawn_from=None):
    stratum = drawn_from or sampling_stratum(verdict, category)
    return KeyItem(item_id, f"c-{item_id}", source, verdict, category, stratum)


# -- strata --------------------------------------------------------------


def test_sampling_stratum_matches_the_export_design():
    assert sampling_stratum("accepted", "") == STRATUM_ACCEPTED
    assert sampling_stratum("rejected", "structural") == STRATUM_STRUCTURAL
    assert sampling_stratum("rejected", "narrative") == STRATUM_CONTESTED
    assert sampling_stratum("rejected", "") == STRATUM_CONTESTED


# -- reading -------------------------------------------------------------


def test_reads_the_utf8_comma_sheet_as_exported(tmp_path):
    path = _sheet(tmp_path, [["G001", "s", "", "", "@A", "p.1", "texto"]])
    labels, report = read_sheet(path)
    assert report.encoding == "utf-8-sig"
    assert report.delimiter == ","
    assert labels[0].verdict == KEEP


def test_reads_a_spreadsheet_resaved_sheet(tmp_path):
    """pt-BR spreadsheet: semicolons and the cp850 code page. Accents must survive."""
    rows = [["G001", "n", "fragmented", "a extração não converteu", "@A", "p.1", "matemática"]]
    path = _sheet(tmp_path, rows, delimiter=";", encoding="cp850")
    labels, report = read_sheet(path)
    assert report.delimiter == ";"
    assert report.encoding == "cp850"
    assert labels[0].note == "a extração não converteu"


def test_decode_prefers_the_code_page_that_reads_as_portuguese():
    raw = "extração não matemática código".encode("cp850")
    text, encoding = decode_sheet(raw)
    assert encoding == "cp850"
    assert "extração" in text


def test_verdict_aliases_are_normalised_and_reported(tmp_path):
    rows = [
        ["G001", "y", "", "", "@A", "", ""],
        ["G002", "Sim", "", "", "@A", "", ""],
        ["G003", "N", "", "", "@A", "", ""],
        ["G004", "?", "", "", "@A", "", ""],
    ]
    labels, report = read_sheet(_sheet(tmp_path, rows))
    assert [lab.verdict for lab in labels] == [KEEP, KEEP, DISCARD, UNJUDGEABLE]
    assert report.normalized == {"sim": 1, "y": 1}  # canonical s/n/? are not "normalised"


def test_missing_and_invalid_verdicts_are_reported_not_scored(tmp_path):
    rows = [
        ["G001", "", "", "", "@A", "", ""],
        ["G002", "talvez", "", "", "@A", "", ""],
        ["G003", "s", "", "", "@A", "", ""],
    ]
    labels, report = read_sheet(_sheet(tmp_path, rows))
    assert [lab.item_id for lab in labels] == ["G003"]
    assert report.missing == ["G001"]
    assert report.invalid == {"G002": "talvez"}


def test_key_without_sampling_stratum_is_refused(tmp_path):
    path = tmp_path / "gabarito.json"
    item = {"item_id": "G1", "chunk_id": "c", "source_id": "@A", "llm_verdict": "accepted"}
    path.write_text(json.dumps({"population": {"accepted": 1}, "items": [item]}), encoding="utf-8")
    with pytest.raises(ValueError, match="sampling_stratum"):
        load_key(path)


def test_key_without_population_is_refused(tmp_path):
    path = tmp_path / "gabarito.json"
    path.write_text(json.dumps({"items": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="population"):
        load_key(path)


# -- scoring -------------------------------------------------------------


def test_confusion_matrix_positive_class_is_keep():
    key = [
        _key("G1", "accepted"),  # human keep  -> tp
        _key("G2", "accepted"),  # human discard -> fp
        _key("G3", "rejected", "narrative"),  # human keep -> fn (silent loss)
        _key("G4", "rejected", "narrative"),  # human discard -> tn
    ]
    labels = [
        HumanLabel("G1", KEEP),
        HumanLabel("G2", DISCARD),
        HumanLabel("G3", KEEP),
        HumanLabel("G4", DISCARD),
    ]
    pop = {STRATUM_ACCEPTED: 2, STRATUM_CONTESTED: 2}
    result = score(labels, key, pop)
    assert result.raw_confusion == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert result.precision == 0.5
    assert result.recall == 0.5
    assert result.rejected_but_keep_rate == 0.5


def test_unjudgeable_is_excluded_not_counted_as_agreement():
    key = [_key("G1", "rejected", "narrative"), _key("G2", "rejected", "narrative")]
    labels = [HumanLabel("G1", UNJUDGEABLE), HumanLabel("G2", KEEP)]
    result = score(labels, key, {STRATUM_CONTESTED: 2})
    assert result.items_unjudgeable == 1
    assert result.items_scored == 1
    assert sum(result.raw_confusion.values()) == 1
    assert result.rejected_but_keep_rate == 1.0


def test_weighting_changes_the_corpus_estimate():
    """Over-sampled strata must not dominate: that is the whole point of the weights.

    Contested: census of 10, human keeps 5 of them (50%).
    Structural: 10 sampled from 1000, human keeps none (0%).
    Unweighted, rejected_but_keep would be 5/20 = 25%. Weighted by population it is
    5 / (10 + 1000) ~= 0.5%, because structural is 100x the size.
    """
    key = [_key(f"C{i}", "rejected", "narrative") for i in range(10)]
    key += [_key(f"S{i}", "rejected", "structural") for i in range(10)]
    labels = [HumanLabel(f"C{i}", KEEP if i < 5 else DISCARD) for i in range(10)]
    labels += [HumanLabel(f"S{i}", DISCARD) for i in range(10)]
    pop = {STRATUM_CONTESTED: 10, STRATUM_STRUCTURAL: 1000}

    result = score(labels, key, pop)
    assert result.raw_confusion["fn"] == 5 and result.raw_confusion["tn"] == 15
    assert result.rejected_but_keep_rate == pytest.approx(5 / 1010, abs=1e-4)
    assert result.rejected_but_keep_rate < 0.25


def test_weights_follow_the_stratum_drawn_from_not_the_current_verdict():
    """A re-extracted key changes verdicts; it must not change inclusion weights.

    C0 was drawn from the contested census (weight 1) and the new prompt now accepts
    it. S0..S9 were drawn from structural (weight 100) and stay rejected. If the
    stratum were re-derived from the new verdict, C0 would be weighed as an
    accepted-stratum item and the rejected-side estimate would be computed over the
    wrong population.
    """
    key = [_key("C0", "accepted", drawn_from=STRATUM_CONTESTED)]
    key += [_key(f"S{i}", "rejected", "structural") for i in range(10)]
    labels = [HumanLabel("C0", KEEP)] + [HumanLabel(f"S{i}", KEEP) for i in range(10)]
    pop = {STRATUM_CONTESTED: 1, STRATUM_STRUCTURAL: 1000}

    result = score(labels, key, pop)
    strata = {s.stratum: s for s in result.strata}
    assert STRATUM_ACCEPTED not in strata  # nothing was drawn from the accepted stratum
    assert strata[STRATUM_CONTESTED].sampled == 1
    # C0 is now a true positive; the structural ones are false negatives at weight 100.
    assert result.raw_confusion == {"tp": 1, "fp": 0, "fn": 10, "tn": 0}
    assert result.recall == pytest.approx(1 / (1 + 1000), abs=1e-4)


def test_stratum_disagreement_follows_the_current_verdict():
    """Drawn from a rejected stratum, now accepted: agreeing with a human keep is not a miss."""
    key = [
        _key("C0", "accepted", drawn_from=STRATUM_CONTESTED),  # human keeps -> agrees
        _key("C1", "rejected", "narrative"),  # human keeps -> disagrees
        _key("C2", "accepted", drawn_from=STRATUM_CONTESTED),  # human discards -> disagrees
    ]
    labels = [HumanLabel("C0", KEEP), HumanLabel("C1", KEEP), HumanLabel("C2", DISCARD)]
    (stratum,) = score(labels, key, {STRATUM_CONTESTED: 3}).strata
    assert stratum.human_keep == 2
    assert stratum.disagreement_rate == pytest.approx(2 / 3, abs=1e-4)


def test_census_stratum_reports_no_sampling_error():
    key = [_key(f"C{i}", "rejected", "narrative") for i in range(4)]
    labels = [HumanLabel(f"C{i}", KEEP if i == 0 else DISCARD) for i in range(4)]
    result = score(labels, key, {STRATUM_CONTESTED: 4})
    (stratum,) = result.strata
    assert stratum.census is True
    assert stratum.disagreement_ci95 == (0.25, 0.25)


def test_sampled_stratum_carries_a_wilson_interval():
    key = [_key(f"S{i}", "rejected", "structural") for i in range(10)]
    labels = [HumanLabel(f"S{i}", KEEP if i < 2 else DISCARD) for i in range(10)]
    result = score(labels, key, {STRATUM_STRUCTURAL: 100})
    (stratum,) = result.strata
    assert stratum.census is False
    low, high = stratum.disagreement_ci95
    assert low < 0.2 < high


def test_category_agreement_only_compares_named_rejections():
    key = [
        _key("G1", "rejected", "structural"),
        _key("G2", "rejected", "structural"),
        _key("G3", "rejected", "narrative"),  # human leaves category blank
        _key("G4", "accepted"),  # human discards with a category, LLM accepted
    ]
    labels = [
        HumanLabel("G1", DISCARD, "structural"),
        HumanLabel("G2", DISCARD, "fragmented"),
        HumanLabel("G3", DISCARD, ""),
        HumanLabel("G4", DISCARD, "trivial"),
    ]
    pop = {STRATUM_STRUCTURAL: 2, STRATUM_CONTESTED: 1, STRATUM_ACCEPTED: 1}
    agreement = score(labels, key, pop).category_agreement
    assert agreement["compared"] == 2
    assert agreement["agree"] == 1
    assert agreement["llm_to_human"] == {"structural": {"fragmented": 1, "structural": 1}}


def test_disagreements_list_carries_notes_and_no_text():
    key = [_key("G1", "rejected", "fragmented"), _key("G2", "accepted")]
    labels = [HumanLabel("G1", KEEP, note="math mal convertida"), HumanLabel("G2", KEEP)]
    rows = score(labels, key, {STRATUM_CONTESTED: 1, STRATUM_ACCEPTED: 1}).disagreements
    assert [r["item_id"] for r in rows] == ["G1"]
    assert rows[0]["human_note"] == "math mal convertida"
    assert "texto" not in rows[0] and "text" not in rows[0]


def test_wilson_is_bounded_and_empty_safe():
    assert wilson(0, 0) == (0.0, 0.0)
    low, high = wilson(0, 30)
    assert low == 0.0 and 0.0 < high < 0.15
    low, high = wilson(30, 30)
    assert 0.85 < low < 1.0 and high == 1.0


# -- isolation (ADR-038) -------------------------------------------------


def test_scoring_opens_no_socket(tmp_path, monkeypatch):
    import socket

    def _boom(*_a, **_k):
        raise AssertionError("o scorer nao pode abrir rede")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    sheet = _sheet(tmp_path, [["G1", "s", "", "", "@A", "", ""]])
    key_path = tmp_path / "gabarito.json"
    key_path.write_text(
        json.dumps(
            {
                "population": {"accepted": 1},
                "items": [
                    {
                        "item_id": "G1",
                        "chunk_id": "c",
                        "source_id": "@A",
                        "llm_verdict": "accepted",
                        "sampling_stratum": "accepted",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    from zettel.evals.extraction import main

    assert main([str(sheet), str(key_path)]) == 0
