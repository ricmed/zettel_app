"""Pack and restore an encrypted ``.zettelbak`` archive.

The file on disk is the blob from :func:`zettel.backup.crypto.encrypt`. Inside,
after decryption, is a zip of relative POSIX members:

- ``manifest.json`` — format version, UTC timestamp, flags, sha256 per member
- ``state.db`` — SQLite snapshot taken with ``Connection.backup`` (not a copy of the live file)
- ``.env`` — the project env file, when it exists
- ``chroma/<path>`` — the Chroma directory, unless ``include_chroma`` is false
- ``inbox/<path>`` — the inbox, only when ``include_inbox`` is true
- ``vault/10_Sources/``, ``vault/20_Literature/``, ``vault/30_Permanent/``,
  ``vault/40_MOCs/``, ``vault/50_Projetos/``, ``vault/90_Assets/``,
  ``vault/99_Templates/``, ``vault/.obsidian/``

``.obsidian`` drops directories named ``cache`` and plugin ``main.js`` /
``styles.css``. ``data/cache``, ``zettel.lock`` and ``.venv`` are never packed.

Restore writes only into the vault, ``state_db_path``, ``chroma_path``,
``inbox_path`` (``inbox/`` members) and the project ``.env``. A member that is
absolute, contains ``..``, a backslash or a drive letter is refused before any
destination file is written.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from zettel.backup.crypto import decrypt, encrypt
from zettel.config import AppConfig
from zettel.state import StateDB

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
STATE_MEMBER = "state.db"
ENV_MEMBER = ".env"
CHROMA_PREFIX = "chroma/"
INBOX_PREFIX = "inbox/"
VAULT_PREFIX = "vault/"

#: Zip members, and the uncompressed bytes of those members.
MAX_MEMBER_COUNT = 100_000
MAX_UNCOMPRESSED_BYTES = 10 * 1024**3

VAULT_FOLDERS = (
    "10_Sources",
    "20_Literature",
    "30_Permanent",
    "40_MOCs",
    "50_Projetos",
    "90_Assets",
    "99_Templates",
)
VAULT_TOP_LEVEL = frozenset((*VAULT_FOLDERS, ".obsidian"))
_PLUGIN_BINARIES = frozenset({"main.js", "styles.css"})
_READ_CHUNK = 1024 * 1024

__all__ = [
    "CHROMA_PREFIX",
    "ENV_MEMBER",
    "FORMAT_VERSION",
    "INBOX_PREFIX",
    "MANIFEST_NAME",
    "MAX_MEMBER_COUNT",
    "MAX_UNCOMPRESSED_BYTES",
    "STATE_MEMBER",
    "VAULT_PREFIX",
    "BackupArchiveError",
    "BackupResult",
    "RestoreResult",
    "create_backup",
    "restore_backup",
]


class BackupArchiveError(Exception):
    """The archive is refused, or the destination path cannot be used."""


@dataclass(frozen=True)
class BackupResult:
    """What ``create_backup`` wrote. Member names include ``manifest.json``."""

    path: Path
    members: tuple[str, ...]
    include_chroma: bool
    include_inbox: bool


@dataclass(frozen=True)
class RestoreResult:
    """What ``restore_backup`` applied.

    ``env_kept`` means an existing ``.env`` was left in place.
    """

    members: tuple[str, ...]
    include_chroma: bool
    include_inbox: bool
    env_written: bool
    env_kept: bool
    session_rotated: bool


def create_backup(
    cfg: AppConfig,
    db: StateDB,
    dest: Path,
    passphrase: str,
    *,
    env_path: Path,
    include_chroma: bool = True,
    include_inbox: bool = False,
) -> BackupResult:
    """Flush retention, snapshot SQLite, and write an encrypted archive to ``dest``."""
    from zettel.retention import flush

    target = Path(dest)
    if target.exists() and target.is_dir():
        raise BackupArchiveError("O destino do backup e um diretorio.")
    if target.resolve() == Path(cfg.state_db_path).resolve():
        raise BackupArchiveError("O destino nao pode ser o banco ao vivo.")

    flush(cfg, db)
    with tempfile.TemporaryDirectory(prefix="zettelbak-") as tmp:
        snapshot = Path(tmp) / STATE_MEMBER
        _snapshot_database(db.conn, snapshot)
        files = _gather(
            cfg,
            Path(env_path),
            snapshot,
            target,
            include_chroma=include_chroma,
            include_inbox=include_inbox,
        )
        payloads = [(name, path.read_bytes()) for name, path in files]
    members = _with_manifest(payloads, include_chroma=include_chroma, include_inbox=include_inbox)
    _publish_restricted(target, encrypt(_zip_bytes(members), passphrase))
    return BackupResult(
        path=target,
        members=tuple(name for name, _data in members),
        include_chroma=include_chroma,
        include_inbox=include_inbox,
    )


def restore_backup(
    cfg: AppConfig,
    source: Path,
    passphrase: str,
    *,
    env_path: Path,
    force_env: bool = False,
    rotate_session: bool = False,
) -> RestoreResult:
    """Decrypt ``source`` and write it into this project's vault, database, Chroma and ``.env``.

    Refuses a path-traversal member, an oversized archive, or any destination
    outside the vault, the state database, Chroma, the inbox and the project
    ``.env`` — and writes nothing when it refuses. Then recreates vault files
    that are in the database but missing on disk.
    """
    from zettel.rebuild import run_rebuild_vault

    archive = Path(source)
    if not archive.is_file():
        raise BackupArchiveError("Arquivo de backup nao encontrado.")
    manifest, payloads = _read_archive(decrypt(archive.read_bytes(), passphrase))
    plan = _plan_restore(cfg, Path(env_path), payloads)
    env_written, env_kept = _write_plan(
        plan, payloads, env_path=Path(env_path), force_env=force_env
    )

    database = StateDB(cfg.state_db_path)
    try:
        if rotate_session:
            database.rotate_web_session_key()
        run_rebuild_vault(cfg, database, force=False)
    finally:
        database.close()
    return RestoreResult(
        members=tuple(payloads),
        include_chroma=bool(manifest["include_chroma"]),
        include_inbox=bool(manifest["include_inbox"]),
        env_written=env_written,
        env_kept=env_kept,
        session_rotated=rotate_session,
    )


def _snapshot_database(conn: sqlite3.Connection, dest: Path) -> None:
    """Copy ``conn`` with the SQLite backup API so a pending WAL is not torn."""
    if dest.exists():
        dest.unlink()
    target = sqlite3.connect(str(dest))
    try:
        target.execute("PRAGMA journal_mode=DELETE")
        conn.backup(target)
        target.commit()
    finally:
        target.close()


def _gather(
    cfg: AppConfig,
    env_path: Path,
    snapshot: Path,
    dest: Path,
    *,
    include_chroma: bool,
    include_inbox: bool,
) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = [(STATE_MEMBER, snapshot)]
    if env_path.is_file():
        found.append((ENV_MEMBER, env_path))
    vault = Path(cfg.vault_path)
    for folder in VAULT_FOLDERS:
        found.extend(_files_under(vault / folder, f"{VAULT_PREFIX}{folder}"))
    found.extend(_files_under(vault / ".obsidian", f"{VAULT_PREFIX}.obsidian", obsidian=True))
    if include_chroma:
        found.extend(_files_under(Path(cfg.chroma_path), "chroma"))
    if include_inbox:
        found.extend(_files_under(Path(cfg.inbox_path), "inbox"))

    cache = Path(cfg.cache_path).resolve()
    blocked = dest.resolve()
    kept: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for name, path in found:
        resolved = path.resolve()
        if resolved == blocked or _within(resolved, cache):
            continue
        if path.name == "zettel.lock" or ".venv" in resolved.parts:
            continue
        if name in seen:
            raise BackupArchiveError("Nome de membro repetido ao montar o backup.")
        seen.add(name)
        kept.append((name, resolved))
    kept.sort(key=lambda item: item[0])
    return kept


def _files_under(root: Path, prefix: str, *, obsidian: bool = False) -> list[tuple[str, Path]]:
    root_resolved = root.resolve()
    if not root_resolved.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for path in _walk_files(root_resolved, drop_cache_dirs=obsidian):
        relative = path.relative_to(root_resolved).as_posix()
        if obsidian and _plugin_binary(relative):
            continue
        found.append((f"{prefix}/{relative}", path))
    return found


def _walk_files(root: Path, *, drop_cache_dirs: bool) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if not _within(current, root):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if name != ".venv" and not (drop_cache_dirs and name == "cache")
        ]
        for filename in filenames:
            path = current / filename
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.is_file() and _within(resolved, root):
                yield resolved


def _plugin_binary(relative: str) -> bool:
    pure = PurePosixPath(relative)
    return "plugins" in pure.parts and pure.name in _PLUGIN_BINARIES


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _with_manifest(
    files: list[tuple[str, bytes]],
    *,
    include_chroma: bool,
    include_inbox: bool,
) -> list[tuple[str, bytes]]:
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "format_version": FORMAT_VERSION,
        "include_chroma": include_chroma,
        "include_inbox": include_inbox,
        "members": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in files
        ],
    }
    encoded = json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")
    return [*files, (MANIFEST_NAME, encoded)]


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for name, data in members:
            info = zipfile.ZipInfo(filename=name, date_time=(2026, 1, 1, 0, 0, 0))
            info.flag_bits |= 0x800
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buffer.getvalue()


def _read_archive(plain: bytes) -> tuple[dict[str, object], dict[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(plain))
    except zipfile.BadZipFile as exc:
        raise BackupArchiveError("O backup nao contem um pacote valido.") from exc
    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(infos) > MAX_MEMBER_COUNT:
            raise BackupArchiveError("O backup tem membros demais.")
        declared = sum(info.file_size for info in infos)
        if declared > MAX_UNCOMPRESSED_BYTES:
            raise BackupArchiveError("O backup descompactado passa do teto de tamanho.")
        names = [info.filename for info in infos]
        if len(set(names)) != len(names):
            raise BackupArchiveError("O backup repete um membro.")
        for name in names:
            _validate_member_name(name)
        payloads: dict[str, bytes] = {}
        total = 0
        for info in infos:
            with zf.open(info, "r") as handle:
                data = _read_capped(handle, MAX_UNCOMPRESSED_BYTES - total)
            total += len(data)
            payloads[info.filename] = data
    return _parse_manifest(payloads), payloads


def _read_capped(handle: io.BufferedIOBase, remaining: int) -> bytes:
    if remaining < 0:
        raise BackupArchiveError("O backup descompactado passa do teto de tamanho.")
    chunks: list[bytes] = []
    left = remaining
    while left > 0:
        block = handle.read(min(_READ_CHUNK, left))
        if not block:
            return b"".join(chunks)
        chunks.append(block)
        left -= len(block)
    extra = handle.read(1)
    if extra:
        raise BackupArchiveError("O backup descompactado passa do teto de tamanho.")
    return b"".join(chunks)


def _validate_member_name(name: str) -> None:
    """Refuse absolute paths, ``..``, backslash escapes and drive letters."""
    if not name or name.endswith("/") or "\\" in name or "\x00" in name or "//" in name:
        raise BackupArchiveError("Caminho de membro recusado.")
    if name.startswith(("/", "./", "../")):
        raise BackupArchiveError("Caminho de membro recusado.")
    pure = PurePosixPath(name)
    if pure.is_absolute():
        raise BackupArchiveError("Caminho de membro recusado.")
    for part in pure.parts:
        if part in {"..", "."}:
            raise BackupArchiveError("Caminho de membro recusado.")
        if len(part) >= 2 and part[0].isalpha() and part[1] == ":":
            raise BackupArchiveError("Caminho de membro recusado.")


def _parse_manifest(payloads: dict[str, bytes]) -> dict[str, object]:
    if MANIFEST_NAME not in payloads:
        raise BackupArchiveError("O backup nao tem manifest.json.")
    if STATE_MEMBER not in payloads:
        raise BackupArchiveError("O backup nao tem state.db.")
    try:
        parsed = json.loads(payloads[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupArchiveError("manifest.json invalido.") from exc
    if not isinstance(parsed, dict):
        raise BackupArchiveError("manifest.json invalido.")
    version = parsed.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != FORMAT_VERSION:
        raise BackupArchiveError("Versao de backup nao suportada.")
    if not isinstance(parsed.get("include_chroma"), bool) or not isinstance(
        parsed.get("include_inbox"), bool
    ):
        raise BackupArchiveError("manifest.json invalido.")
    created = parsed.get("created_at")
    if not isinstance(created, str) or not created:
        raise BackupArchiveError("manifest.json invalido.")
    members = parsed.get("members")
    if not isinstance(members, list):
        raise BackupArchiveError("manifest.json invalido.")
    listed: dict[str, str] = {}
    for item in members:
        if not isinstance(item, dict):
            raise BackupArchiveError("manifest.json invalido.")
        member_path = item.get("path")
        digest = item.get("sha256")
        if (
            not isinstance(member_path, str)
            or not isinstance(digest, str)
            or member_path == MANIFEST_NAME
            or member_path in listed
        ):
            raise BackupArchiveError("manifest.json invalido.")
        listed[member_path] = digest
    if set(listed) != set(payloads) - {MANIFEST_NAME}:
        raise BackupArchiveError("O manifest nao bate com o pacote.")
    for member_path, digest in listed.items():
        if hashlib.sha256(payloads[member_path]).hexdigest() != digest:
            raise BackupArchiveError("Integridade do backup falhou.")
    return parsed


def _plan_restore(
    cfg: AppConfig, env_path: Path, payloads: dict[str, bytes]
) -> dict[str, Path | None]:
    plan: dict[str, Path | None] = {}
    claimed: dict[Path, str] = {}
    for name in payloads:
        dest = _destination(cfg, env_path, name)
        plan[name] = dest
        if dest is None:
            continue
        key = dest.resolve()
        previous = claimed.get(key)
        if previous is not None:
            raise BackupArchiveError("Dois membros apontam para o mesmo destino.")
        claimed[key] = name
    return plan


def _destination(cfg: AppConfig, env_path: Path, name: str) -> Path | None:
    if name == MANIFEST_NAME:
        return None
    if name == STATE_MEMBER:
        return Path(cfg.state_db_path)
    if name == ENV_MEMBER:
        return Path(env_path)
    if name.startswith(CHROMA_PREFIX):
        return _contained(Path(cfg.chroma_path), name.removeprefix(CHROMA_PREFIX))
    if name.startswith(INBOX_PREFIX):
        return _contained(Path(cfg.inbox_path), name.removeprefix(INBOX_PREFIX))
    if name.startswith(VAULT_PREFIX):
        relative = name.removeprefix(VAULT_PREFIX)
        top = relative.split("/", 1)[0]
        if top not in VAULT_TOP_LEVEL:
            raise BackupArchiveError("Destino nao permitido.")
        return _contained(Path(cfg.vault_path), relative)
    raise BackupArchiveError("Destino nao permitido.")


def _contained(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or ".." in PurePosixPath(relative).parts:
        raise BackupArchiveError("Caminho de membro recusado.")
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise BackupArchiveError("Caminho de membro recusado.") from exc
    if candidate == base:
        raise BackupArchiveError("Caminho de membro recusado.")
    return candidate


def _write_plan(
    plan: dict[str, Path | None],
    payloads: dict[str, bytes],
    *,
    env_path: Path,
    force_env: bool,
) -> tuple[bool, bool]:
    env_written = False
    env_kept = False
    env_target = env_path.resolve()
    for name, dest in plan.items():
        if dest is None:
            continue
        if name == ENV_MEMBER and dest.resolve() == env_target and dest.exists() and not force_env:
            env_kept = True
            continue
        _install(
            dest,
            payloads[name],
            restricted=name == ENV_MEMBER,
            sqlite=name == STATE_MEMBER,
        )
        if name == ENV_MEMBER:
            env_written = True
    return env_written, env_kept


def _install(path: Path, data: bytes, *, restricted: bool, sqlite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.zettelbak-partial")
    if restricted:
        _write_restricted(temporary, data)
    else:
        temporary.write_bytes(data)
    try:
        if sqlite:
            _discard_sqlite_sidecars(path)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    if restricted:
        _chmod_0600(path)


def _discard_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_file():
            sidecar.unlink()


def _publish_restricted(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    _write_restricted(temporary, data)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    _chmod_0600(path)


def _write_restricted(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if os.name == "nt":
        flags |= os.O_BINARY
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    _chmod_0600(path)


def _chmod_0600(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        if os.name == "posix":
            raise
