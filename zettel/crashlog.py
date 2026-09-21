"""Make a native crash leave a trace.

A fault inside a C/C++ extension (docling-parse, torch, onnxruntime) kills the
process before Python can raise: no traceback, the terminal just returns, and the
``runs`` row is left ``running``. The only evidence was the Windows Event Log,
which names the faulting DLL but not what the pipeline was doing.

``enable_crash_log`` points :mod:`faulthandler` at a per-process file, so a fatal
signal (access violation, segfault, abort) dumps the Python stack of every thread
to disk. The file opens with a header (time, pid, command line) and is deleted at
a normal exit, so ``data/logs/`` only ever holds the crashes. Stdlib only: this
runs before the CLI imports anything heavy.

Not every native death is catchable: a heap-corruption fail-fast
(``0xc0000374``) terminates the process without raising an exception, and then
the file keeps only its header — which still records that the run died.
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

CRASH_LOG_DIR = Path("data") / "logs"

# Kept open for the life of the process: faulthandler writes to the raw fd at
# crash time, so the file object must never be garbage-collected.
_crash_file: TextIO | None = None
_crash_path: Path | None = None


def enable_crash_log(log_dir: Path = CRASH_LOG_DIR) -> Path | None:
    """Route fatal-signal tracebacks to ``log_dir``; return the file, or None.

    A no-op when faulthandler is already on (pytest's own plugin, ``python -X
    faulthandler``) — whoever enabled it chose where the dump goes — and when the
    directory cannot be created, so diagnostics never stop the pipeline.
    """
    global _crash_file, _crash_path
    if faulthandler.is_enabled():
        return None
    # Local wall-clock time with offset: what the Windows Event Log shows.
    now = datetime.now(UTC).astimezone()
    path = log_dir / f"crash-{now:%Y%m%d-%H%M%S}-{os.getpid()}.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _crash_file = path.open("w", encoding="utf-8")
    except OSError:
        return None
    _crash_file.write(
        f"zettel native crash log\n"
        f"started: {now.isoformat(timespec='seconds')}\n"
        f"pid: {os.getpid()}\n"
        f"argv: {' '.join(sys.argv)}\n\n"
    )
    _crash_file.flush()
    faulthandler.enable(file=_crash_file, all_threads=True)
    atexit.register(_discard_on_clean_exit, path)
    _crash_path = path
    return path


def leftover_crash_logs(log_dir: Path = CRASH_LOG_DIR) -> list[Path]:
    """Crash logs of other processes: a native crash, or a process still running."""
    if not log_dir.is_dir():
        return []
    return sorted(p for p in log_dir.glob("crash-*.log") if p != _crash_path)


def _discard_on_clean_exit(path: Path) -> None:
    """A process that reaches atexit did not crash natively: drop its log."""
    global _crash_file, _crash_path
    faulthandler.disable()
    if _crash_file is not None:
        _crash_file.close()
        _crash_file = None
    _crash_path = None
    path.unlink(missing_ok=True)
