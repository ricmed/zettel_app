# ADR-072: Encrypted Backup of the Vault, SQLite and Chroma

**Status:** Accepted
**Date:** 2026-11-21
**Issue:** #415
**Depends on:** [ADR-005: Dual-Store Persistence Without Cross-Store Transactions](./ADR-005-dual-store-persistence.md), [ADR-001: SQLite with WAL and FTS5](./ADR-001-sqlite-wal-fts5-primary-persistence.md)
**Related to:** [ADR-030: Manual Notes Adopted at Sync](../MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md), [ADR-063: Web Login Without a Secret Oracle](../WEB/ADR-063-web-login-without-secret-oracle.md), [ADR-067: The Mesa](../WEB/ADR-067-mesa-writing-desk.md)

## Context and Problem Statement

SQLite and Chroma are independent stores ([ADR-005](./ADR-005-dual-store-persistence.md)). Copying one and not the other, or copying `state.db` while a WAL is still open, does not produce a pair that can be restored. The vault is not a pure rendering of the database either: image bytes live only in `90_Assets/`, and until this change a hand-edited note could not be recreated from SQLite.

The same package has to carry secrets. `.env` holds API keys and `SESSION_SECRET`. `web_auth.session_key` lives inside `state.db` and is what signs the web cookie ([ADR-063](../WEB/ADR-063-web-login-without-secret-oracle.md)). A cleartext zip, or a zip that encrypts only `.env`, would leave notes and a session key readable to anyone who obtains the file.

## Decision Drivers

* One snapshot must cover both stores, the vault folders a reader actually needs, and `.env` when it exists.
* The passphrase must not appear on the command line (process list, shell history).
* A leaked file must not be readable without the passphrase. Notes, API keys and the session key travel together, so the whole archive is encrypted, not selected members.
* Restore must refuse a hostile path before it writes anything.
* Image bytes stay files. SQLite retains markdown so a missing note can be recreated; it does not become a blob store.
* There is no web upload of this archive, and restore does not spend an embedding by running `reindex` on its own.

## Decision Outcome

Chosen option: one encrypted `.zettelbak`, written by `zettel backup` and applied by `zettel restore`. Both commands take the operation lock. There is no `--passphrase`. The passphrase comes from `getpass` (`Senha do backup:`) or from `--passphrase-file`. Without a terminal and without that file, the command exits 2 (`Sem terminal para ler a senha. Use --passphrase-file.`). An empty passphrase exits 1. On POSIX the passphrase file must be mode `0600`; Windows does not expose that mode, so it is not checked. The passphrase is an argument of `encrypt` / `decrypt`. It is not stored in the archive.

### On-disk format

The file is one blob, not a zip a reader can open:

1. Magic `ZETTELBAK1` (10 bytes, the ASCII string itself).
2. scrypt salt, 16 random bytes.
3. AES-GCM nonce, 12 random bytes.
4. Ciphertext. `AESGCM.encrypt` appends the 16-byte tag.

The key is `scrypt(passphrase, salt, n=2**14, r=8, p=1, length=32)`. Associated data is empty. A wrong passphrase and a tampered blob both raise `BackupCryptoError` and return no plaintext. On `restore`, that failure exits 1 with `senha incorreta ou arquivo danificado` and does not print the passphrase or the file's contents. A failure while *creating* the blob exits 1 with `Nao foi possivel cifrar o backup.`

Inside the plaintext is a deflated zip (Zip64). `manifest.json` is format version 1 (`format_version`), with `created_at` in UTC, `include_chroma`, `include_inbox`, and a sha256 for every other member. Restore checks those digests before it writes.

### What is packed

`create_backup` runs `retention.flush` first, then snapshots SQLite with the backup API (`journal_mode=DELETE` on the copy), so a pending WAL is not torn and the snapshot is not a copy of the live file.

| Member | When |
| --- | --- |
| `manifest.json` | Always |
| `state.db` | Always |
| `.env` | When the project env file exists |
| `chroma/…` | Unless `--no-chroma` |
| `inbox/…` | Only with `--with-inbox` |
| `vault/10_Sources/`, `20_Literature/`, `30_Permanent/`, `40_MOCs/`, `50_Projetos/`, `90_Assets/`, `99_Templates/`, `vault/.obsidian/` | When the directory exists |

`.obsidian` drops directories named `cache`, and `plugins/**/main.js` and `styles.css`. `data/cache`, `zettel.lock` and `.venv` are never packed. Other vault folders (`00_Inbox/`, an old `60_Estante/`) are not members.

The `.zettelbak` is written mode `0600` when the OS allows. `chmod` failure on POSIX is an error; on Windows it is ignored.

### Restore

