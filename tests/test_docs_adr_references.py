"""Guards generated ADRs against reference rot, in both directions.

*Code references*: ADR-027 turned `zettel/harvester.py` into a package and left 13
dead references behind across six ADRs (issue #87). Only bullets under
`## References` are checked — prose elsewhere may legitimately discuss a file that
was deleted (ADR-027 describing the monolith it removed) or one that does not exist
yet (ADR-029 proposing a package).

*Links between ADRs*: the `Depends on:` / `Related to:` / `Used by:` headers had 26
broken relative links across 18 ADRs (issue #93) — wrong depth, a `needs-input/`
subfolder the resolved ADRs had left, `ADR-XXX-` placeholders never renumbered, and
renamed files. Those links are how a reviewer walks from one decision to its
neighbours, so every relative link in a generated ADR must resolve.

Both checks skip fenced blocks and inline code spans: an ADR may show Markdown
syntax as an example (ADR-031 quotes `![alt](relative/path.png)` to describe
Obsidian's link forms) without that being a link.
"""

import re
from pathlib import Path

import pytest

from zettel.harvester import iter_fenced_spans

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATED = REPO_ROOT / "docs" / "adrs" / "generated"

# Repo-relative paths cited inside a code span, e.g. `zettel/harvester/chunking.py`.
_PATH_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|toml|lock|yaml|yml|md))`")

# Markdown inline link: [label](target)
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_EXTERNAL = ("http://", "https://", "mailto:", "//")


def _without_code(text: str) -> str:
    """Blank out fenced blocks and inline code spans, preserving offsets.

    Reuses the harvester's own CommonMark fence scanner (ADR-014 addendum) rather
    than reimplementing it here.
    """
    chars = list(text)
    for start, end in iter_fenced_spans(text):
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    return _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), "".join(chars))


def _relative_links(adr: Path) -> list[str]:
    """Link targets that should resolve on disk, relative to the ADR's folder."""
    targets = []
    for raw in _LINK_RE.findall(_without_code(adr.read_text(encoding="utf-8"))):
        target = raw.split("#")[0].strip()
        if not target or target.startswith(_EXTERNAL):
            continue
        targets.append(target)
    return targets


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


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_adr_relative_links_resolve(adr: Path):
    broken = [t for t in _relative_links(adr) if not (adr.parent / t).exists()]
    assert not broken, (
        f"{adr.relative_to(REPO_ROOT)} tem link(s) relativo(s) quebrado(s): "
        f"{', '.join(broken)}. O caminho e relativo a pasta da propria ADR."
    )
