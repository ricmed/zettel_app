"""A native crash leaves a Python stack on disk; a clean exit leaves nothing."""

import faulthandler
import subprocess
import sys
import textwrap
from pathlib import Path

from zettel.crashlog import enable_crash_log, leftover_crash_logs

ROOT = Path(__file__).resolve().parent.parent


def _run(log_dir: Path, body: str) -> subprocess.CompletedProcess:
    # A fresh interpreter: pytest keeps faulthandler enabled in its own process.
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from zettel.crashlog import enable_crash_log
        assert enable_crash_log(Path({str(log_dir)!r})) is not None
        {body}
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60
    )


def test_clean_exit_leaves_no_log(tmp_path):
    result = _run(tmp_path, "print('ok')")
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob("crash-*.log")) == []


def test_native_crash_leaves_the_python_stack(tmp_path):
    body = textwrap.dedent(
        """
        import faulthandler
        def converter_documento():
            faulthandler._sigsegv()
        converter_documento()
        """
    ).replace("\n", "\n        ")
    result = _run(tmp_path, body)
    assert result.returncode != 0

    logs = list(tmp_path.glob("crash-*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "zettel native crash log" in text
    assert "argv:" in text
    # The dump names the Python frame the process was in when it died.
    assert "converter_documento" in text


def test_noop_when_faulthandler_already_enabled(tmp_path):
    """pytest / `python -X faulthandler` chose where the dump goes: do not steal it."""
    assert faulthandler.is_enabled()
    assert enable_crash_log(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_leftover_crash_logs(tmp_path):
    assert leftover_crash_logs(tmp_path / "ausente") == []
    (tmp_path / "crash-20260921-180714-1.log").write_text("x", encoding="utf-8")
    (tmp_path / "crash-20260920-100000-2.log").write_text("x", encoding="utf-8")
    (tmp_path / "outro.txt").write_text("x", encoding="utf-8")
    assert [p.name for p in leftover_crash_logs(tmp_path)] == [
        "crash-20260920-100000-2.log",
        "crash-20260921-180714-1.log",
    ]