Members are refused, and nothing is written, when a name is absolute, contains `..`, `.`, a backslash, a NUL, `//` or a drive letter, when two members share a destination, when the destination is outside the vault, `state_db_path`, `chroma_path`, `inbox_path` and the project `.env`, or when the archive has more than 100,000 members or more than 10 GiB uncompressed.

Packed paths are then replaced. An existing `.env` is replaced only with `--force-env`; otherwise it is kept and the command says so. The restored `.env` is mode `0600` when the OS allows. SQLite `-wal` / `-shm` sidecars of `state.db` are removed so the snapshot is the database that opens.

`--rotate-session` replaces `web_auth.session_key` and does not print the new key. Without it, restoring `state.db` puts the archived session key back in service. The key is inside the encrypted database: anyone who can open a leaked archive can forge session cookies until the key is rotated (this flag, or logout).

After the write, `run_rebuild_vault(..., force=False)` recreates vault files the database has and the disk does not. A file that already exists is left as it is. `origin: manual` is not overwritten even by `rebuild --force`. If the archive omitted Chroma, restore prints that the user should run `zettel reindex --force`, or delete the Chroma directory before a plain `zettel reindex`. It does not run it, and it does not delete a Chroma directory that was already on disk. Without `--force`, searches can still return vectors that the restored SQLite does not have.

### Consequences

* One passphrase opens notes, API keys and the session key. Losing the passphrase loses the archive; there is no recovery path and the passphrase is not in the file.
* `init --reset` still drops manual rows with the database and keeps their files. `zettel backup` is what brings those rows back. The command says so: `As linhas das notas manuais somem com o banco. 'zettel backup' e o que as traz de volta.`
* `init` without `--reset` recreates missing files from SQLite. When it writes any, it prints `{n} arquivo(s) recriado(s) a partir do banco.`
* Plugin binaries under `.obsidian/plugins/` are not in the archive. `data.json` and `manifest.json` are. A restore may need the plugin installed again.
* The two stores are still not one transaction ([ADR-005](./ADR-005-dual-store-persistence.md)). The archive is a snapshot of a quiescent pair, taken under the operation lock.

## Non-goals

* No web UI for backup or restore.
* No image blobs in SQLite. The bytes stay in `90_Assets/`, which the archive does include.
* No automatic `reindex` after a restore that omitted Chroma.

## Alternatives

* **Copy `data/` and `vault/` in the clear.** Rejected: easy to forget one store, and the copy includes API keys and the session key as ordinary files.
* **Encrypt only `.env`.** Rejected: the notes and `web_auth.session_key` would be readable in the same package.
* **A `--passphrase` flag.** Rejected: the secret would show up in the process list and in shell history.
* **Store image bytes in SQLite so the database alone is the vault.** Rejected: the files are already content-addressed in `90_Assets/`, and the archive carries that directory.

## References

* `zettel/cli/backup.py` — flags, exit codes, the messages above
* `zettel/backup/crypto.py` — `ZETTELBAK1`, scrypt, AES-256-GCM
* `zettel/backup/archive.py` — members, limits, manifest, restore plan
* `zettel/retention.py` — `flush`, `whole_file_sha256`
* `zettel/rebuild.py` — `run_rebuild_vault`
* `zettel/cli/maintenance.py` — `init` materializes missing files

## Addendum: paths are relative to the vault or the inbox

`origin_path`, `files.path`, `literature_note_path`, `notes.path`, `mocs.path` and `projects.path` are stored relative to the vault or, for an inbox original, relative to the inbox. A file outside both stays absolute. Restore into another directory works because those fields name a place inside the restored tree, not a drive letter from the machine that made the backup. Covers and `assets.path` were already vault-relative. `scripts/relativize_stored_paths.py` rewrites rows and SRC frontmatter that were stored before this; it is not part of the package.

## Addendum: the web page

The non-goal "no web UI" is amended. An authenticated page at `/backup` downloads a `.zettelbak` and restores one. It calls the same `create_backup` and `restore_backup` as the CLI. The passphrase is a `type=password` field with `autocomplete=off`. It is written to a temp file outside the vault and deleted when the job finishes. It is not stored in `web_jobs.payload_json`, in a job event, or in the URL. The payload holds only the flags. Chroma starts included and the inbox starts excluded. Restore asks for the word `restaurar`. `force_env` and `rotate_session` start off.

The 26 MB body limit still applies to every other route. Only `POST /backup/restaurar` accepts a larger body, and it stops at the archive cap (10 GiB, 100,000 members), counting bytes as it writes. A wrong passphrase or a damaged file fails the job with `senha incorreta ou arquivo danificado`, writes nothing, and does not repeat the passphrase. Before replacing `state.db` the worker closes the SQLite connection it opened. Afterwards it drops the cached session key, and the page says the user has to log in again. The page does not run `reindex`.
