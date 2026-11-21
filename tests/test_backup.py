"""Backup and restore. Fake passphrase, temporary vault, no network and no real .env."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from zettel.backup.archive import (
    MAX_MEMBER_COUNT,
    MAX_UNCOMPRESSED_BYTES,
    BackupArchiveError,
    _validate_member_name,
    _with_manifest,
    _zip_bytes,
    create_backup,
    restore_backup,
)
from zettel.backup.crypto import MAGIC, BackupCryptoError, decrypt, encrypt
from zettel.config import AppConfig
from zettel.state import StateDB

PHRASE = "frase-ficticia"
OTHER = "outra-frase-ficticia"
SECRET = b"ZETTEL_BACKUP_FIXTURE=valor-ficticio-413\n"
NOTE = b"# Ideia\n\nTexto da nota.\n"
ASSET = b"\x89PNG\r\n\x1a\nfake-asset"
TEMPLATE = b"template-ficticio\n"
SESSION = "sess-FICTICIA-413-original"
CACHE_BYTES = b"CACHE-FICTICIO-413"
_NO_PASSPHRASE_FLAG = re.compile(r"--passphrase(?!-file)\b")


@dataclass
class _Layout:
    root: Path
    cfg: AppConfig
    config_path: Path
    env_path: Path
    note: Path
    asset: Path
    template: Path


def _cfg(root: Path) -> AppConfig:
    return AppConfig(
        vault_path=(root / "vault").resolve(),
        inbox_path=(root / "data" / "inbox").resolve(),
        chroma_path=(root / "data" / "chroma").resolve(),
        state_db_path=(root / "data" / "state.db").resolve(),
        cache_path=(root / "data" / "cache").resolve(),
        prompts_path=(root / "prompts").resolve(),
    )


def _config_file(root: Path) -> Path:
    path = root / "config" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _phrase_file(path: Path, text: str = PHRASE) -> Path:
    path.write_text(text + "\n", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    return path


def _layout(root: Path, *, session: str = SESSION) -> _Layout:
    cfg = _cfg(root)
    _config_file(root)
    vault = cfg.vault_path
    for folder in (
        "10_Sources",
        "20_Literature",
        "30_Permanent",
        "40_MOCs",
        "50_Projetos",
        "90_Assets",
        "99_Templates",
    ):
        (vault / folder).mkdir(parents=True)
    note = vault / "30_Permanent" / "nota.md"
    asset = vault / "90_Assets" / "img.bin"
    template = vault / "99_Templates" / "ZTL.md"
    note.write_bytes(NOTE)
    asset.write_bytes(ASSET)
    template.write_bytes(TEMPLATE)
    obsidian = vault / ".obsidian"
    plugin = obsidian / "plugins" / "templater-obsidian"
    plugin.mkdir(parents=True)
    (obsidian / "community-plugins.json").write_text("[]", encoding="utf-8")
    (obsidian / "app.json").write_text("{}", encoding="utf-8")
    (plugin / "data.json").write_text('{"on":true}', encoding="utf-8")
    (plugin / "main.js").write_text("plugin-binary", encoding="utf-8")
    (plugin / "styles.css").write_text("plugin-css", encoding="utf-8")
    (plugin / "cache").mkdir()
    (plugin / "cache" / "blob.bin").write_bytes(b"plugin-cache")
    (vault / "zettel.lock").write_text("lock", encoding="utf-8")
    (vault / ".venv").mkdir()
    (vault / ".venv" / "pyvenv.cfg").write_text("home = nowhere", encoding="utf-8")
    cfg.chroma_path.mkdir(parents=True)
    (cfg.chroma_path / "marker.bin").write_bytes(b"chroma-bytes")
    cfg.inbox_path.mkdir(parents=True)
    (cfg.inbox_path / "paper.md").write_text("entrada", encoding="utf-8")
    cfg.cache_path.mkdir(parents=True)
    (cfg.cache_path / "scratch.bin").write_bytes(CACHE_BYTES)
    (cfg.state_db_path.parent / "zettel.lock").write_text("lock", encoding="utf-8")
    env_path = root / ".env"
    env_path.write_bytes(SECRET)
    db = StateDB(cfg.state_db_path)
    try:
        db.conn.execute(
            "INSERT INTO web_auth (id, session_key, rotated_at) VALUES (1, ?, ?)",
            (session, "2026-01-01T00:00:00"),
        )
        db.conn.commit()
    finally:
        db.close()
    return _Layout(root, cfg, root / "config" / "config.yaml", env_path, note, asset, template)


def _open_archive(blob: bytes, passphrase: str = PHRASE) -> dict[str, bytes]:
    plain = decrypt(blob, passphrase)
    with zipfile.ZipFile(io.BytesIO(plain)) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist() if not info.is_dir()}


def _session_key(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT session_key FROM web_auth WHERE id=1").fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def _invoke(args: list[str]):
    from typer.testing import CliRunner
    from zettel.cli import app

    return CliRunner().invoke(app, args)


def test_roundtrip_restores_note_asset_template_env_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    src = _layout(tmp_path / "src")
    dest_root = tmp_path / "dest"
    _config_file(dest_root)
    archive = tmp_path / "acervo.zettelbak"
    phrase = _phrase_file(tmp_path / "phrase.txt")
    modes: list[tuple[Path, int]] = []
    real_chmod = os.chmod

    def _spy(path, mode, *args, **kwargs):
        modes.append((Path(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", _spy)
    backup_out = _invoke(
        ["backup", str(archive), "--config", str(src.config_path), "--passphrase-file", str(phrase)]
    )
    assert backup_out.exit_code == 0, backup_out.output
    blob = archive.read_bytes()
    assert blob.startswith(MAGIC)
    assert not blob.startswith(b"PK")
    members = _open_archive(blob)
    assert members["vault/30_Permanent/nota.md"] == NOTE
    assert members["vault/90_Assets/img.bin"] == ASSET
    assert members["vault/99_Templates/ZTL.md"] == TEMPLATE
    assert members[".env"] == SECRET
    assert members["chroma/marker.bin"] == b"chroma-bytes"
    assert members["vault/.obsidian/community-plugins.json"] == b"[]"
    assert b'{"on":true}' in members["vault/.obsidian/plugins/templater-obsidian/data.json"]
    assert _session_key_from_file_bytes(members["state.db"]) == SESSION
    manifest = json.loads(members["manifest.json"])
    assert manifest["format_version"] == 1
    assert manifest["include_chroma"] is True
    assert manifest["include_inbox"] is False
    assert datetime.fromisoformat(manifest["created_at"]).tzinfo is not None
    listed = {item["path"]: item["sha256"] for item in manifest["members"]}
    assert listed[".env"] == hashlib.sha256(SECRET).hexdigest()
    raw_manifest = members["manifest.json"].decode("utf-8")
    assert b"valor-ficticio-413".decode() not in raw_manifest
    assert PHRASE not in raw_manifest
    assert not any(name.endswith(("main.js", "styles.css")) for name in members)
    assert not any("cache/" in name or name.endswith("scratch.bin") for name in members)
    assert not any(name.endswith("zettel.lock") or ".venv/" in name for name in members)
    assert "inbox/paper.md" not in members
    assert SECRET.decode() not in backup_out.output
    assert PHRASE not in backup_out.output
    assert "zettel reindex" not in backup_out.output

    restore_out = _invoke(
        [
            "restore",
            str(archive),
            "--config",
            str(dest_root / "config" / "config.yaml"),
            "--passphrase-file",
            str(phrase),
        ]
    )
    assert restore_out.exit_code == 0, restore_out.output
    dest = _cfg(dest_root)
    assert (dest.vault_path / "30_Permanent" / "nota.md").read_bytes() == NOTE
    assert (dest.vault_path / "90_Assets" / "img.bin").read_bytes() == ASSET
    assert (dest.vault_path / "99_Templates" / "ZTL.md").read_bytes() == TEMPLATE
    assert (dest_root / ".env").read_bytes() == SECRET
    assert (dest.chroma_path / "marker.bin").read_bytes() == b"chroma-bytes"
    assert not (
        dest.vault_path / ".obsidian" / "plugins" / "templater-obsidian" / "main.js"
    ).exists()
    assert _session_key(dest.state_db_path) == SESSION
    assert SECRET.decode() not in restore_out.output
    assert PHRASE not in restore_out.output
    env_modes = [mode for path, mode in modes if path == (dest_root / ".env")]
    archive_modes = [mode for path, mode in modes if path == archive]
    assert 0o600 in env_modes
    assert 0o600 in archive_modes
    if os.name == "posix":
        assert stat.S_IMODE((dest_root / ".env").stat().st_mode) == 0o600
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def _session_key_from_file_bytes(blob: bytes) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.db"
        path.write_bytes(blob)
        return _session_key(path)


def test_flush_runs_before_the_sqlite_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg(tmp_path)
    cfg.state_db_path.parent.mkdir(parents=True, exist_ok=True)
    db = StateDB(cfg.state_db_path)
    order: list[str] = []

    def _flush(_cfg, database):
        order.append("flush")
        database.conn.execute("CREATE TABLE flush_marker (n INTEGER)")
        database.conn.execute("INSERT INTO flush_marker VALUES (7)")
        database.conn.commit()
        return {"notes": 1}

    from zettel.backup import archive as archive_mod

    real_snapshot = archive_mod._snapshot_database

    def _spy(conn, dest):
        order.append("snapshot")
        found = conn.execute("SELECT n FROM flush_marker").fetchone()
        assert found[0] == 7
        real_snapshot(conn, dest)

    monkeypatch.setattr("zettel.retention.flush", _flush)
    monkeypatch.setattr(archive_mod, "_snapshot_database", _spy)
    archive = tmp_path / "a.zettelbak"
    try:
        create_backup(cfg, db, archive, PHRASE, env_path=tmp_path / ".env")
    finally:
        db.close()
    assert order.index("flush") < order.index("snapshot")
    snap = tmp_path / "snap.db"
    snap.write_bytes(_open_archive(archive.read_bytes())["state.db"])
    conn = sqlite3.connect(snap)
    try:
        assert conn.execute("SELECT n FROM flush_marker").fetchone()[0] == 7
    finally:
        conn.close()


def test_snapshot_uses_sqlite_backup_and_keeps_uncheckpointed_wal(tmp_path: Path):
    """A copy of the live file misses WAL pages; the snapshot file has them.

    ``wal_autocheckpoint=0`` leaves the new row only in the WAL. The backup API
    does not checkpoint the source, so the live file still lacks that row after
    the snapshot. Copying the live file would miss it too.
    """
    cfg = _cfg(tmp_path)
    db = StateDB(cfg.state_db_path)
    db.conn.execute("PRAGMA wal_autocheckpoint=0")
    db.conn.execute(
        "INSERT INTO web_auth (id, session_key, rotated_at) VALUES (1, ?, ?)",
        ("wal-key", "t"),
    )
    db.conn.commit()
    assert db.conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
    assert Path(str(cfg.state_db_path) + "-wal").is_file()
    archive = tmp_path / "wal.zettelbak"
    try:
        create_backup(cfg, db, archive, PHRASE, env_path=tmp_path / "missing.env")
        plain = tmp_path / "plain.db"
        plain.write_bytes(cfg.state_db_path.read_bytes())
    finally:
        db.close()
    copied = sqlite3.connect(plain)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            copied.execute("SELECT session_key FROM web_auth WHERE id=1").fetchone()
    finally:
        copied.close()
    archived = _open_archive(archive.read_bytes())["state.db"]
    assert _session_key_from_file_bytes(archived) == "wal-key"


def test_path_traversal_writes_nothing(tmp_path: Path):
    escaped = "zettelbak-escaped-413.txt"
    outside = Path.cwd() / escaped
    dest = tmp_path / "dest"
    cfg = _cfg(dest)
    cases = [
        f"../{escaped}",
        f"vault/10_Sources/../../{escaped}",
        f"..\\{escaped}",
        (tmp_path / escaped).as_posix(),
        "/tmp/zettelbak-escaped-413.txt",
        "data/cache/scratch.bin",
        "nota.md",
    ]
    try:
        for member in cases:
            blob = encrypt(
                _zip_bytes(
                    _with_manifest(
                        [(member, b"pwned"), ("state.db", b"db")],
                        include_chroma=False,
                        include_inbox=False,
                    )
                ),
                PHRASE,
            )
            archive = tmp_path / "bad.zettelbak"
            archive.write_bytes(blob)
            before = {path for path in tmp_path.rglob("*") if path.is_file()}
            with pytest.raises(BackupArchiveError):
                restore_backup(cfg, archive, PHRASE, env_path=dest / ".env")
            after = {path for path in tmp_path.rglob("*") if path.is_file()}
            assert after == before
            assert not (tmp_path / escaped).exists()
            assert not outside.exists()
            assert not cfg.state_db_path.exists()
    finally:
        if outside.exists():
            outside.unlink()


def test_escaping_member_names_are_refused():
    for name in (
        "/etc/passwd",
        "C:/Windows/system.ini",
        "..\\outside.txt",
        "vault/../../outside.txt",
        "a/../b",
        "chroma/../../.env",
    ):
        with pytest.raises(BackupArchiveError):
            _validate_member_name(name)
    _validate_member_name("vault/30_Permanent/nota.md")
    _validate_member_name("state.db")
    _validate_member_name(".env")
    _validate_member_name("chroma/marker.bin")
    _validate_member_name("manifest.json")


def test_member_and_size_caps_write_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert MAX_MEMBER_COUNT == 100_000
    assert MAX_UNCOMPRESSED_BYTES == 10 * 1024**3
    dest = tmp_path / "dest"
    cfg = _cfg(dest)
    archive = tmp_path / "caps.zettelbak"

    monkeypatch.setattr("zettel.backup.archive.MAX_MEMBER_COUNT", 2)
    packed = [("state.db", b"db"), *[(f"vault/90_Assets/{i}.bin", b"x") for i in range(4)]]
    archive.write_bytes(
        encrypt(
            _zip_bytes(_with_manifest(packed, include_chroma=False, include_inbox=False)),
            PHRASE,
        )
    )
    with pytest.raises(BackupArchiveError):
        restore_backup(cfg, archive, PHRASE, env_path=dest / ".env")
    assert not cfg.state_db_path.exists()

    monkeypatch.setattr("zettel.backup.archive.MAX_MEMBER_COUNT", MAX_MEMBER_COUNT)
    monkeypatch.setattr("zettel.backup.archive.MAX_UNCOMPRESSED_BYTES", 8)
    archive.write_bytes(
        encrypt(
            _zip_bytes(
                _with_manifest([("state.db", b"x" * 64)], include_chroma=False, include_inbox=False)
            ),
            PHRASE,
        )
    )
    with pytest.raises(BackupArchiveError):
        restore_backup(cfg, archive, PHRASE, env_path=dest / ".env")
    assert not cfg.state_db_path.exists()
    assert list(dest.rglob("*")) == []


def test_existing_env_is_kept_unless_force_env(tmp_path: Path):
    src = _layout(tmp_path / "src")
    kept = tmp_path / "kept"
    forced = tmp_path / "forced"
    _config_file(kept)
    _config_file(forced)
    archive = tmp_path / "env.zettelbak"
    phrase = _phrase_file(tmp_path / "phrase.txt")
    backup_out = _invoke(
        ["backup", str(archive), "--config", str(src.config_path), "--passphrase-file", str(phrase)]
    )
    assert backup_out.exit_code == 0, backup_out.output
    old = b"KEEP=valor-antigo\n"
    (kept / ".env").write_bytes(old)
    (forced / ".env").write_bytes(old)
    kept_out = _invoke(
        [
            "restore",
            str(archive),
            "--config",
            str(kept / "config" / "config.yaml"),
            "--passphrase-file",
            str(phrase),
        ]
    )
    assert kept_out.exit_code == 0, kept_out.output
    assert (kept / ".env").read_bytes() == old
    assert b"valor-ficticio-413".decode() not in kept_out.output
    assert "--force-env" in kept_out.output
    forced_out = _invoke(
        [
            "restore",
            str(archive),
            "--config",
            str(forced / "config" / "config.yaml"),
            "--passphrase-file",
            str(phrase),
            "--force-env",
        ]
    )
    assert forced_out.exit_code == 0, forced_out.output
    assert (forced / ".env").read_bytes() == SECRET
    assert b"valor-ficticio-413".decode() not in forced_out.output
    assert PHRASE not in forced_out.output


def test_wrong_passphrase_fails_without_echoing_secrets(tmp_path: Path):
    archive = tmp_path / "blob.zettelbak"
    archive.write_bytes(encrypt(b"not-a-zip", PHRASE))
    phrase = _phrase_file(tmp_path / "phrase.txt", OTHER)
    dest = tmp_path / "dest"
    config = _config_file(dest)
    result = _invoke(
        ["restore", str(archive), "--config", str(config), "--passphrase-file", str(phrase)]
    )
    assert result.exit_code == 1
    assert "senha incorreta ou arquivo danificado" in result.output
    assert PHRASE not in result.output
    assert OTHER not in result.output
    assert "valor-ficticio" not in result.output
    assert not (dest / "data" / "state.db").exists()
    with pytest.raises(BackupCryptoError):
        decrypt(archive.read_bytes(), OTHER)


def test_no_chroma_omits_chroma_and_records_it(tmp_path: Path):
    src = _layout(tmp_path / "src")
    dest = tmp_path / "dest"
    _config_file(dest)
    archive = tmp_path / "no-chroma.zettelbak"
    phrase = _phrase_file(tmp_path / "phrase.txt")
    backup_out = _invoke(
        [
            "backup",
            str(archive),
            "--config",
            str(src.config_path),
            "--passphrase-file",
            str(phrase),
            "--no-chroma",
        ]
    )
    assert backup_out.exit_code == 0, backup_out.output
    members = _open_archive(archive.read_bytes())
    assert not any(name.startswith("chroma/") for name in members)
    manifest = json.loads(members["manifest.json"])
    assert manifest["include_chroma"] is False
    restore_out = _invoke(
        [
            "restore",
            str(archive),
            "--config",
            str(dest / "config" / "config.yaml"),
            "--passphrase-file",
            str(phrase),
        ]
    )
    assert restore_out.exit_code == 0, restore_out.output
    assert "zettel reindex" in restore_out.output
    assert not (dest / "data" / "chroma").exists()
    assert (dest / "vault" / "30_Permanent" / "nota.md").read_bytes() == NOTE


def test_with_inbox_includes_inbox_and_cache_stays_out(tmp_path: Path):
    src = _layout(tmp_path / "src")
    phrase = _phrase_file(tmp_path / "phrase.txt")
    plain = tmp_path / "plain.zettelbak"
    with_inbox = tmp_path / "inbox.zettelbak"
    base = ["--config", str(src.config_path), "--passphrase-file", str(phrase)]
    assert _invoke(["backup", str(plain), *base]).exit_code == 0
    assert _invoke(["backup", str(with_inbox), *base, "--with-inbox"]).exit_code == 0
    without = _open_archive(plain.read_bytes())
    included = _open_archive(with_inbox.read_bytes())
    assert "inbox/paper.md" not in without
    assert json.loads(without["manifest.json"])["include_inbox"] is False
    assert included["inbox/paper.md"] == b"entrada"
    assert json.loads(included["manifest.json"])["include_inbox"] is True
    for members in (without, included):
        assert all(CACHE_BYTES not in payload for payload in members.values())
        assert not any("scratch.bin" in name or name.endswith("zettel.lock") for name in members)
    dest = tmp_path / "dest"
    _config_file(dest)
    restore_out = _invoke(
        [
            "restore",
            str(with_inbox),
            "--config",
            str(dest / "config" / "config.yaml"),
            "--passphrase-file",
            str(phrase),
        ]
    )
    assert restore_out.exit_code == 0, restore_out.output
    assert (dest / "data" / "inbox" / "paper.md").read_text(encoding="utf-8") == "entrada"
    assert not (dest / "data" / "cache").exists()


def test_rotate_session_is_off_by_default(tmp_path: Path):
    src = _layout(tmp_path / "src")
    archive = tmp_path / "sess.zettelbak"
    phrase = _phrase_file(tmp_path / "phrase.txt")
    assert (
        _invoke(
            [
                "backup",
                str(archive),
                "--config",
                str(src.config_path),
                "--passphrase-file",
                str(phrase),
            ]
        ).exit_code
        == 0
    )
    kept = tmp_path / "kept"
    rotated = tmp_path / "rotated"
    _config_file(kept)
    _config_file(rotated)
    kept_out = _invoke(
        [
            "restore",
            str(archive),
            "--config",
            str(kept / "config" / "config.yaml"),
            "--passphrase-file",
            str(phrase),
        ]
    )
    assert kept_out.exit_code == 0, kept_out.output
    assert _session_key(kept / "data" / "state.db") == SESSION
    assert SESSION not in kept_out.output
    rotated_out = _invoke(
        [
            "restore",
            str(archive),
            "--config",
            str(rotated / "config" / "config.yaml"),
            "--passphrase-file",
            str(phrase),
            "--rotate-session",
        ]
    )
    assert rotated_out.exit_code == 0, rotated_out.output
    new_key = _session_key(rotated / "data" / "state.db")
    assert new_key != SESSION
    assert new_key not in rotated_out.output
    assert SESSION not in rotated_out.output


def test_rebuild_runs_after_restore_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    src = _layout(tmp_path / "src")
    archive = tmp_path / "rebuild.zettelbak"
    db = StateDB(src.cfg.state_db_path)
    try:
        create_backup(src.cfg, db, archive, PHRASE, env_path=src.env_path)
    finally:
        db.close()
    seen: list[bool] = []

    def _rebuild(_cfg, _db, force=False, dry_run=False):
        seen.append(force)
        assert dry_run is False
        return {"written": 0}

    monkeypatch.setattr("zettel.rebuild.run_rebuild_vault", _rebuild)
    dest = tmp_path / "dest"
    restore_backup(_cfg(dest), archive, PHRASE, env_path=dest / ".env")
    assert seen == [False]
    assert (_cfg(dest).vault_path / "30_Permanent" / "nota.md").read_bytes() == NOTE


def test_help_lists_backup_and_restore_without_a_passphrase_option():
    root = _invoke(["--help"])
    assert root.exit_code == 0, root.output
    assert "backup" in root.output
    assert "restore" in root.output
    for command in ("backup", "restore"):
        result = _invoke([command, "--help"])
        assert result.exit_code == 0, result.output
        assert "--passphrase-file" in result.output
        assert _NO_PASSPHRASE_FLAG.search(result.output) is None
        assert "--password" not in result.output
    backup_help = _invoke(["backup", "--help"]).output
    restore_help = _invoke(["restore", "--help"]).output
    assert "--no-chroma" in backup_help
    assert "--with-inbox" in backup_help
    assert "--force-env" in restore_help
    assert "--rotate-session" in restore_help
    assert "--force-env" not in backup_help
    assert "--rotate-session" not in backup_help


def test_noninteractive_stdin_requires_passphrase_file(tmp_path: Path, monkeypatch):
    def fail_getpass(*_args, **_kwargs):
        raise AssertionError("getpass nao deve ser chamado")

    monkeypatch.setattr("getpass.getpass", fail_getpass)
    config = _config_file(tmp_path)
    result = _invoke(["backup", str(tmp_path / "out.zettelbak"), "--config", str(config)])
    assert result.exit_code == 2
    assert "--passphrase-file" in result.output
    assert not (tmp_path / "out.zettelbak").exists()


def test_backup_and_restore_take_the_operation_lock(tmp_path: Path):
    from zettel.cli.deps import MUTATING_COMMANDS
    from zettel.oplock import OperationLock

    assert {"backup", "restore"} <= set(MUTATING_COMMANDS)
    config = _config_file(tmp_path)
    held = OperationLock(tmp_path / "data" / "zettel.lock")
    held.acquire("garden", "web")
    try:
        for command in ("backup", "restore"):
            result = _invoke([command, str(tmp_path / "x.zettelbak"), "--config", str(config)])
            assert result.exit_code == 1, result.output
            assert "garden" in result.output
            assert "web" in result.output
    finally:
        held.release()
