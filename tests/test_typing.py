"""Run the type checker over `tests/typing/`, so `pytest` fails when types regress.

`positive.py` asserts what must be inferred; `negative.py` asserts what must be
an error. Neither is ever imported — `negative.py` is full of deliberate
mistakes, and `pytest.ini` keeps the directory out of collection.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TYPING = ROOT / "tests" / "typing"

pytestmark = pytest.mark.typing


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_the_typing_suite_is_clean():
    result = subprocess.run(
        ["uv", "run", "basedpyright", str(TYPING)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
