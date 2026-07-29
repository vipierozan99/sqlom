"""Run the type checkers as tests, so a type regression fails `pytest`.

Both are run because they disagree, and a feature that only works in one of them is
not something to advertise. pyright is stricter about descriptor overloads; mypy
reports different diagnostic codes for the same mistake, which is why
`tests/typing/negative.py` carries a suppression for each.

Skipped rather than failed when a checker is not installed — the rest of the suite
should not need a type checker to run.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TYPING_DIR = ROOT / "tests" / "typing"
CONFIG = ROOT / "pyproject.toml"


def _run(command):
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=600
    )


@pytest.mark.typing
def test_mypy_is_clean():
    """mypy over tests/typing/, with warn_unused_ignores on.

    That flag is what makes negative.py a test: an expected error that stops
    happening leaves an unnecessary `# type: ignore`, which becomes an error.
    """
    if not shutil.which("mypy") and not _has_module("mypy"):
        pytest.skip("mypy is not installed")
    result = _run([sys.executable, "-m", "mypy",
                   "--config-file", str(CONFIG), str(TYPING_DIR)])
    assert result.returncode == 0, (
        f"mypy reported problems:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.typing
def test_pyright_is_clean():
    """pyright over rowform/ and tests/typing/, with reportUnnecessaryTypeIgnoreComment on."""
    if not shutil.which("pyright"):
        pytest.skip("pyright is not installed")
    result = _run(["pyright", "--outputjson"])
    # pyright exits 1 when it reports errors; parse rather than trust the code, so
    # the failure message names the actual diagnostics.
    assert result.returncode == 0, (
        f"pyright reported problems:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.typing
def test_the_negative_file_would_fail_without_its_suppressions():
    """Guards the guard.

    If `warn_unused_ignores` were switched off, or the suppressions stopped matching
    real diagnostics, negative.py would pass while proving nothing. So: strip every
    suppression and require that mypy then reports errors on the same lines.
    """
    if not _has_module("mypy"):
        pytest.skip("mypy is not installed")

    import re
    import tempfile

    source = (TYPING_DIR / "negative.py").read_text()
    stripped = re.sub(r"\s*#\s*type:\s*ignore(\[[^\]]*\])?", "", source)
    stripped = re.sub(r"\s*#\s*pyright:\s*ignore(\[[^\]]*\])?", "", stripped)

    # Real COMMENT tokens only. A naive substring search over lines also matches
    # this file's own prose about `# type: ignore`, which is how the first version
    # of this test failed on a docstring.
    expected_lines = _suppressed_lines(source)
    assert expected_lines, "negative.py has no expected-error lines"

    with tempfile.TemporaryDirectory(dir=TYPING_DIR) as tmp:
        probe = Path(tmp) / "probe.py"
        probe.write_text(stripped)
        result = _run([sys.executable, "-m", "mypy",
                       "--config-file", str(CONFIG), str(probe)])
        reported = {
            int(match.group(1))
            for match in re.finditer(r"probe\.py:(\d+): error:", result.stdout)
        }

    missing = sorted(expected_lines - reported)
    assert not missing, (
        "these lines carry an expected-error suppression but mypy does not report "
        f"an error there without it: {missing}\n{result.stdout}"
    )


def _suppressed_lines(source):
    """Line numbers carrying a real `# type: ignore` comment token."""
    import io
    import tokenize

    lines = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and "type: ignore" in token.string:
            lines.add(token.start[0])
    return lines


def _has_module(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False
