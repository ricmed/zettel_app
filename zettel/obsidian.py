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
from zettel.vault import TEMPLATES_DIR, build_literature_index_note, source_note_filename

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


# -- the vault's Obsidian setup (#345) -----------------------------------------

TEMPLATER_ID = "templater-obsidian"
TEMPLATER_FILES = ("main.js", "manifest.json", "styles.css")
TEMPLATER_RELEASES = "https://github.com/SilentVoid13/Templater/releases"
TEMPLATER_DATA_VERSION = 2

# Vault folder -> the template a new empty note there gets (folder templates).
FOLDER_TEMPLATES = {
    "10_Sources": "SRC.md",
    "20_Literature": "LIT - granular.md",
    "30_Permanent": "ZTL.md",
    "40_MOCs": "MOC.md",
}


class ObsidianSettingsError(ValueError):
    """A ``.obsidian`` settings file this setup would merge into is unreadable."""


class TemplaterUnavailable(RuntimeError):
    """The Templater release could not be found in the cache, downloaded or verified."""


@dataclass
class SetupReport:
    """What ``setup_obsidian`` did, for the CLI to print."""

    templates: list[Path]
    settings: list[Path]
    plugin: str
    plugin_ok: bool
    sha256: dict[str, str]


def _read_json(path: Path, default: Any) -> Any:
    """``path`` as JSON of ``default``'s type, or ``default`` when the file is absent.

    A file the user (or Obsidian) wrote in another shape is not overwritten: it
    raises ``ObsidianSettingsError`` naming the file.
    """
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ObsidianSettingsError(
            f"{path} nao e JSON valido; corrija ou apague o arquivo"
        ) from exc
    if not isinstance(value, type(default)):
        raise ObsidianSettingsError(f"{path} deveria conter {type(default).__name__}")
    return value


