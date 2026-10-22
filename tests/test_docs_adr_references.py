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
from zettel.markdown_fences import iter_fenced_spans

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


def test_potential_adrs_index_links_resolve():
    """The inventory index must point at where the potential ADRs actually live.

    27 of its 34 links rotted when triaged items moved out of `must-document/`
    into `done/` (issue #95). The indexed files themselves stay out of this
    guard: `potential-adrs/**` is the historical record of the analysis phase and
    should keep showing what was seen at the time.
    """
    index = REPO_ROOT / "docs" / "adrs" / "potential-adrs-index.md"
    assert index.exists(), f"indice nao encontrado: {index}"
    broken = [t for t in _relative_links(index) if not (index.parent / t).exists()]
    assert not broken, (
        f"potential-adrs-index.md tem link(s) relativo(s) quebrado(s): "
        f"{', '.join(broken)}. Itens triados mudam de pasta (must-document -> done)."
    )


# -- the ADR index against the files (#334) ------------------------------------

ADR_INDEX = REPO_ROOT / "docs" / "adrs" / "ADR-INDEX.md"
_ROW_RE = re.compile(r"^\| \*\*([A-Z-]+)\*\* \| (\d+) \| \[([^\]]+)\]", re.MULTILINE)


def _numbers_by_folder() -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for adr in _adr_files():
        out.setdefault(adr.parent.name, set()).add(int(adr.name[4:7]))
    return out


_EN_DASH = chr(0x2013)  # the index writes ranges as 001-008 with an en dash


def _expand(ranges: str) -> set[int]:
    """`001-008, 041-042, 052` (en dashes) -> {1, ..., 8, 41, 42, 52}."""
    numbers: set[int] = set()
    for part in ranges.split(","):
        low, _, high = part.strip().partition(_EN_DASH)
        numbers.update(range(int(low), int(high or low) + 1))
    return numbers


def test_adr_index_counts_match_the_files():
    """The header, the statistics and the module table drifted to 58, 56 and 52 while
    64 ADRs were on disk, with ADR-057 missing (#334)."""
    text = ADR_INDEX.read_text(encoding="utf-8")
    total = len(_adr_files())
    assert f"# zettel_app ADR Index ({total} Decisions)" in text
    assert f"| **Total ADRs** | {total} |" in text

    by_folder = _numbers_by_folder()
    rows = {name: (int(count), _expand(ranges)) for name, count, ranges in _ROW_RE.findall(text)}
    assert set(rows) == set(by_folder), "a tabela de modulos difere das pastas"
    for folder, numbers in by_folder.items():
        assert rows[folder] == (len(numbers), numbers), folder


def test_adr_index_has_one_entry_per_file_with_a_working_link():
    text = ADR_INDEX.read_text(encoding="utf-8")
    sections = re.findall(r"^### ADR-(\d{3}):", text, re.MULTILINE)
    assert sorted(sections) == sorted(adr.name[4:7] for adr in _adr_files())
    links = {t for t in _relative_links(ADR_INDEX) if t.startswith("./generated/")}
    for adr in _adr_files():
        assert f"./{adr.relative_to(ADR_INDEX.parent).as_posix()}" in links, adr.name


# -- commands in the user-facing docs (#334) -----------------------------------

_USER_DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "evals" / "README.md",
    *sorted((REPO_ROOT / "docs").glob("*.md")),
]
_TEST_PATH_RE = re.compile(r"(?<![\w/.])(tests/[\w/]*\.py)")


@pytest.mark.parametrize("doc", _USER_DOCS, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_test_paths_cited_in_the_docs_exist(doc: Path):
    """A documented test command must run the file it names."""
    cited = set(_TEST_PATH_RE.findall(doc.read_text(encoding="utf-8")))
    missing = sorted(p for p in cited if not (REPO_ROOT / p).is_file())
    assert not missing, f"{doc.name} cita teste(s) inexistente(s): {', '.join(missing)}"


def test_only_the_adr_index_states_how_many_adrs_there_are():
    """A count copied into the README aged from 43 to 55 while the index said 58."""
    stale = [
        doc.relative_to(REPO_ROOT).as_posix()
        for doc in _USER_DOCS
        if re.search(r"\b\d+\s*(?:%20)?decis(?:õ|%C3%B5)es", doc.read_text(encoding="utf-8"))
    ]
    assert not stale, f"contagem de ADRs fora do ADR-INDEX: {', '.join(stale)}"
