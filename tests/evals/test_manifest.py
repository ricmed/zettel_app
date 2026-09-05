"""Run identity: same envelope in, same identity out; incomplete in, refusal out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from zettel.evals.manifest import (
    FLOOR_KEYS,
    ManifestError,
    build_manifest,
    hash_files,
    hash_json,
    manifest_identity,
)

_BASE = {
    "condition": "current_ask",
    "fixture_hash": "abc123",
    "question_set_hash": "def456",
    "llm_model": "fixture/no-llm",
    "embedding_model": "fixture/no-embedding",
    "commit_sha": "0" * 40,
}


def test_identical_inputs_give_identical_identity():
    a = build_manifest(**_BASE)
    b = build_manifest(**_BASE)
    assert manifest_identity(a) == manifest_identity(b)


def test_a_different_condition_is_a_different_run():
    a = build_manifest(**_BASE)
    b = build_manifest(**{**_BASE, "condition": "no_graph"})
    assert manifest_identity(a) != manifest_identity(b)


def test_a_different_commit_is_a_different_run():
    """Same questions, different code — the comparison must not be assumed away."""
    a = build_manifest(**_BASE)
    b = build_manifest(**{**_BASE, "commit_sha": "1" * 40})
    assert manifest_identity(a) != manifest_identity(b)


@pytest.mark.parametrize("field", list(_BASE.keys() - {"commit_sha"}))
def test_missing_required_field_is_refused(field):
    payload = {**_BASE, field: ""}
    with pytest.raises(ManifestError, match="incompleto"):
        build_manifest(**payload)


def test_only_the_judged_knobs_are_recorded():
    manifest = build_manifest(
        **_BASE,
        retrieval_params={
            "mode": "hybrid",
            "min_vector_similarity": 0.7,
            "irrelevante": "nao deve entrar",
        },
    )
    assert set(manifest.retrieval_params) <= set(FLOOR_KEYS)
    assert "irrelevante" not in manifest.retrieval_params


def test_knob_order_is_fixed_so_the_identity_is_stable():
    a = build_manifest(**_BASE, retrieval_params={"mode": "hybrid", "topk": 8})
    b = build_manifest(**_BASE, retrieval_params={"topk": 8, "mode": "hybrid"})
    assert manifest_identity(a) == manifest_identity(b)


@pytest.mark.parametrize(
    "poison",
    ["sk-abcdef123456", "meu api_key aqui", "Bearer eyJhbGciOi", "ghp_deadbeef"],
)
def test_secret_looking_values_are_refused(poison):
    with pytest.raises(ManifestError, match="segredo"):
        build_manifest(**{**_BASE, "condition": poison})


def test_a_token_budget_is_not_a_secret():
    """`max_input_tokens` is a budget knob — the guard reads values, not field names."""
    manifest = build_manifest(**_BASE, max_calls=50, max_input_tokens=200_000)
    assert manifest.max_input_tokens == 200_000


def test_file_hash_is_content_and_name_addressed(tmp_path: Path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("[1]", encoding="utf-8")
    b.write_text("[2]", encoding="utf-8")
    first = hash_files([a, b])
    assert hash_files([b, a]) == first, "ordem dos arquivos nao pode importar"
    b.write_text("[3]", encoding="utf-8")
    assert hash_files([a, b]) != first


def test_json_hash_ignores_key_order():
    assert hash_json({"a": 1, "b": 2}) == hash_json({"b": 2, "a": 1})


def test_manifest_is_json_serialisable():
    payload = build_manifest(**_BASE).as_dict()
    assert json.loads(json.dumps(payload))["condition"] == "current_ask"


def test_file_hash_ignores_line_endings(tmp_path: Path):
    """Fixtures are committed text; git rewrites CRLF/LF per platform on checkout.

    Without normalisation the same fixture hashes differently on Windows and
    Linux, and the committed baseline in `evals/results/` could never match on
    both — found while rebasing this branch onto a fresh checkout.
    """
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "lf.json.crlf"
    lf.write_bytes(b'[\n  {"a": 1}\n]\n')
    crlf.write_bytes(b'[\r\n  {"a": 1}\r\n]\r\n')
    # Same name, so only the bytes differ.
    renamed = tmp_path / "sub"
    renamed.mkdir()
    twin = renamed / "lf.json"
    twin.write_bytes(crlf.read_bytes())
    assert hash_files([lf]) == hash_files([twin])
