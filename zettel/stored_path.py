"""Paths stored in SQLite and in the source frontmatter.

A note, a project and a literature file are relative to the vault. An inbox
original is relative to the inbox. Anything outside both stays absolute: a
download folder has no base inside the project. Slashes are ``/``, with no
``..`` and no ``./``. Readers join a relative value to its base, so a restore
into another directory still finds the file.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

# Top folder of a vault-relative path. A PDF never starts with one of these,
# so an origin that is just ``livro.pdf`` resolves against the inbox.
_VAULT_TOP = frozenset(
    {
        "00_Inbox",
        "10_Sources",
        "20_Literature",
        "30_Permanent",
        "40_MOCs",
        "50_Projetos",
        "90_Assets",
        "99_Templates",
    }
)


class StoredPathError(ValueError):
    """The stored text is empty, escapes its base, or is not a file path."""


def store_vault(path: Path, vault: Path) -> str:
    """Vault-relative form, or the absolute path when it sits outside the vault."""
    return _under(path, vault) or _absolute(path)


def store_inbox(path: Path, inbox: Path) -> str:
    """Inbox-relative form, or the absolute path when it sits outside the inbox."""
    return _under(path, inbox) or _absolute(path)


def store_origin(path: Path, vault: Path, inbox: Path) -> str:
    """Vault-relative, else inbox-relative, else absolute."""
    return _under(path, vault) or _under(path, inbox) or _absolute(path)


def resolve_stored(stored: str, base: Path) -> Path:
    """Absolute path of a value stored relative to ``base``.

    An absolute stored path is returned unchanged. ``..`` that leaves the base
    raises ``StoredPathError``.
    """
    if not stored or "\x00" in stored:
        raise StoredPathError("caminho vazio")
    raw = Path(stored)
    if raw.is_absolute():
        return raw
    parts = _posix_parts(stored)
    if any(part == ".." for part in parts):
        raise StoredPathError("caminho escapa a base")
    root = Path(base).resolve()
    dest = root.joinpath(*parts).resolve()
    try:
        dest.relative_to(root)
    except ValueError as exc:
        raise StoredPathError("caminho escapa a base") from exc
    if dest == root:
        raise StoredPathError("caminho e a propria base")
    return dest


def resolve_origin(stored: str, vault: Path, inbox: Path) -> Path:
    """Resolve an origin: a vault folder goes to the vault, anything else to the inbox."""
    if not stored or "\x00" in stored:
        raise StoredPathError("caminho vazio")
    if Path(stored).is_absolute():
        return Path(stored)
    first = _posix_parts(stored)[0]
    base = vault if first in _VAULT_TOP else inbox
    return resolve_stored(stored, base)


def open_stored(stored: str, base: Path | None) -> Path | None:
    """The file a stored path names, or ``None`` when it cannot be resolved.

    An absolute value is used as-is. A relative value needs ``base``. A broken
    relative value is missing, not an exception, so a doctor pass can list it.
    """
    if not stored:
        return None
    if Path(stored).is_absolute():
        return Path(stored)
    if base is None:
        return None
    try:
        return resolve_stored(stored, base)
    except StoredPathError:
        return None


def same_stored(stored: str, path: Path, base: Path) -> bool:
    """True when ``stored`` and ``path`` are the same file under ``base``.

    An absolute stored value and the relative form of that same file compare
    equal, so a sync does not treat the note as moved.
    """
    try:
        left = resolve_stored(stored, base)
    except StoredPathError:
        return False
    try:
        return left.resolve() == Path(path).resolve()
    except OSError:
        return False


def relativize(stored: str, *, kind: str, vault: Path, inbox: Path) -> tuple[str, str]:
    """Canonical form of a path already stored, and what happened to it.

    The status is ``converted``, ``already``, ``outside`` or ``invalid``.
    An outside path is returned unchanged: the caller lists it and does not
    invent a relative name.
    """
    if kind not in {"vault", "inbox", "origin"}:
        raise ValueError(kind)
    if not stored or "\x00" in stored:
        return stored, "invalid"
    if Path(stored).is_absolute():
        target = Path(stored)
    else:
        try:
            parts = _posix_parts(stored)
        except StoredPathError:
            return stored, "invalid"
        if any(part == ".." for part in parts):
            return stored, "invalid"
        try:
            target = _resolve_kind(stored, kind, vault, inbox)
        except StoredPathError:
            return stored, "invalid"
    canonical = _store_kind(target, kind, vault, inbox)
    if Path(canonical).is_absolute():
        return stored, "outside"
    if stored.replace("\\", "/") == canonical:
        return canonical, "already"
    return canonical, "converted"


def _resolve_kind(stored: str, kind: str, vault: Path, inbox: Path) -> Path:
    if kind == "vault":
        return resolve_stored(stored, vault)
    if kind == "inbox":
        return resolve_stored(stored, inbox)
    return resolve_origin(stored, vault, inbox)


def _store_kind(path: Path, kind: str, vault: Path, inbox: Path) -> str:
    if kind == "vault":
        return store_vault(path, vault)
    if kind == "inbox":
        return store_inbox(path, inbox)
    return store_origin(path, vault, inbox)


def _posix_parts(stored: str) -> tuple[str, ...]:
    text = stored.replace("\\", "/")
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        raise StoredPathError("caminho absoluto disfarcado")
    parts = PurePosixPath(text).parts
    if not parts or any(part in {"", "."} for part in parts):
        raise StoredPathError("caminho vazio")
    return parts


def _under(path: Path, root: Path) -> str | None:
    try:
        dest = Path(path).expanduser().resolve()
        base = Path(root).resolve()
        relative = dest.relative_to(base)
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return None
    return relative.as_posix()


def _absolute(path: Path) -> str:
    return Path(path).expanduser().resolve().as_posix()
