"""Offline replay: score recorded trajectories without touching an LLM.

`python -m zettel.evals.replay evals/configs/<name>.yaml`

The config names a fixture directory (gold questions + recorded trajectories)
and a condition. Nothing here opens a network socket, so it is safe in CI and
its output is byte-stable apart from the fields that are meant to vary.

`trajectory_from_ask_result` is the bridge to a future live runner: it converts
what `run_ask` already returns into the same record the replay scores, using
only the public `AskResult` surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from .manifest import build_manifest, hash_files, hash_json, manifest_identity
from .score import GoldQuestion, RunScore, Trajectory, score_run

RESULT_SCHEMA_VERSION = 1


def load_gold(path: Path) -> list[GoldQuestion]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [GoldQuestion.from_dict(item) for item in payload]


def load_trajectories(path: Path) -> list[Trajectory]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Trajectory.from_dict(item) for item in payload]


def trajectory_from_ask_result(question_id: str, result: Any) -> Trajectory:
    """Convert an `AskResult` into a recordable trajectory (public surface only)."""
    return Trajectory(
        question_id=question_id,
        question=result.question,
        hit_ids=[s.note_id for s in result.sources],
        candidate_ids=[s.note_id for s in result.candidates],
        floor_reasons={s.note_id: s.floor_reason for s in result.candidates},
        passed_floor={s.note_id: s.passed_floor for s in result.candidates},
        llm_called=bool(result.llm_called),
        answer=result.answer,
    )


def replay(config_path: Path) -> dict:
    """Score one recorded run. Returns the JSON-serialisable result document."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    fixture_dir = (config_path.parent / config["fixture_dir"]).resolve()
    gold_path = fixture_dir / config.get("gold_file", "questions.json")
    traj_path = fixture_dir / config.get("trajectories_file", "trajectories.json")

    gold = load_gold(gold_path)
    trajectories = load_trajectories(traj_path)
    score: RunScore = score_run(gold, trajectories)

    manifest = build_manifest(
        condition=config["condition"],
        fixture_hash=hash_files(sorted(fixture_dir.glob("*.json"))),
        question_set_hash=hash_json([g.question_id for g in gold]),
        llm_model=config["llm_model"],
        embedding_model=config["embedding_model"],
        retrieval_params=config.get("retrieval_params") or {},
        commit_sha=config.get("commit_sha", ""),
        max_calls=int(config.get("max_calls") or 0),
        max_input_tokens=int(config.get("max_input_tokens") or 0),
    )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "runner": "replay",
        "manifest": manifest.as_dict(),
        "manifest_identity": manifest_identity(manifest),
        **score.as_dict(),
    }


def render(result: dict) -> str:
    """Stable JSON: sorted keys, no timestamps, so two runs diff to nothing."""
    return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m zettel.evals.replay",
        description="Score recorded ask trajectories offline (no LLM, no network).",
    )
    parser.add_argument("config", type=Path, help="YAML in evals/configs/")
    parser.add_argument("--out", type=Path, default=None, help="Write the JSON here")
    args = parser.parse_args(argv)

    result = replay(args.config)
    text = render(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
