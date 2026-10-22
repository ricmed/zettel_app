"""The map from prompts and config keys to the measurement that covers them (#333)."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from zettel.evals.measurement_map import (
    COMMENT_MARKER,
    MAP_PATH,
    changed_config_keys,
    covering,
    flatten,
    load_map,
    main,
    render,
)
from zettel.llm import REQUIRED_PROMPTS

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
MAP = load_map()


def _names(paths: list[str], keys: list[str]) -> list[str]:
    hits, _ = covering(MAP, paths, keys)
    return [hit.measurement.name for hit in hits]


# -- completeness ----------------------------------------------------------


@pytest.mark.parametrize("prompt", REQUIRED_PROMPTS)
def test_every_prompt_is_measured_or_says_why_not(prompt):
    path = f"prompts/{prompt}"
    hits, unmeasured = covering(MAP, [path], [])
    assert hits or unmeasured, f"{path} sem entrada em evals/measurement_map.yaml"
    assert not (hits and unmeasured), f"{path} medido e listado como sem medicao"


def test_every_path_pattern_matches_a_file():
    files = [p.relative_to(REPO_ROOT).as_posix() for p in REPO_ROOT.rglob("*") if p.is_file()]
    patterns = [p for m in MAP.measurements for p in m.paths]
    patterns += [p for u in MAP.unmeasured for p in u.paths]
    for pattern in patterns:
        assert fnmatch.filter(files, pattern), f"padrao sem arquivo: {pattern}"


def test_every_config_pattern_matches_a_key_of_config_yaml():
    """A renamed key must break the map, not silently stop matching."""
    keys = list(flatten(CONFIG))
    for measurement in MAP.measurements:
        for pattern in measurement.config:
            assert fnmatch.filter(keys, pattern), f"{measurement.name}: chave sem par: {pattern}"


def test_every_preregistration_and_script_exists():
    for measurement in MAP.measurements:
        for path in measurement.preregistration:
            assert (REPO_ROOT / path).is_file(), path
        for command in measurement.commands:
            for script in re.findall(r"scripts/[\w.-]+\.py", command):
                assert (REPO_ROOT / script).is_file(), f"{measurement.name}: {script}"
        assert measurement.commands or measurement.note, measurement.name


# -- what a change maps to -------------------------------------------------


def test_the_dedupe_prompt_points_at_its_probe():
    text = render(*covering(MAP, ["prompts/dedupe_decision.md"], []))
    assert text.startswith(COMMENT_MARKER)
    assert "probe_dedupe_context.py" in text
    assert "218-prompt-dedupe.md" in text


def test_a_relevance_floor_points_at_the_floor_probe():
    key = "retrieval.relevance_floor.min_vector_similarity"
    text = render(*covering(MAP, [], [key]))
    assert f"`config: {key}`" in text
    assert "probe_relevance_floor.py" in text


def test_a_key_is_covered_by_every_measurement_that_reads_it():
    assert _names([], ["retrieval.relevance_floor.bm25_bypass_max_rank"]) == [
        "relevance-floor",
        "bm25-bypass",
    ]
    assert set(_names([], ["embedding.model"])) == {"relevance-floor", "corroborates"}


def test_an_unmeasured_prompt_is_listed_with_its_reason():
    text = render(*covering(MAP, ["prompts/article_judge.md"], []))
    assert "Sem medição associada" in text
    assert "`prompts/article_judge.md`" in text


def test_a_change_outside_the_map_renders_nothing():
    assert render(*covering(MAP, ["zettel/web/server.py", "README.md"], ["web.port"])) == ""


# -- config diff -----------------------------------------------------------


def test_changed_config_keys_sees_changed_added_and_removed_leaves():
    old = {"a": {"b": 1, "c": [1, 2], "gone": True}, "same": "x"}
    new = {"a": {"b": 2, "c": [1, 2, 3], "new": 0}, "same": "x"}
    assert changed_config_keys(old, new) == ["a.b", "a.c", "a.gone", "a.new"]
    assert changed_config_keys(None, {"k": 1}) == ["k"]
    assert changed_config_keys(old, old) == []


# -- CLI over a real git history -------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_cli_reads_the_branch_diff_and_the_config_keys(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "prompts").mkdir()
    config = {"retrieval": {"relevance_floor": {"min_vector_similarity": 0.4}}, "web": {"p": 1}}
    (repo / "config" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (repo / "prompts" / "dedupe_decision.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    config["retrieval"]["relevance_floor"]["min_vector_similarity"] = 0.45
    (repo / "config" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (repo / "prompts" / "dedupe_decision.md").write_text("v2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "change")

    monkeypatch.chdir(repo)
    out = tmp_path / "comment.md"
    assert main(["--base", "main", "--map", str(MAP_PATH), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "`prompts/dedupe_decision.md`" in text
    assert "`config: retrieval.relevance_floor.min_vector_similarity`" in text

    _git(repo, "checkout", "-q", "main")
    assert main(["--base", "main", "--map", str(MAP_PATH), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == ""
