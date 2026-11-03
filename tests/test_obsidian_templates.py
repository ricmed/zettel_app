"""Obsidian templates generated from the vault builders (#344).

The generated ``zettel.js`` runs under Node with a fake Obsidian ``app`` and a
fake Templater ``tp``: every flow must return the note ``zettel.new_note``'s
scaffold builds for the same answers, at the path ``new-note`` would use, and
``sync-manual`` must adopt what it returns.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from zettel.config import AppConfig
from zettel.harvester.citekey import _disambiguation_suffix
from zettel.new_note import (
    literature_scaffold,
    moc_scaffold,
    permanent_scaffold,
    provisional_citekey,
    source_scaffold,
)
from zettel.obsidian import (
    SCRIPT_NAME,
    SCRIPTS_DIR,
    TEMPLATE_FLOWS,
    TEMPLATES_DIR,
    build_script,
    template_text,
    write_templates,
)
from zettel.state import StateDB
from zettel.sync import run_sync_manual
from zettel.vault import (
    _slug,
    author_year_label,
    build_literature_index_note,
    compose_note,
    literature_chunk_filename,
    literature_index_filename,
    note_filename,
    parse_frontmatter_strict,
)

from tests.fakes import FakeVectorIndex

TZ = "America/Sao_Paulo"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_HARNESS = r"""
const [scriptPath, raw] = process.argv.slice(2);
const scenario = JSON.parse(raw);
const files = (scenario.files || []).map((f) => ({
  path: f.path,
  basename: f.path.split("/").pop().replace(/\.md$/, ""),
  fm: f.frontmatter || {},
}));
const folders = new Set(scenario.folders || []);
const created = [];
global.app = {
  vault: {
    getMarkdownFiles: () => files,
    getAbstractFileByPath: (p) => (folders.has(p) || files.some((f) => f.path === p) ? {} : null),
    createFolder: async (p) => { folders.add(p); created.push({ folder: p }); },
    create: async (p, content) => { created.push({ path: p, content }); },
  },
  metadataCache: { getFileCache: (f) => ({ frontmatter: f.fm }) },
};
const answers = [...(scenario.prompts || [])];
let moved = null;
const tp = {
  system: {
    prompt: async () => answers.shift(),
    suggester: async (labels, items) => items[scenario.choice ?? 0],
  },
  file: { move: async (p) => { moved = p; } },
};
const z = require(scriptPath)();
(async () => {
  if (scenario.helpers) {
    // {"$date": ms} stands for a Date, which JSON cannot carry.
    const arg = (a) => (a && typeof a === "object" && "$date" in a ? new Date(a.$date) : a);
    const out = scenario.helpers.map(([name, args]) => z[name](...args.map(arg)));
    console.log(JSON.stringify(out));
    return;
  }
  const content = await z[scenario.flow](tp);
  console.log(JSON.stringify({ moved, content, created }));
})().catch((e) => console.log(JSON.stringify({ error: e.message })));
"""

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node nao instalado")


@pytest.fixture(scope="module")
def script(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("obsidian")
    path = root / "zettel.js"
    path.write_text(build_script(TZ), encoding="utf-8")
    (root / "harness.js").write_text(_HARNESS, encoding="utf-8")
    return path


def _node(script: Path, scenario: dict):
    result = subprocess.run(
        [NODE, str(script.with_name("harness.js")), str(script), json.dumps(scenario)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    out = json.loads(result.stdout)
    assert not (isinstance(out, dict) and "error" in out), out
    return out


def _parse(content: str) -> tuple[dict, str]:
    return parse_frontmatter_strict(content)


def _expect(content: str, meta: dict, body: str, *, now: str) -> None:
    """The template's note equals the scaffold's, read the way sync reads it."""
    meta = {**meta, "created_at": now, "updated_at": now}
    got_meta, got_body = _parse(content)
    want_meta, want_body = _parse(compose_note(meta, body))
    assert got_meta == want_meta
    assert got_body == want_body


def _file(path: str, content: str) -> dict:
    return {"path": path, "frontmatter": _parse(content)[0]}


# -- generation ---------------------------------------------------------------


def test_each_template_is_one_templater_call():
    assert template_text("permanent") == "<%* tR += await tp.user.zettel().permanent(tp); -%>\n"


def test_the_script_carries_no_sentinel_and_the_vault_timezone():
    script = build_script("Europe/Berlin")
    assert "zsent" not in script.lower()
    assert 'const VAULT_TIMEZONE = "Europe/Berlin";' in script
    assert "module.exports = function zettel()" in script


def test_write_templates_keeps_existing_files_unless_forced(tmp_path):
    written = write_templates(tmp_path, TZ)
    expected = {tmp_path / TEMPLATES_DIR / name for name in TEMPLATE_FLOWS} | {
        tmp_path / SCRIPTS_DIR / SCRIPT_NAME
    }
    assert set(written) == expected
    edited = tmp_path / TEMPLATES_DIR / "ZTL.md"
    edited.write_text("meu template", encoding="utf-8")

    assert write_templates(tmp_path, TZ) == []
    assert edited.read_text(encoding="utf-8") == "meu template"

    assert set(write_templates(tmp_path, TZ, force=True)) == expected
    assert edited.read_text(encoding="utf-8") == template_text("permanent")


def test_cli_writes_the_templates(cli_instance):
    output = cli_instance.run("obsidian-templates")
    assert (cli_instance.vault / SCRIPTS_DIR / SCRIPT_NAME).is_file()
    assert "ZTL.md" in output
    assert "Nada gravado" in cli_instance.run("obsidian-templates")


# -- helpers mirror the Python functions --------------------------------------

_SLUGS = [
    "Ação e Reação: um estudo",
    "  Já_vi   isso!! ",
    "Ünïcödé — teste",
    "a" * 120,
    "termina com ---",
    "日本語 タイトル",
    "",
]
_CITEKEYS = ["@Gonçalves2021Analise", "Silva2020Obra", "Anon", "2020Obra", "a/../b"]
_PROVISIONAL = [
    (["Daniel Kahneman"], 2011, "Rápido e devagar: duas formas de pensar"),
    (["Ana Souza"], None, "Obra sem ano"),
    ([], 2020, "Só o ano"),
    ([], None, "Nem autor nem ano aqui"),
    (["José da Silva"], 1999, "!!!"),
]
_CHUNKS = [
    ("Souza2020Obra", 3, 42, "Capítulo 2 > Escassez e valor"),
    ("Souza2020Obra", 12, None, "Documento completo"),
    ("Souza2020Obra", 1, 7, ""),
]


@pytest.mark.slow
@needs_node
def test_js_helpers_match_the_python_functions(script):
    calls = (
        [["slug", [t]] for t in _SLUGS]
        + [["slug", [t, 40]] for t in _SLUGS]
        + [["authorYearLabel", [c]] for c in _CITEKEYS]
        + [["provisionalCitekey", list(case)] for case in _PROVISIONAL]
        + [["literatureChunkStem", list(case)] for case in _CHUNKS]
        + [["disambiguation", [n]] for n in (1, 26, 27, 52, 703)]
    )
    expected = (
        [_slug(t) for t in _SLUGS]
        + [_slug(t, max_len=40) for t in _SLUGS]
        + [author_year_label(c) for c in _CITEKEYS]
        + [provisional_citekey(*case) for case in _PROVISIONAL]
        + [
            literature_chunk_filename(ck, chunk_index=i, page_in_book=p, section_path=t)[:-3]
            for ck, i, p, t in _CHUNKS
        ]
        + [_disambiguation_suffix(n) for n in (1, 26, 27, 52, 703)]
    )
    assert _node(script, {"helpers": calls}) == expected


@pytest.mark.slow
@needs_node
@pytest.mark.parametrize("tz", ["America/Sao_Paulo", "Europe/Berlin", "Asia/Kolkata"])
def test_vault_now_matches_now_vault_iso(tmp_path, tz):
    path = tmp_path / "zettel.js"
    path.write_text(build_script(tz), encoding="utf-8")
    (tmp_path / "harness.js").write_text(_HARNESS, encoding="utf-8")
    instant = datetime(2026, 7, 1, 12, 34, 56, tzinfo=UTC)

    out = _node(path, {"helpers": [["vaultNow", [{"$date": instant.timestamp() * 1000}]]]})

    assert out == [instant.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")]


@pytest.mark.slow
@needs_node
def test_ulid_is_crockford_and_encodes_the_time(script):
    (value,) = _node(script, {"helpers": [["ulid", [1_700_000_000_000]]]})
    assert len(value) == 26 and set(value) <= set(CROCKFORD)
    millis = 0
    for ch in value[:10]:
        millis = millis * 32 + CROCKFORD.index(ch)
    assert millis == 1_700_000_000_000


# -- each flow writes what new-note writes ------------------------------------


@pytest.fixture(scope="module")
def source_note(script):
    out = _node(
        script,
        {"flow": "source", "prompts": ["Obra de Teste: a escassez", "Ana Souza; Rui Lima", "2020"]},
    )
    return out


@pytest.mark.slow
@needs_node
def test_source_flow_writes_the_new_note_source_and_its_index(source_note):
    citekey = provisional_citekey(["Ana Souza", "Rui Lima"], 2020, "Obra de Teste: a escassez")
    title = "Obra de Teste: a escassez"
    stem = note_filename("SRC", author_year_label(citekey), title)[:-3]
    assert source_note["moved"] == f"10_Sources/{stem}"
    meta, _ = _parse(source_note["content"])
    want_meta, want_body = source_scaffold(
        source_id=f"@{citekey}",
        citekey=citekey,
        title=title,
        authors=["Ana Souza", "Rui Lima"],
        year=2020,
        note_path=Path(f"10_Sources/{stem}.md"),
        vault_timezone=TZ,
    )
    _expect(source_note["content"], want_meta, want_body, now=meta["created_at"])

    (index,) = [c for c in source_note["created"] if "path" in c]
    assert index["path"] == f"20_Literature/{literature_index_filename(citekey, title)}"
    index_meta, index_body = build_literature_index_note(
        source_id=f"@{citekey}", citekey=citekey, title=title, origin="manual"
    )
    _expect(index["content"], index_meta, index_body, now=meta["created_at"])


@pytest.mark.slow
@needs_node
def test_source_flow_disambiguates_a_citekey_already_in_the_vault(script):
    taken = provisional_citekey(["Ana Souza"], 2020, "Obra")
    out = _node(
        script,
        {
            "flow": "source",
            "prompts": ["Obra", "Ana Souza", "2020"],
            "files": [
                {"path": "10_Sources/x.md", "frontmatter": {"source_id": f"@{taken}"}},
            ],
        },
    )
    assert _parse(out["content"])[0]["citekey"] == f"{taken}a"


def _source_files(source_note) -> list[dict]:
    return [_file(source_note["moved"] + ".md", source_note["content"])]


@pytest.mark.slow
@needs_node
def test_literature_flow_numbers_after_the_vault_and_matches_new_note(script, source_note):
    src_meta = _parse(source_note["content"])[0]
    citekey = src_meta["citekey"]
    files = [
        *_source_files(source_note),
        {"path": f"20_Literature/{citekey}/LIT - a.md", "frontmatter": {"chunk_index": 2}},
    ]
    topic = "Capítulo 2 > Escassez e valor"
    out = _node(
        script,
        {"flow": "literature", "prompts": [topic, "42"], "files": files, "choice": 0},
    )
    filename = literature_chunk_filename(
        citekey, chunk_index=3, page_in_book=42, section_path=topic
    )
    assert out["moved"] == f"20_Literature/{citekey}/{filename[:-3]}"
    meta, body = literature_scaffold(
        source_id=src_meta["source_id"],
        citekey=citekey,
        source_title=src_meta["title"],
        topic=topic,
        chunk_index=3,
        page=42,
        vault_timezone=TZ,
    )
    _expect(out["content"], meta, body, now=_parse(out["content"])[0]["created_at"])


@pytest.mark.slow
@needs_node
def test_literature_flow_without_a_source_refuses(script):
    out = subprocess.run(
        [
            NODE,
            str(script.with_name("harness.js")),
            str(script),
            json.dumps({"flow": "literature", "prompts": ["t"]}),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert "nenhuma SRC" in json.loads(out.stdout)["error"]


@pytest.mark.slow
@needs_node
@pytest.mark.parametrize("with_source", [True, False])
def test_permanent_flow_matches_new_note(script, source_note, with_source):
    src_meta = _parse(source_note["content"])[0]
    out = _node(
        script,
        {
            "flow": "permanent",
            "prompts": ["Escassez gera valor: uma tese"],
            "files": _source_files(source_note),
            "choice": 1 if with_source else 0,
        },
    )
    meta = _parse(out["content"])[0]
    note_id = meta["note_id"]
    assert out["moved"] == "30_Permanent/" + note_filename("ZTL", note_id, meta["title"])[:-3]
    stem = source_note["moved"].split("/")[-1]
    want_meta, want_body = permanent_scaffold(
        note_id=note_id,
        title="Escassez gera valor: uma tese",
        now=meta["created_at"],
        source_id=src_meta["source_id"] if with_source else None,
        source_ref=f"[[{stem}]]" if with_source else "",
    )
    _expect(out["content"], want_meta, want_body, now=meta["created_at"])


@pytest.mark.slow
@needs_node
def test_moc_flow_matches_new_note(script):
    out = _node(script, {"flow": "moc", "prompts": ["Economia: escassez"]})
    meta = _parse(out["content"])[0]
    assert (
        out["moved"] == "40_MOCs/" + note_filename("MOC", meta["moc_id"], "Economia: escassez")[:-3]
    )
    want_meta, want_body = moc_scaffold(
        moc_id=meta["moc_id"], title="Economia: escassez", now=meta["created_at"]
    )
    _expect(out["content"], want_meta, want_body, now=meta["created_at"])


@pytest.mark.slow
@needs_node
def test_sync_manual_adopts_what_the_templates_write(script, source_note, tmp_path):
    vault = tmp_path / "vault"
    files = _source_files(source_note)
    citekey = files[0]["frontmatter"]["citekey"]
    lit = _node(
        script,
        {"flow": "literature", "prompts": ["Escassez", ""], "files": files, "choice": 0},
    )
    ztl = _node(
        script,
        {"flow": "permanent", "prompts": ["Uma tese"], "files": files, "choice": 1},
    )
    notes = {
        source_note["moved"]: source_note["content"],
        **{c["path"][:-3]: c["content"] for c in source_note["created"] if "path" in c},
        lit["moved"]: lit["content"],
        ztl["moved"]: ztl["content"],
    }
    for rel, content in notes.items():
        path = vault / f"{rel}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    cfg = AppConfig(vault_path=vault)
    db = StateDB(tmp_path / "state.db")
    try:
        stats = run_sync_manual(cfg, db, FakeVectorIndex())
        assert stats["failures"] == []
        assert db.get_source(f"@{citekey}")["origin"] == "manual"
        assert db.get_chunk(f"@{citekey}::manual::0001")["status"] == "persisted"
        (note,) = db.list_notes()
        assert note["source_id"] == f"@{citekey}"
    finally:
        db.close()
