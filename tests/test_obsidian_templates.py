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
from zettel.bibliography import format_abnt_in_text
from zettel.config import AppConfig
from zettel.harvester.citekey import _disambiguation_suffix
from zettel.manual_lit import create_permanent_from_literature
from zettel.new_note import (
    literature_scaffold,
    moc_scaffold,
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
  content: f.content || "",
}));
const folders = new Set(scenario.folders || []);
const created = [];
global.app = {
  vault: {
    getMarkdownFiles: () => files,
    getAbstractFileByPath: (p) => (folders.has(p) || files.some((f) => f.path === p) ? {} : null),
    createFolder: async (p) => { folders.add(p); created.push({ folder: p }); },
    create: async (p, content) => { created.push({ path: p, content }); },
    read: async (file) => {
      const path = typeof file === "string" ? file : file.path;
      const found = files.find((f) => f.path === path);
      if (!found) throw new Error("not found: " + path);
      return found.content || "";
    },
  },
  metadataCache: { getFileCache: (f) => ({ frontmatter: f.fm }) },
};
const answers = [...(scenario.prompts || [])];
const choices = [...(scenario.choices || [scenario.choice ?? 0])];
let moved = null;
const tp = {
  system: {
    prompt: async () => answers.shift(),
    suggester: async (labels, items) => items[choices.shift() ?? 0],
  },
  file: {
    move: async (p) => { moved = p; },
    path: () => scenario.targetPath || "",
  },
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
    return {"path": path, "frontmatter": _parse(content)[0], "content": content}


def _node_error(script: Path, scenario: dict) -> str:
    result = subprocess.run(
        [NODE, str(script.with_name("harness.js")), str(script), json.dumps(scenario)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    out = json.loads(result.stdout)
    assert "error" in out, out
    return out["error"]


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
_ABNT = [
    (["Daniel Kahneman"], 2011, "p. 42"),
    (["Ana Souza", "Rui Lima"], 2020, None),
    (["A", "B", "C", "D"], 1999, "p. 1"),
    ([], 2020, None),
    ([], None, None),
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
        + [["formatAbnt", list(case)] for case in _ABNT]
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
        + [format_abnt_in_text(*case) for case in _ABNT]
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
    want_meta["origin_path"] = want_meta["origin_path"].replace("\\", "/")
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


def _filled_lit(source_note, *, topic="Escassez", page=42, **kwargs):
    src_meta = _parse(source_note["content"])[0]
    citekey = src_meta["citekey"]
    defaults = {
        "summary": "A escassez define o valor relativo dos bens.",
        "source_text": "O valor nasce da escassez relativa.",
        "key_concepts": ["escassez", "valor"],
        "candidates": [{"thesis": "Escassez gera valor."}],
        **kwargs,
    }
    meta, body = literature_scaffold(
        source_id=src_meta["source_id"],
        citekey=citekey,
        source_title=src_meta["title"],
        topic=topic,
        chunk_index=1,
        page=page,
        vault_timezone=TZ,
        **defaults,
    )
    content = compose_note(meta, body)
    filename = literature_chunk_filename(
        citekey, chunk_index=1, page_in_book=page, section_path=topic
    )
    path = f"20_Literature/{citekey}/{filename}"
    return path, content


@pytest.mark.slow
@needs_node
def test_permanent_flow_matches_from_lit_without_llm(script, source_note, tmp_path):
    lit_path, lit_content = _filled_lit(source_note)
    files = [*_source_files(source_note), _file(lit_path, lit_content)]
    out = _node(
        script,
        {"flow": "permanent", "prompts": [""], "files": files, "choices": [0, 0]},
    )
    got_meta, got_body = _parse(out["content"])
    assert (
        out["moved"]
        == "30_Permanent/" + note_filename("ZTL", got_meta["note_id"], got_meta["title"])[:-3]
    )

    vault = tmp_path / "vault"
    notes = {
        source_note["moved"]: source_note["content"],
        **{c["path"][:-3]: c["content"] for c in source_note["created"] if "path" in c},
        lit_path[:-3]: lit_content,
    }
    for rel, content in notes.items():
        path = vault / f"{rel}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    cfg = AppConfig(vault_path=vault)
    db = StateDB(tmp_path / "state.db")
    try:
        run_sync_manual(cfg, db, FakeVectorIndex())
        want_path, via_llm = create_permanent_from_literature(
            cfg, db, FakeVectorIndex(), str(vault / lit_path)
        )
        assert via_llm is False
        want_meta, want_body = _parse(want_path.read_text(encoding="utf-8"))
    finally:
        db.close()

    for key in ("note_id", "created_at", "updated_at"):
        want_meta[key] = got_meta[key]
    assert got_meta == want_meta
    assert got_body == want_body


@pytest.mark.slow
@needs_node
def test_permanent_flow_uses_the_thesis_override(script, source_note):
    lit_path, lit_content = _filled_lit(source_note)
    files = [*_source_files(source_note), _file(lit_path, lit_content)]
    out = _node(
        script,
        {
            "flow": "permanent",
            "prompts": ["Tese escrita na mao"],
            "files": files,
            "choices": [0, 0],
        },
    )
    meta, body = _parse(out["content"])
    assert meta["title"] == "Tese escrita na mao"
    assert "> **Tese**: Tese escrita na mao" in body


@pytest.mark.slow
@needs_node
def test_permanent_flow_refuses_a_placeholder_lit(script, source_note):
    lit = _node(
        script,
        {"flow": "literature", "prompts": ["Escassez", ""], "files": _source_files(source_note)},
    )
    files = [*_source_files(source_note), _file(lit["moved"] + ".md", lit["content"])]
    error = _node_error(
        script,
        {"flow": "permanent", "prompts": [""], "files": files, "choices": [0, 0]},
    )
    assert "Nao foi possivel derivar uma tese" in error


@pytest.mark.slow
@needs_node
def test_permanent_flow_refuses_without_a_source(script):
    error = _node_error(script, {"flow": "permanent", "prompts": [""]})
    assert "nenhuma SRC" in error


@pytest.mark.slow
@needs_node
def test_permanent_flow_refuses_insert_on_an_existing_note(script, source_note):
    lit_path, lit_content = _filled_lit(source_note)
    existing = "30_Permanent/ZTL - already.md"
    files = [
        *_source_files(source_note),
        _file(lit_path, lit_content),
        _file(existing, "---\ntype: permanent\nnote_id: x\n---\n"),
    ]
    error = _node_error(
        script,
        {
            "flow": "permanent",
            "prompts": [""],
            "files": files,
            "choices": [0, 0],
            "targetPath": existing,
        },
    )
    assert "Create new note from template" in error


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
    lit_path, lit_content = _filled_lit(source_note)
    files = [*files, _file(lit_path, lit_content)]
    ztl = _node(
        script,
        {"flow": "permanent", "prompts": [""], "files": files, "choices": [0, 0]},
    )
    notes = {
        source_note["moved"]: source_note["content"],
        **{c["path"][:-3]: c["content"] for c in source_note["created"] if "path" in c},
        lit_path[:-3]: lit_content,
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


# -- the vault's Obsidian setup (#345) ---------------------------------------


def _release(version="2.9.1", plugin_id="templater-obsidian") -> dict[str, bytes]:
    return {
        "main.js": f"// templater {version}".encode(),
        "manifest.json": json.dumps({"id": plugin_id, "version": version}).encode(),
        "styles.css": b".templater {}",
    }


def _downloader(files: dict[str, bytes], urls: list[str] | None = None):
    def download(url: str) -> bytes:
        if urls is not None:
            urls.append(url)
        return files[url.rsplit("/", 1)[-1]]

    return download


def _offline(url: str) -> bytes:
    raise OSError("sem rede")


def _obsidian_cfg(**overrides):
    from zettel.config import ObsidianConfig

    return ObsidianConfig(**overrides)


def test_templater_settings_keep_the_users_settings_and_folders():
    from zettel.obsidian import templater_settings

    existing = {
        "command_timeout": 9,
        "folder_templates": [
            {"folder": "Diario", "template": "Templates/Dia.md"},
            {"folder": "30_Permanent", "template": "Velho.md"},
            {"folder": "", "template": ""},
        ],
        "enabled_templates_hotkeys": ["Templates/Dia.md", ""],
    }

    merged = templater_settings(existing)

    assert merged["command_timeout"] == 9
    assert "data_version" not in merged  # an old file is Templater's to migrate
    assert merged["templates_folder"] == "99_Templates"
    assert merged["user_scripts_folder"] == "99_Templates/scripts"
    assert merged["trigger_on_file_creation_mode"] == "folder"
    assert "trigger_on_file_creation" not in merged  # a per-device switch, not the vault's
    folders = {e["folder"]: e["template"] for e in merged["folder_templates"]}
    assert folders == {
        "Diario": "Templates/Dia.md",
        "10_Sources": "99_Templates/SRC.md",
        "20_Literature": "99_Templates/LIT - granular.md",
        "30_Permanent": "99_Templates/ZTL.md",
        "40_MOCs": "99_Templates/MOC.md",
    }
    assert merged["enabled_templates_hotkeys"][0] == "Templates/Dia.md"
    assert "99_Templates/ZTL.md" in merged["enabled_templates_hotkeys"]


def test_setup_installs_templater_and_merges_the_obsidian_settings(tmp_path):
    from zettel.obsidian import setup_obsidian

    vault, cache = tmp_path / "vault", tmp_path / "cache"
    plugins = vault / ".obsidian" / "community-plugins.json"
    plugins.parent.mkdir(parents=True)
    plugins.write_text('["dataview"]', encoding="utf-8")

    report = setup_obsidian(vault, cache, _obsidian_cfg(), TZ, download=_downloader(_release()))

    assert report.plugin_ok and "versao 2.9.1" in report.plugin
    assert json.loads(plugins.read_text(encoding="utf-8")) == ["dataview", "templater-obsidian"]
    plugin = vault / ".obsidian" / "plugins" / "templater-obsidian"
    assert (plugin / "main.js").read_bytes() == b"// templater 2.9.1"
    data = json.loads((plugin / "data.json").read_text(encoding="utf-8"))
    assert data["data_version"] == 2 and data["templates_folder"] == "99_Templates"
    assert (vault / SCRIPTS_DIR / SCRIPT_NAME).is_file()
    assert (cache / "obsidian" / "templater-obsidian" / "2.9.1" / "main.js").is_file()
    assert set(report.sha256) == {"main.js", "manifest.json", "styles.css"}

    again = setup_obsidian(vault, cache, _obsidian_cfg(), TZ, download=_offline)
    assert again.plugin == "ja instalado (versao 2.9.1)"
    assert again.templates == [] and again.settings == []


def test_a_second_vault_installs_from_the_cache_without_network(tmp_path):
    from zettel.obsidian import setup_obsidian

    cache = tmp_path / "cache"
    setup_obsidian(tmp_path / "a", cache, _obsidian_cfg(), TZ, download=_downloader(_release()))

    report = setup_obsidian(tmp_path / "b", cache, _obsidian_cfg(), TZ, download=_offline)

    assert report.plugin_ok and "cache (2.9.1)" in report.plugin


def test_latest_takes_the_highest_cached_version_and_a_tag_downloads_that_tag(tmp_path):
    from zettel.obsidian import fetch_templater

    cache = tmp_path / "cache"
    for version in ("2.9.1", "2.10.0"):
        folder = cache / version
        folder.mkdir(parents=True)
        for name, data in _release(version).items():
            (folder / name).write_bytes(data)
    assert fetch_templater(cache, "latest", {}, download=_offline)[0].name == "2.10.0"

    urls: list[str] = []
    fetch_templater(cache, "2.11.0", {}, download=_downloader(_release("2.11.0"), urls))
    assert urls[0] == "https://github.com/SilentVoid13/Templater/releases/download/2.11.0/main.js"


def test_without_network_or_cache_the_plugin_is_reported_and_the_rest_written(tmp_path):
    from zettel.obsidian import setup_obsidian

    report = setup_obsidian(
        tmp_path / "vault", tmp_path / "cache", _obsidian_cfg(), TZ, download=_offline
    )

    assert not report.plugin_ok
    assert "download de main.js falhou" in report.plugin and "Community plugins" in report.plugin
    assert (tmp_path / "vault" / TEMPLATES_DIR / "ZTL.md").is_file()
    assert not list((tmp_path / "cache").rglob("*.js"))


def test_a_pinned_hash_that_does_not_match_is_refused(tmp_path):
    from zettel.obsidian import setup_obsidian

    pinned = _obsidian_cfg(templater_sha256={"main.js": "0" * 64})

    report = setup_obsidian(
        tmp_path / "vault", tmp_path / "cache", pinned, TZ, download=_downloader(_release())
    )

    assert not report.plugin_ok and "sha256 de main.js nao confere" in report.plugin
    assert not (
        tmp_path / "vault" / ".obsidian" / "plugins" / "templater-obsidian" / "main.js"
    ).exists()


def test_a_release_of_another_plugin_is_refused(tmp_path):
    from zettel.obsidian import TemplaterUnavailable, fetch_templater

    with pytest.raises(TemplaterUnavailable, match="nao e do Templater"):
        fetch_templater(tmp_path, "latest", {}, download=_downloader(_release(plugin_id="outro")))


def test_templater_from_a_local_folder(tmp_path):
    from zettel.obsidian import setup_obsidian

    folder = tmp_path / "release"
    folder.mkdir()
    for name, data in _release("2.9.4").items():
        (folder / name).write_bytes(data)

    report = setup_obsidian(
        tmp_path / "vault",
        tmp_path / "cache",
        _obsidian_cfg(),
        TZ,
        source=folder,
        download=_offline,
    )

    assert report.plugin_ok and f"pasta {folder}" in report.plugin


def test_an_unreadable_obsidian_setting_is_never_overwritten(tmp_path):
    from zettel.obsidian import ObsidianSettingsError, setup_obsidian

    plugins = tmp_path / "vault" / ".obsidian" / "community-plugins.json"
    plugins.parent.mkdir(parents=True)
    plugins.write_text("{quebrado", encoding="utf-8")

    with pytest.raises(ObsidianSettingsError, match=r"community-plugins\.json"):
        setup_obsidian(
            tmp_path / "vault", tmp_path / "cache", _obsidian_cfg(), TZ, download=_offline
        )
    assert plugins.read_text(encoding="utf-8") == "{quebrado"


def test_obsidian_health_names_what_is_missing(tmp_path):
    from zettel.obsidian import obsidian_health, setup_obsidian

    assert obsidian_health(tmp_path)[0] is False
    setup_obsidian(
        tmp_path, tmp_path / "cache", _obsidian_cfg(), TZ, download=_downloader(_release())
    )
    assert obsidian_health(tmp_path) == (True, "templates em 99_Templates/, Templater 2.9.1")


def test_init_prepares_the_vault_for_obsidian(cli_instance):
    output = cli_instance.run("init")

    assert (cli_instance.vault / TEMPLATES_DIR / "ZTL.md").is_file()
    data = cli_instance.vault / ".obsidian" / "plugins" / "templater-obsidian" / "data.json"
    assert json.loads(data.read_text(encoding="utf-8"))["user_scripts_folder"] == SCRIPTS_DIR
    # The test suite has no network: the plugin is reported, init still succeeds.
    assert "Templater" in output and "nao instalado" in output


def test_obsidian_setup_exits_1_when_the_plugin_could_not_be_installed(cli_instance, tmp_path):
    output = cli_instance.run("obsidian-setup", code=1)
    assert "nao instalado" in output

    # The operational config pins the 2.26.0 hashes: a release that differs is refused.
    folder = tmp_path / "release"
    folder.mkdir()
    for name, data in _release().items():
        (folder / name).write_bytes(data)
    output = cli_instance.run("obsidian-setup", "--templater-from", str(folder), code=1)
    assert "sha256 de main.js nao confere" in output
