"""One mutating operation at a time, across the CLI and the web (#308). Stdlib only.

The web queue already allowed one job at a time, but only within the web
process: a ``zettel connect`` in a terminal and a ``garden`` job wrote the same
``state.db``, the same Chroma directory (thread-safe, not process-safe) and the
same vault at once. ``OperationLock`` is an exclusive OS lock on one byte of
``data/zettel.lock`` (``msvcrt.locking`` on Windows, ``flock`` elsewhere). The OS
drops it when the holder dies, so a crashed process never leaves the lock
behind. Who holds it lives in a sidecar file, only read while the lock is taken.

Two handles exclude each other even inside one process, which is what keeps a
web request thread out while the worker runs a job.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import IO, Any


class LockBusy(Exception):
    """Another operation holds the lock. ``holder`` says which, when known."""

    def __init__(self, holder: dict[str, Any] | None):
        self.holder = holder
        super().__init__(describe_holder(holder))


def lock_path(cfg: Any) -> Path:
    """The lock sits beside ``state.db``: one lock per vault/state pair."""
    return Path(cfg.state_db_path).parent / "zettel.lock"


def describe_holder(holder: dict[str, Any] | None) -> str:
    if not holder:
        return "Outra operação mutante está em andamento."
    surface = "CLI" if holder.get("surface") == "cli" else "web"
    return (
        f"Outra operação mutante está em andamento: {holder.get('operation')} "
        f"({surface}, pid {holder.get('pid')}, desde {holder.get('started_at')})."
    )


def _try_lock(fh: IO[bytes]) -> bool:
    fh.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    fh.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _info_path(path: Path) -> Path:
    return path.with_name(path.name + ".info")


def _read_holder(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(_info_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class OperationLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh: IO[bytes] | None = None

    def acquire(self, operation: str, surface: str) -> None:
        """Take the lock or raise ``LockBusy``; never waits."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 - held until release()
        if not _try_lock(fh):
            fh.close()
            raise LockBusy(_read_holder(self.path))
        self._fh = fh
        _info_path(self.path).write_text(
            json.dumps(
                {
                    "operation": operation,
                    "surface": surface,
                    "pid": os.getpid(),
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            ),
            encoding="utf-8",
        )

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            _info_path(self.path).unlink()
        try:
            _unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> OperationLock:
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def lock_holder(path: Path) -> dict[str, Any] | None:
    """Who holds the lock, or None when it is free (a dead holder counts as free)."""
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "a+b") as fh:
        if _try_lock(fh):
            _unlock(fh)
            return None
    return _read_holder(path) or {}
