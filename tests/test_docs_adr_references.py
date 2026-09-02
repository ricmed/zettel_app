"""Guards the `## References` section of generated ADRs against path rot.

ADR-027 turned `zettel/harvester.py` into a package and left 13 dead references
behind across six ADRs (issue #87). A reference that looks authoritative and
resolves to nothing is worse than no reference, so this test fails the moment a
References bullet cites a repo path that does not exist.

Only bullets under `## References` are checked. Prose elsewhere may legitimately
discuss a file that was deleted (ADR-027 describing the monolith it removed) or
one that does not exist yet (ADR-029 proposing a package).
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATED = REPO_ROOT / "docs" / "adrs" / "generated"

# Repo-relative paths cited inside a code span, e.g. `zettel/harvester/chunking.py`.
_PATH_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|toml|lock|yaml|yml|md))`")


def _reference_paths(adr: Path) -> list[str]:
    text = adr.read_text(encoding="utf-8")
    if "## References" not in text:
        return []
    section = text.split("## References", 1)[1]
    # Stop at the next top-level heading, if any.
    section = re.split(r"^## ", section, maxsplit=1, flags=re.MULTILINE)[0]
    paths: list[str] = []
    for line in section.splitlines():
        if line.lstrip().startswith(("*", "-")):
            paths.extend(_PATH_RE.findall(line))
    return paths


def _adr_files() -> list[Path]:
    return sorted(GENERATED.rglob("ADR-*.md"))


def test_generated_adrs_are_present():
    """Guard against the glob silently matching nothing."""
    assert _adr_files(), f"nenhuma ADR encontrada em {GENERATED}"


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_adr_reference_paths_exist(adr: Path):
    missing = [p for p in _reference_paths(adr) if not (REPO_ROOT / p).exists()]
    assert not missing, (
        f"{adr.relative_to(REPO_ROOT)} cita caminho(s) inexistente(s) em '## References': "
        f"{', '.join(missing)}. Cite modulo + nome de simbolo, sem intervalo de linha."
    )
