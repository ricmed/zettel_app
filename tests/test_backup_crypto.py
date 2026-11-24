"""Cipher tests. Fake passphrase and fake bytes only; no vault and no .env."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from zettel.backup.crypto import (
    MAGIC,
    BackupCryptoError,
    decrypt,
    encrypt,
    read_passphrase_file,
)

_PHRASE = "frase-ficticia"
_OTHER = "outra-frase-ficticia"
_PLAIN = b"bytes ficticios do pacote"
_SALT_LEN = 16
_NONCE_LEN = 12
_TAG_LEN = 16


def test_roundtrip():
    blob = encrypt(_PLAIN, _PHRASE)
    assert blob.startswith(MAGIC)
    assert MAGIC == b"ZETTELBAK1"
    assert len(MAGIC) == 10
    salt, nonce, body = _split(blob)
    assert len(salt) == _SALT_LEN
    assert len(nonce) == _NONCE_LEN
    assert len(body) == len(_PLAIN) + _TAG_LEN
    assert decrypt(blob, _PHRASE) == _PLAIN

    empty = encrypt(b"", _PHRASE)
    assert len(empty) == len(MAGIC) + _SALT_LEN + _NONCE_LEN + _TAG_LEN
    assert decrypt(empty, _PHRASE) == b""

    again = encrypt(_PLAIN, _PHRASE)
    salt_again, nonce_again, _body_again = _split(again)
    assert salt_again != salt
    assert nonce_again != nonce
    assert decrypt(again, _PHRASE) == _PLAIN
    assert decrypt(encrypt("ação".encode(), "senha-fictícia"), "senha-fictícia") == "ação".encode()


def test_wrong_passphrase():
    blob = encrypt(_PLAIN, _PHRASE)
    with pytest.raises(BackupCryptoError) as caught:
        decrypt(blob, _OTHER)
    assert _OTHER not in str(caught.value)
    assert _PLAIN not in str(caught.value).encode()


def test_tampered_byte():
    blob = bytearray(encrypt(_PLAIN, _PHRASE))
    blob[len(MAGIC) + _SALT_LEN + _NONCE_LEN] ^= 0x01
    with pytest.raises(BackupCryptoError) as caught:
        decrypt(bytes(blob), _PHRASE)
    assert _PLAIN not in str(caught.value).encode()


def test_bad_magic():
    blob = bytearray(encrypt(_PLAIN, _PHRASE))
    blob[0] ^= 0x01
    with pytest.raises(BackupCryptoError) as caught:
        decrypt(bytes(blob), _PHRASE)
    assert _PLAIN not in str(caught.value).encode()


def test_truncated_blob():
    blob = encrypt(_PLAIN, _PHRASE)
    short = (
        b"",
        MAGIC,
        blob[: len(MAGIC) + _SALT_LEN + _NONCE_LEN],
        blob[: len(MAGIC) + _SALT_LEN + _NONCE_LEN + _TAG_LEN - 1],
    )
    for item in short:
        with pytest.raises(BackupCryptoError) as caught:
            decrypt(item, _PHRASE)
        assert _PLAIN not in str(caught.value).encode()


def test_empty_passphrase():
    with pytest.raises(BackupCryptoError):
        encrypt(_PLAIN, "")
    blob = encrypt(_PLAIN, _PHRASE)
    with pytest.raises(BackupCryptoError):
        decrypt(blob, "")


def test_read_passphrase_file(tmp_path: Path):
    path = tmp_path / "phrase.txt"
    path.write_bytes(b"frase-ficticia\n")
    _own_only(path)
    assert read_passphrase_file(path) == "frase-ficticia"

    path.write_bytes(b"frase-ficticia\r\n")
    assert read_passphrase_file(path) == "frase-ficticia"

    path.write_bytes(b"frase-ficticia\n\n")
    assert read_passphrase_file(path) == "frase-ficticia\n"

    path.write_bytes(b"frase-ficticia")
    assert read_passphrase_file(path) == "frase-ficticia"

    path.write_bytes(b" frase \n")
    assert read_passphrase_file(path) == " frase "

    missing = tmp_path / "ausente"
    with pytest.raises(BackupCryptoError):
        read_passphrase_file(missing)

    empty = tmp_path / "vazio"
    empty.write_bytes(b"\n")
    _own_only(empty)
    with pytest.raises(BackupCryptoError):
        read_passphrase_file(empty)

    empty.write_bytes(b"")
    with pytest.raises(BackupCryptoError):
        read_passphrase_file(empty)

    empty.write_bytes(b"\xff")
    with pytest.raises(BackupCryptoError):
        read_passphrase_file(empty)


def test_read_passphrase_file_refuses_loose_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "phrase.txt"
    path.write_bytes(b"frase-ficticia\n")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(Path, "stat", lambda _self: _Stat(0o644))
    with pytest.raises(BackupCryptoError):
        read_passphrase_file(path)

    monkeypatch.setattr(Path, "stat", lambda _self: _Stat(0o600))
    assert read_passphrase_file(path) == "frase-ficticia"


@pytest.mark.skipif(os.name != "posix", reason="Unix file mode is not exposed")
def test_read_passphrase_file_refuses_real_loose_mode(tmp_path: Path):
    path = tmp_path / "phrase.txt"
    path.write_bytes(b"frase-ficticia\n")
    path.chmod(0o644)
    with pytest.raises(BackupCryptoError):
        read_passphrase_file(path)
    path.chmod(0o600)
    assert read_passphrase_file(path) == "frase-ficticia"


def _own_only(path: Path) -> None:
    if os.name == "posix":
        path.chmod(0o600)


def _split(blob: bytes) -> tuple[bytes, bytes, bytes]:
    rest = blob[len(MAGIC) :]
    salt = rest[:_SALT_LEN]
    nonce = rest[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
    body = rest[_SALT_LEN + _NONCE_LEN :]
    return salt, nonce, body


class _Stat:
    def __init__(self, mode: int) -> None:
        self.st_mode = stat.S_IFREG | mode
