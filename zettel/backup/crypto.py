"""AES-256-GCM blobs for a ``.zettelbak`` file.

On-wire layout, in order:

- magic ``ZETTELBAK1`` (10 bytes; the ASCII string itself)
- scrypt salt, 16 random bytes
- AES-GCM nonce, 12 random bytes
- ciphertext, with the 16-byte GCM tag appended by ``AESGCM.encrypt``

The key is ``scrypt(passphrase, salt, n=2**14, r=8, p=1, length=32)`` from
``cryptography.hazmat``. Associated data is empty. A wrong passphrase and a
tampered blob fail the same way: ``BackupCryptoError``, and no plaintext is
returned.

The passphrase is an argument of :func:`encrypt` / :func:`decrypt`, or the
contents of a file read by :func:`read_passphrase_file`. This module does not
read ``sys.argv`` and has no command-line flag.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"ZETTELBAK1"

_SALT_LEN = 16
_NONCE_LEN = 12
_TAG_LEN = 16
_KEY_LEN = 32
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_HEADER_LEN = len(MAGIC) + _SALT_LEN + _NONCE_LEN
_MIN_BLOB_LEN = _HEADER_LEN + _TAG_LEN

__all__ = [
    "MAGIC",
    "BackupCryptoError",
    "decrypt",
    "encrypt",
    "read_passphrase_file",
]


class BackupCryptoError(Exception):
    """Raised for a bad header, truncated blob, or wrong passphrase."""


def encrypt(plaintext: bytes, passphrase: str) -> bytes:
    """Return the full .zettelbak blob."""
    _require_passphrase(passphrase)
    salt = secrets.token_bytes(_SALT_LEN)
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = AESGCM(_derive_key(passphrase, salt)).encrypt(nonce, plaintext, None)
    return b"".join((MAGIC, salt, nonce, ciphertext))


def decrypt(blob: bytes, passphrase: str) -> bytes:
    """Inverse of encrypt. Wrong passphrase or tampered blob raises BackupCryptoError."""
    _require_passphrase(passphrase)
    if len(blob) < _MIN_BLOB_LEN:
        raise BackupCryptoError("Blob de backup truncado.")
    if not blob.startswith(MAGIC):
        raise BackupCryptoError("Cabeçalho de backup inválido.")
    salt = blob[len(MAGIC) : len(MAGIC) + _SALT_LEN]
    nonce = blob[len(MAGIC) + _SALT_LEN : _HEADER_LEN]
    ciphertext = blob[_HEADER_LEN:]
    try:
        return AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, ciphertext, None)
    except (InvalidTag, ValueError) as exc:
        raise BackupCryptoError("Não foi possível abrir o backup.") from exc


def read_passphrase_file(path: Path) -> str:
    """Read a passphrase file.

    Strip one trailing newline only. Raise BackupCryptoError if missing or
    empty. A CRLF pair counts as that one newline. On POSIX, a mode other
    than 0o600 also raises BackupCryptoError. Windows does not expose it.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise BackupCryptoError("Arquivo de senha ausente ou ilegível.")
    _require_mode_0600(file_path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise BackupCryptoError("Arquivo de senha ausente ou ilegível.") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupCryptoError("Arquivo de senha não é UTF-8.") from exc
    text = _strip_one_trailing_newline(text)
    if text == "":
        raise BackupCryptoError("Arquivo de senha vazio.")
    return text


def _require_passphrase(passphrase: str) -> None:
    if passphrase == "":
        raise BackupCryptoError("A senha está vazia.")


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def _require_mode_0600(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise BackupCryptoError("Arquivo de senha ausente ou ilegível.") from exc
    if mode != 0o600:
        raise BackupCryptoError("Arquivo de senha precisa estar no modo 0600.")


def _strip_one_trailing_newline(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith(("\n", "\r")):
        return text[:-1]
    return text
