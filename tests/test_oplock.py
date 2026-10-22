"""The cross-process lock that serialises mutating operations (#308)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from zettel.oplock import LockBusy, OperationLock, lock_holder

_HOLDER = """
import sys, time
from pathlib import Path
from zettel.oplock import OperationLock
lock = OperationLock(Path(sys.argv[1]))
lock.acquire("connect", "cli")
Path(sys.argv[2]).write_text("held")
time.sleep(60)
"""


def _hold_in_another_process(tmp_path: Path) -> subprocess.Popen:
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(tmp_path / "zettel.lock"), str(ready)],
        cwd=Path(__file__).resolve().parents[1],
    )
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None, "holder process died"
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return proc


def test_a_second_process_sees_who_holds_the_lock(tmp_path: Path):
    proc = _hold_in_another_process(tmp_path)
    try:
        with pytest.raises(LockBusy) as busy:
            OperationLock(tmp_path / "zettel.lock").acquire("extract", "web")
        assert busy.value.holder["operation"] == "connect"
        assert busy.value.holder["surface"] == "cli"
        # Not proc.pid: on Windows the venv python.exe is a launcher whose child
        # runs the script, so the holder is another process with another pid.
        assert lock_holder(tmp_path / "zettel.lock")["pid"] not in {None, os.getpid()}
    finally:
        proc.kill()
        proc.wait()


def test_a_dead_holder_frees_the_lock(tmp_path: Path):
    """The OS frees a dead holder's lock, with no cleanup by us.

    On Windows the release is asynchronous ("depends upon available system
    resources", LockFileEx docs), so under a loaded parallel run it can lag the
    process exit: wait for the condition instead of asserting it at once (#326).
    """
    proc = _hold_in_another_process(tmp_path)
    proc.kill()
    proc.wait()
    deadline = time.monotonic() + 30
    while lock_holder(tmp_path / "zettel.lock") is not None:
        assert time.monotonic() < deadline, "the OS never released a dead holder's lock"
        time.sleep(0.05)
    lock = OperationLock(tmp_path / "zettel.lock")
    lock.acquire("extract", "web")
    lock.release()


def test_two_handles_in_one_process_exclude_each_other(tmp_path: Path):
    """The web worker and a request thread must not both hold it."""
    first = OperationLock(tmp_path / "zettel.lock")
    first.acquire("garden", "web")
    try:
        with pytest.raises(LockBusy):
            OperationLock(tmp_path / "zettel.lock").acquire("review_links", "web")
    finally:
        first.release()
    assert lock_holder(tmp_path / "zettel.lock") is None


def _cli_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f"vault_path: {tmp_path / 'vault'}",
                f"inbox_path: {tmp_path / 'inbox'}",
                f"chroma_path: {tmp_path / 'chroma'}",
                f"state_db_path: {tmp_path / 'state.db'}",
                f"cache_path: {tmp_path / 'cache'}",
            ]
        ),
        encoding="utf-8",
    )
    return config


def test_a_mutating_cli_command_refuses_while_another_operation_runs(tmp_path: Path):
    from typer.testing import CliRunner
    from zettel.cli import app

    config = _cli_config(tmp_path)
    held = OperationLock(tmp_path / "zettel.lock")
    held.acquire("garden", "web")
    try:
        result = CliRunner().invoke(app, ["extract", "--config", str(config), "--yes"])
    finally:
        held.release()
    assert result.exit_code == 1
    assert "garden" in result.output and "web" in result.output


def test_a_read_only_cli_command_runs_while_the_lock_is_held(tmp_path: Path):
    from typer.testing import CliRunner
    from zettel.cli import app

    config = _cli_config(tmp_path)
    held = OperationLock(tmp_path / "zettel.lock")
    held.acquire("garden", "web")
    try:
        result = CliRunner().invoke(app, ["status", "--config", str(config)])
    finally:
        held.release()
    assert result.exit_code == 0, result.output


def test_every_mutating_command_goes_through_load_deps():
    """The lock is taken in load_deps; a mutating command that skips it is unguarded."""
    import ast

    from zettel.cli.deps import MUTATING_COMMANDS

    cli = Path(__file__).resolve().parents[1] / "zettel" / "cli"
    unguarded = set(MUTATING_COMMANDS)
    for path in cli.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef):
                continue
            names = {
                kw.value.value
                for dec in node.decorator_list
                if isinstance(dec, ast.Call)
                for kw in dec.keywords
                if kw.arg == "name" and isinstance(kw.value, ast.Constant)
            } or {node.name.replace("_", "-")}
            calls = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if "load_deps" in calls:
                unguarded -= names
    assert not unguarded, f"comandos mutantes sem load_deps: {sorted(unguarded)}"
