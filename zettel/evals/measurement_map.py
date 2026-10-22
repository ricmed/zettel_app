"""Which measurement covers a change to a prompt, a threshold or a model (#333).

`python -m zettel.evals.measurement_map --base origin/main [--out comment.md]`

A change to `prompts/dedupe_decision.md` or to a floor in `config/config.yaml` passes
CI as soon as the deterministic tests pass; what says whether it made the pipeline
better is a measurement against a gold set, run by hand with a budget and judged by a
pre-registered rule. `evals/measurement_map.yaml` records which measurement covers
which file and which config key. This module reads the files a branch changed (`git
diff` against the merge base) and the config keys whose value changed (both YAML
versions parsed and flattened to dotted keys), and renders the covering measurements
as Markdown, which the CI posts on the pull request. It runs nothing and spends
nothing; an empty output means the change touches no measured surface.
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = REPO_ROOT / "evals" / "measurement_map.yaml"
CONFIG_PATH = "config/config.yaml"
COMMENT_MARKER = "<!-- zettel:measurement-map -->"


@dataclass(frozen=True)
class Measurement:
    name: str
    title: str
    paths: tuple[str, ...]
    config: tuple[str, ...]
    preregistration: tuple[str, ...]
    commands: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class Unmeasured:
    paths: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class MeasurementMap:
    measurements: tuple[Measurement, ...]
    unmeasured: tuple[Unmeasured, ...]


@dataclass(frozen=True)
class Hit:
    """A measurement and the changed paths and keys it covers."""

    measurement: Measurement
    paths: tuple[str, ...]
    keys: tuple[str, ...]


def load_map(path: Path = MAP_PATH) -> MeasurementMap:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    measurements = tuple(
        Measurement(
            name=name,
            title=entry["title"],
            paths=tuple(entry.get("paths") or ()),
            config=tuple(entry.get("config") or ()),
            preregistration=tuple(entry.get("preregistration") or ()),
            commands=tuple(entry.get("commands") or ()),
            note=entry.get("note") or "",
        )
        for name, entry in data["measurements"].items()
    )
    unmeasured = tuple(
        Unmeasured(paths=tuple(entry["paths"]), reason=entry["reason"])
        for entry in data.get("unmeasured") or ()
    )
    return MeasurementMap(measurements, unmeasured)


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """Dotted key -> leaf value; a list is a leaf."""
    if not isinstance(data, dict):
        return {prefix: data} if prefix else {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        out.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    return out


def changed_config_keys(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[str]:
    """Keys added, removed or with a different value between two parsed configs."""
    before, after = flatten(old or {}), flatten(new or {})
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def _matching(values: list[str], patterns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(v for v in values if any(fnmatch.fnmatchcase(v, p) for p in patterns))


def covering(
    mmap: MeasurementMap, paths: list[str], keys: list[str]
) -> tuple[list[Hit], list[tuple[Unmeasured, tuple[str, ...]]]]:
    """The measurements covering the changed paths and keys, and the unmeasured paths hit."""
    hits = []
    for measurement in mmap.measurements:
        hit_paths = _matching(paths, measurement.paths)
        hit_keys = _matching(keys, measurement.config)
        if hit_paths or hit_keys:
            hits.append(Hit(measurement, hit_paths, hit_keys))
    unmeasured = [
        (entry, hit) for entry in mmap.unmeasured if (hit := _matching(paths, entry.paths))
    ]
    return hits, unmeasured


def render(hits: list[Hit], unmeasured: list[tuple[Unmeasured, tuple[str, ...]]]) -> str:
    """Markdown for the pull request; empty when the change touches no measured surface."""
    if not hits and not unmeasured:
        return ""
    lines = [
        COMMENT_MARKER,
        "### Medições associadas a esta mudança",
        "",
        (
            "Este PR altera prompts, limiares ou modelos que têm uma medição em `evals/` "
            "(`evals/measurement_map.yaml`, #333). O CI não roda nenhuma delas: rode antes "
            "do merge e aplique a regra pré-registrada."
        ),
    ]
    for hit in hits:
        m = hit.measurement
        changed = [f"`{p}`" for p in hit.paths] + [f"`config: {k}`" for k in hit.keys]
        lines += ["", f"#### {m.title} (`{m.name}`)", "", f"- Mudou: {', '.join(changed)}"]
        if m.preregistration:
            lines.append(f"- Pré-registro: {', '.join(f'`{p}`' for p in m.preregistration)}")
        if m.note:
            lines.append(f"- {m.note}")
        if m.commands:
            lines += ["", "```bash", *m.commands, "```"]
    if unmeasured:
        lines += ["", "#### Sem medição associada", ""]
        lines += [f"- {', '.join(f'`{p}`' for p in hit)}: {e.reason}" for e, hit in unmeasured]
    return "\n".join(lines) + "\n"


# -- CLI -----------------------------------------------------------------


def _git(*args: str) -> str:
    """Run git in the current directory (the CI checkout)."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _config_at(ref: str) -> dict[str, Any] | None:
    try:
        return yaml.safe_load(_git("show", f"{ref}:{CONFIG_PATH}"))
    except subprocess.CalledProcessError:  # the file does not exist at that ref
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--base", required=True, help="ref da base (ex.: origin/main)")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--map", type=Path, default=MAP_PATH)
    parser.add_argument("--out", type=Path, default=None, help="grava o Markdown aqui")
    args = parser.parse_args(argv)

    base = _git("merge-base", args.base, args.head).strip()
    paths = _git("diff", "--name-only", base, args.head).split()
    keys = changed_config_keys(_config_at(base), _config_at(args.head))
    text = render(*covering(load_map(args.map), paths, keys))

    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