def _write_json(path: Path, value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def templater_settings(existing: dict[str, Any]) -> dict[str, Any]:
    """Templater's ``data.json`` with the vault's folders, keeping every other setting.

    Folder templates for the four note folders replace any earlier entry for the
    same folder; entries for other folders stay. The mode is ``folder``, but the
    trigger itself is a per-device switch Templater keeps out of ``data.json``
    (since 2.x a vault cannot turn on code execution for whoever opens it): the
    user turns on "Trigger Templater on new file creation" on each device.
    A new file gets ``data_version`` 2; an older one is left for Templater to migrate.
    """
    settings = dict(existing) if existing else {"data_version": TEMPLATER_DATA_VERSION}
    ours = [
        {"folder": folder, "template": f"{TEMPLATES_DIR}/{name}"}
        for folder, name in FOLDER_TEMPLATES.items()
    ]
    kept = [
        entry
        for entry in settings.get("folder_templates") or []
        if isinstance(entry, dict)
        and entry.get("folder")
        and entry["folder"] not in FOLDER_TEMPLATES
    ]
    hotkeys = [f"{TEMPLATES_DIR}/{name}" for name in TEMPLATE_FLOWS]
    previous_hotkeys = [h for h in settings.get("enabled_templates_hotkeys") or [] if h]
    settings.update(
        {
            "templates_folder": TEMPLATES_DIR,
            "user_scripts_folder": SCRIPTS_DIR,
            "trigger_on_file_creation_mode": "folder",
            "folder_templates": kept + ours,
            "enabled_templates_hotkeys": previous_hotkeys
            + [h for h in hotkeys if h not in previous_hotkeys],
        }
    )
    return settings


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(url: str) -> bytes:
    from urllib.request import urlopen

    with urlopen(url, timeout=30) as response:  # noqa: S310 - fixed https release URL
        return response.read()


def _release_url(version: str, name: str) -> str:
    if version == "latest":
        return f"{TEMPLATER_RELEASES}/latest/download/{name}"
    return f"{TEMPLATER_RELEASES}/download/{version}/{name}"


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def _cached_release(cache_dir: Path, version: str) -> Path | None:
    if version != "latest":
        folder = cache_dir / version
        return folder if (folder / "main.js").is_file() else None
    versions = (
        [d for d in cache_dir.glob("*") if (d / "main.js").is_file()] if cache_dir.is_dir() else []
    )
    return max(versions, key=lambda d: _version_key(d.name), default=None)


def _check_release(folder: Path, pinned: dict[str, str]) -> dict[str, str]:
    """sha256 of each file; raises if a pinned hash or the plugin id does not match."""
    try:
        manifest = _read_json(folder / "manifest.json", {})
    except ObsidianSettingsError as exc:
        raise TemplaterUnavailable(str(exc)) from exc
    if manifest.get("id") != TEMPLATER_ID:
        raise TemplaterUnavailable(
            f"{folder}: manifest.json nao e do Templater ({manifest.get('id')!r})"
        )
    hashes = {name: _sha256(folder / name) for name in TEMPLATER_FILES if (folder / name).is_file()}
    for name, expected in pinned.items():
        if hashes.get(name) != expected.lower():
            raise TemplaterUnavailable(
                f"sha256 de {name} nao confere com obsidian.templater_sha256 "
                f"(esperado {expected}, obtido {hashes.get(name)})"
            )
    return hashes


def fetch_templater(
    cache_dir: Path,
    version: str,
    pinned: dict[str, str],
    *,
    source: Path | None = None,
    download: Any = None,
) -> tuple[Path, str]:
    """A verified Templater release folder, and where it came from.

    Order: ``source`` (a folder with the release files, for a machine with no
    network), the cache, then the GitHub release, saved to the cache under the
    version its ``manifest.json`` declares.
    """
    if source is not None:
        _check_release(source, pinned)
        return source, f"pasta {source}"
    cached = _cached_release(cache_dir, version)
    if cached is not None:
        _check_release(cached, pinned)
        return cached, f"cache ({cached.name})"
    download = download or _download
    staging = cache_dir / ".download"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for name in TEMPLATER_FILES:
            try:
                (staging / name).write_bytes(download(_release_url(version, name)))
            except Exception as exc:
                if name == "styles.css":
                    continue  # optional in a release
                raise TemplaterUnavailable(f"download de {name} falhou: {exc}") from exc
        _check_release(staging, pinned)
        release = _read_json(staging / "manifest.json", {}).get("version") or version
        target = cache_dir / str(release)
        target.mkdir(parents=True, exist_ok=True)
        for item in staging.iterdir():
            item.replace(target / item.name)
        return target, f"download ({release})"
    finally:
        for item in staging.glob("*"):
            item.unlink()
        staging.rmdir()


def setup_obsidian(
    vault_path: Path,
    cache_path: Path,
    obsidian: Any,
    vault_timezone: str,
    *,
    force: bool = False,
    source: Path | None = None,
    download: Any = None,
) -> SetupReport:
    """Make the vault ready to write notes in Obsidian: templates, Templater, settings.

    Existing Obsidian settings are merged, never replaced. The plugin files come
    from ``fetch_templater``; when it cannot get them the templates and settings
    are still written and the report says how to install the plugin by hand.
    ``force`` regenerates the templates and reinstalls the plugin files.
    """
    templates = write_templates(vault_path, vault_timezone, force=force)
    dot = vault_path / ".obsidian"
    settings: list[Path] = []

    plugins_file = dot / "community-plugins.json"
    enabled = _read_json(plugins_file, [])
    if TEMPLATER_ID not in enabled and _write_json(plugins_file, [*enabled, TEMPLATER_ID]):
        settings.append(plugins_file)

    plugin_dir = dot / "plugins" / TEMPLATER_ID
    data_file = plugin_dir / "data.json"
    merged = templater_settings(_read_json(data_file, {}))
    if _write_json(data_file, merged):
        settings.append(data_file)

    installed = plugin_dir / "main.js"
    if installed.is_file() and not force:
        version = _read_json(plugin_dir / "manifest.json", {}).get("version", "?")
        hashes = {n: _sha256(plugin_dir / n) for n in TEMPLATER_FILES if (plugin_dir / n).is_file()}
        return SetupReport(templates, settings, f"ja instalado (versao {version})", True, hashes)
    try:
        release, origin = fetch_templater(
            cache_path / "obsidian" / TEMPLATER_ID,
            obsidian.templater_version,
            obsidian.templater_sha256,
            source=source,
            download=download,
        )
    except TemplaterUnavailable as exc:
        return SetupReport(
            templates,
            settings,
            f"nao instalado: {exc}. No Obsidian, instale o plugin Templater pela loja "
            "(Community plugins); a configuracao ja esta gravada.",
            False,
            {},
        )
    for name in TEMPLATER_FILES:
        if (release / name).is_file():
            (plugin_dir / name).write_bytes((release / name).read_bytes())
    version = _read_json(plugin_dir / "manifest.json", {}).get("version", "?")
    hashes = {n: _sha256(plugin_dir / n) for n in TEMPLATER_FILES if (plugin_dir / n).is_file()}
    return SetupReport(
        templates, settings, f"instalado de {origin}, versao {version}", True, hashes
    )


def obsidian_health(vault_path: Path) -> tuple[bool, str]:
    """One line for ``zettel doctor``: templates, script and Templater in the vault."""
    missing = [
        name
        for name in (*TEMPLATE_FLOWS, f"scripts/{SCRIPT_NAME}")
        if not (vault_path / TEMPLATES_DIR / name).is_file()
    ]
    plugin_dir = vault_path / ".obsidian" / "plugins" / TEMPLATER_ID
    if missing:
        return False, f"faltam em {TEMPLATES_DIR}/: {', '.join(missing)} (rode obsidian-setup)"
    if not (plugin_dir / "main.js").is_file():
        return False, "Templater nao instalado no vault (rode obsidian-setup)"
    try:
        enabled = _read_json(vault_path / ".obsidian" / "community-plugins.json", [])
        version = _read_json(plugin_dir / "manifest.json", {}).get("version", "?")
    except ObsidianSettingsError as exc:
        return False, str(exc)
    if TEMPLATER_ID not in enabled:
        return False, "Templater instalado mas nao habilitado (rode obsidian-setup)"
    return True, f"templates em {TEMPLATES_DIR}/, Templater {version}"
