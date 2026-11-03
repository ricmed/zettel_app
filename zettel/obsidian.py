"""Obsidian templates generated from the vault builders (#344).

Writing a note in Obsidian must give the same note ``zettel new-note`` writes:
the same frontmatter keys, sections and managed blocks, so ``sync-manual`` adopts
it the same way. The templates are therefore not written by hand. The scaffolds
in ``zettel.new_note`` (built on ``zettel.vault``) run here with sentinel values,
and each sentinel, and every form derived from it (slug, AuthorYear label), is
replaced by the JavaScript expression that computes it inside Obsidian. A change
to a builder reaches the templates the next time they are generated.

Inside Obsidian the Templater plugin runs ``99_Templates/scripts/zettel.js``:
it prompts for the title (and the source), generates the ULID or the next
``chunk_index``, moves the file to ``PREFIX - ID - slug.md`` and returns the
content. Each template file is one line that calls it.

Pure: no SQLite, Chroma, LLM or network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zettel.new_note import (
    literature_scaffold,
    moc_scaffold,
    permanent_scaffold,
    source_scaffold,
)
from zettel.vault import build_literature_index_note, source_note_filename

TEMPLATES_DIR = "99_Templates"
SCRIPTS_DIR = f"{TEMPLATES_DIR}/scripts"
SCRIPT_NAME = "zettel.js"

# Template file -> flow in zettel.js. The folder templates (#345) map each vault
# folder to one of these files.
TEMPLATE_FLOWS = {
    "ZTL.md": "permanent",
    "SRC.md": "source",
    "LIT - indice.md": "literatureIndex",
    "LIT - granular.md": "literature",
    "MOC.md": "moc",
}

_RUNTIME = Path(__file__).with_name("obsidian_runtime.js")

# Sentinels. The citekey carries a year so its AuthorYear label is a distinct,
# shorter string; the numbers stand where a builder needs an int.
_CITEKEY = "Zsentck2000Zsentct"
_LABEL = "Zsentck2000"
_TITLE = "Zsenttitle"
_SOURCE_TITLE = "Zsentsrctitle"
_TOPIC = "Zsenttopic"
_AUTHOR = "Zsentauthor"
_NOTE_ID = "Zsentnoteid"
_SRC_STEM = "Zsentsrcstem"
_PATH = "Zsentpath"
_YEAR = 2999
_PAGE = 2998
_CHUNK_INDEX = 9997
_LEFTOVERS = ("zsent", str(_YEAR), str(_PAGE), str(_CHUNK_INDEX))


@dataclass(frozen=True)
class _Render:
    """One RENDER entry: a note scaffold plus how its sentinels map to JavaScript."""

    name: str
    meta: dict[str, Any]
    body: str
    tokens: tuple[tuple[str, str], ...]  # text sentinel -> JS expression, longest first
    values: tuple[tuple[Any, str], ...] = ()  # whole-value sentinel -> JS expression


def _js_template(text: str, tokens: tuple[tuple[str, str], ...]) -> str:
    """``text`` as a JS template literal, each sentinel replaced by ``${expression}``."""
    out = text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    for token, expression in tokens:
        out = out.replace(token, "${" + expression + "}")
    return f"`{out}`"


def _js_value(key: str, value: Any, render: _Render) -> str:
    if key in ("created_at", "updated_at"):
        return "v.now"
    for sentinel, expression in render.values:
        if value == sentinel and type(value) is type(sentinel):
            return expression
    if isinstance(value, str) and any(token in value for token, _ in render.tokens):
        return _js_template(value, render.tokens)
    return json.dumps(value, ensure_ascii=False)


def _js_render(render: _Render) -> str:
    pairs = ",\n      ".join(
        f"[{json.dumps(key)}, {_js_value(key, value, render)}]"
        for key, value in render.meta.items()
    )
    body = _js_template(render.body, render.tokens)
    code = (
        f"  {render.name}: (v) =>\n"
        f"    frontmatter([\n      {pairs},\n    ]) +\n"
        f'    "\\n" +\n'
        f"    {body},"
    )
    leftover = [s for s in _LEFTOVERS if s in code.lower()]
    if leftover:
        raise ValueError(f"Template {render.name}: sentinela sem expressao JS: {leftover}")
    return code


def _renders(vault_timezone: str) -> list[_Render]:
    source_id = f"@{_CITEKEY}"
    citekey_tokens = ((_CITEKEY, "v.citekey"), (_LABEL, "v.label"))
    title_tokens = ((_TITLE, "v.title"), (_TITLE.lower(), "v.slug"))

    src_meta, src_body = source_scaffold(
        source_id=source_id,
        citekey=_CITEKEY,
        title=_TITLE,
        authors=[_AUTHOR],
        year=_YEAR,
        note_path=Path(source_note_filename(_CITEKEY, _TITLE)),
        vault_timezone=vault_timezone,
    )
    src_meta["origin_path"] = _PATH
    index_meta, index_body = build_literature_index_note(
        source_id=source_id,
        citekey=_CITEKEY,
        title=_TITLE,
        origin="manual",
        vault_timezone=vault_timezone,
    )
    lit_meta, lit_body = literature_scaffold(
        source_id=source_id,
        citekey=_CITEKEY,
        source_title=_SOURCE_TITLE,
        topic=_TOPIC,
        chunk_index=_CHUNK_INDEX,
        page=_PAGE,
        vault_timezone=vault_timezone,
    )
    ztl_meta, ztl_body = permanent_scaffold(note_id=_NOTE_ID, title=_TITLE, now="")
    ztl_src_meta, ztl_src_body = permanent_scaffold(
        note_id=_NOTE_ID,
        title=_TITLE,
        now="",
        source_id=source_id,
        source_ref=f"[[{_SRC_STEM}]]",
    )
    moc_meta, moc_body = moc_scaffold(moc_id=_NOTE_ID, title=_TITLE, now="")
    ztl_tokens = ((_NOTE_ID, "v.id"), (_TITLE, "v.title"))
    return [
        _Render(
            "source",
            src_meta,
            src_body,
            (
                *citekey_tokens,
                *title_tokens,
                (_AUTHOR, "v.authorsText"),
                (str(_YEAR), "v.yearText"),
                (_PATH, "v.path"),
            ),
            ((_YEAR, "v.year"), ([_AUTHOR], "v.authors")),
        ),
        _Render("literatureIndex", index_meta, index_body, (*citekey_tokens, *title_tokens)),
        _Render(
            "literature",
            lit_meta,
            lit_body,
            (
                *citekey_tokens,
                (_SOURCE_TITLE, "v.sourceTitle"),
                (_SOURCE_TITLE.lower(), "v.sourceSlug"),
                (_TOPIC, "v.topic"),
                (f"p. {_PAGE}", "v.pageLabel"),
                (str(_CHUNK_INDEX), "v.idx4"),
            ),
            ((_CHUNK_INDEX, "v.chunkIndex"), (_PAGE, "v.page")),
        ),
        _Render("permanent", ztl_meta, ztl_body, ztl_tokens),
        _Render(
            "permanentWithSource",
            ztl_src_meta,
            ztl_src_body,
            (*ztl_tokens, *citekey_tokens, (_SRC_STEM, "v.srcStem")),
        ),
        _Render("moc", moc_meta, moc_body, ztl_tokens),
    ]


def build_script(vault_timezone: str) -> str:
    """The full ``zettel.js``: header, runtime helpers and flows, RENDER."""
    renders = "\n".join(_js_render(r) for r in _renders(vault_timezone))
    return (
        "// Gerado por `zettel obsidian-templates` a partir de zettel/vault.py.\n"
        "// Nao edite: rode o comando de novo (--force) depois de atualizar o Zettel.\n"
        '"use strict";\n\n'
        f"const VAULT_TIMEZONE = {json.dumps(vault_timezone)};\n\n"
        f"{_RUNTIME.read_text(encoding='utf-8')}\n"
        "// Note scaffolds generated from zettel.new_note / zettel.vault.\n"
        f"const RENDER = {{\n{renders}\n}};\n"
    )


def template_text(flow: str) -> str:
    """A template file: one Templater command that appends the note the flow returns."""
    return f"<%* tR += await tp.user.zettel().{flow}(tp); -%>\n"


def write_templates(vault_path: Path, vault_timezone: str, *, force: bool = False) -> list[Path]:
    """Write the templates and the script under ``99_Templates/``; return what was written.

    Without ``force`` a file that already exists is kept, so a template the user
    adjusted survives; ``force`` regenerates everything (after an update).
    """
    templates = vault_path / TEMPLATES_DIR
    targets = {templates / name: template_text(flow) for name, flow in TEMPLATE_FLOWS.items()}
    targets[vault_path / SCRIPTS_DIR / SCRIPT_NAME] = build_script(vault_timezone)
    written: list[Path] = []
    for path, text in targets.items():
        if path.exists() and not force:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
