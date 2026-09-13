"""Tests for the LLM-as-reader signal probe (issue #176).

Nothing here calls an LLM. The judge is injected, and the replay test uses one that
raises if invoked -- a fully recorded run must cost nothing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_reader_signal import (
    DEFAULT_PROMPT,
    ReaderVerdict,
    collect_verdicts,
    parse_verdict,
    recording_key,
    render_candidates,
    score_signal,
)

ROOT = Path(__file__).resolve().parents[1]


# -- rendering -----------------------------------------------------------


def test_render_candidates_omits_empty_fields():
    text = render_candidates(
        [{"thesis": "Uma tese", "definition": "Def", "intuition": "", "limits": None}]
    )
    assert "Tese: Uma tese" in text
    assert "Definicao: Def" in text
    assert "Intuicao" not in text and "Limites" not in text


def test_render_candidates_numbers_multiple_notes():
    text = render_candidates([{"thesis": "A"}, {"thesis": "B"}])
    assert "### Nota 1" in text and "### Nota 2" in text


def test_render_candidates_without_any_note_says_so():
    assert "nenhuma nota" in render_candidates([])


# -- parsing -------------------------------------------------------------


def test_parse_plain_json():
    assert parse_verdict('{"score": 4, "reason": "boa"}') == ReaderVerdict(4, "boa")


def test_parse_fenced_json():
    assert parse_verdict('```json\n{"score": 2, "reason": "raso"}\n```').score == 2


def test_parse_accepts_a_numeric_string():
    assert parse_verdict('{"score": "5", "reason": ""}').score == 5


@pytest.mark.parametrize(
    "text",
    [
        '{"score": 0, "reason": "x"}',
        '{"score": 6, "reason": "x"}',
        '{"reason": "sem score"}',
        '{"score": "alto"}',
        "nao e json",
    ],
)
def test_parse_refuses_to_guess(text):
    with pytest.raises(ValueError):
        parse_verdict(text)


# -- replay --------------------------------------------------------------


def test_fully_recorded_run_never_calls_the_judge():
    def judge(_chunk_id):
        raise AssertionError("uma gravacao completa nao pode gastar chamada")

    recorded = {"c1": {"score": 5, "reason": "a"}, "c2": {"score": 1, "reason": "b"}}
    verdicts, failures = collect_verdicts(["c1", "c2"], recorded, judge, lambda *_: None)
    assert {c: v.score for c, v in verdicts.items()} == {"c1": 5, "c2": 1}
    assert failures == {}


def test_only_missing_chunks_are_called_and_recorded():
    calls, saved = [], {}
    verdicts, _failures = collect_verdicts(
        ["c1", "c2"],
        {"c1": {"score": 3, "reason": ""}},
        lambda c: calls.append(c) or '{"score": 4, "reason": "ok"}',
        lambda c, v: saved.__setitem__(c, v),
    )
    assert calls == ["c2"]
    assert saved == {"c2": ReaderVerdict(4, "ok")}
    assert verdicts["c1"].score == 3 and verdicts["c2"].score == 4


def test_parse_failure_is_reported_and_not_recorded():
    saved = {}
    verdicts, failures = collect_verdicts(
        ["c1"], {}, lambda _c: "lixo", lambda c, v: saved.__setitem__(c, v)
    )
    assert verdicts == {} and saved == {}
    assert "c1" in failures


def test_recording_key_changes_with_prompt_model_or_temperature():
    base = recording_key("prompt", "gpt-4o-mini", "openai", 0.0)
    assert recording_key("prompt2", "gpt-4o-mini", "openai", 0.0) != base
    assert recording_key("prompt", "gpt-4o", "openai", 0.0) != base
    assert recording_key("prompt", "gpt-4o-mini", "openai", 0.2) != base
    assert recording_key("prompt", "gpt-4o-mini", "openai", 0.0) == base


def test_recording_key_survives_a_crlf_checkout(tmp_path):
    """Git on Windows (core.autocrlf) rewrites the prompt with CRLF on checkout.

    If that changed the key, a fresh clone would miss the recording and pay again.
    `load_prompt` reads in text mode, which folds CRLF to LF; this pins it.
    """
    from zettel.llm import load_prompt_parts

    lf = (ROOT / DEFAULT_PROMPT).read_bytes().replace(b"\r\n", b"\n")
    keys = []
    for name, data in (("lf.md", lf), ("crlf.md", lf.replace(b"\n", b"\r\n"))):
        path = tmp_path / name
        path.write_bytes(data)
        template = load_prompt_parts(path).full_template
        keys.append(recording_key(template, "gpt-4o-mini", "openai", 0.0))
    assert keys[0] == keys[1]


# -- scoring -------------------------------------------------------------


def test_score_signal_on_a_separating_judge():
    gold = {f"k{i}": "keep" for i in range(30)} | {f"d{i}": "discard" for i in range(30)}
    verdicts = {c: ReaderVerdict(5 if v == "keep" else 1, "") for c, v in gold.items()}
    result = score_signal(verdicts, gold)
    assert result["verdict"] == "separa"
    assert result["distribution_keep"][5] == 30
    assert result["distribution_discard"][1] == 30


def test_score_signal_ignores_chunks_without_a_verdict():
    gold = {"k": "keep", "d": "discard", "lost": "keep"}
    result = score_signal({"k": ReaderVerdict(4, ""), "d": ReaderVerdict(2, "")}, gold)
    assert result["n_keep"] == 1 and result["n_discard"] == 1


# -- pre-commitment ------------------------------------------------------


def test_reader_prompt_has_the_payload_placeholders():
    from zettel.llm import load_prompt_parts

    parts = load_prompt_parts(ROOT / DEFAULT_PROMPT)
    assert parts.has_split
    for placeholder in ("{source_title}", "{chunk_text}", "{candidates}"):
        assert placeholder in parts.user_template


def test_reader_criteria_come_from_the_existing_extraction_prompt():
    """The judge's criteria must predate the gold set, not be derived from its labels.

    The labeler's notes were visible when this prompt was written. Tying every named
    criterion to `prompts/literature_note.md` -- written before #175 -- is what keeps a
    positive result from being the prompt fitted to the answers.
    """
    extraction = (ROOT / "prompts" / "literature_note.md").read_text(encoding="utf-8")
    reader = (ROOT / DEFAULT_PROMPT).read_text(encoding="utf-8")
    for criterion in ("Densidade", "Atomicidade", "Autonomia"):
        assert criterion in extraction
        assert criterion in reader
