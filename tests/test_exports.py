"""What `import rowform` gives you, and what it does not drag in with it.

Both properties used to live in `test_pool.py` and went out with the pool, which
they had nothing to do with. They are the sort that no other test can fail on:
`__all__` naming something that no longer exists breaks only `from rowform import
*`, and a stray top-level driver import breaks only the person who installed
rowform without that driver.
"""

from __future__ import annotations

import subprocess
import sys

import rowform as rf


class TestAll:
    def test_every_name_resolves(self):
        """A typo in `__all__` is otherwise found by whoever star-imports first."""
        missing = [name for name in rf.__all__ if not hasattr(rf, name)]
        assert missing == []

    def test_star_import_reaches_them(self):
        namespace: dict = {}
        exec("from rowform import *", namespace)  # noqa: S102 -- exercising __all__
        assert set(rf.__all__) <= set(namespace)


class TestItImportsNoDrivers:
    def test_importing_rowform_imports_no_driver(self):
        """`drivers.py` states this as the reason the lazy `AsyncpgEngine` export
        could be deleted: nothing there imports a driver any more, it only calls
        methods on a connection SQLAlchemy opened. A stray top-level import
        re-breaks it silently — and makes asyncpg a hard dependency of `import
        rowform` for everyone.

        A subprocess, because this session has imported all three already.
        """
        code = (
            "import rowform, sys;"
            "print([m for m in ('asyncpg', 'psycopg', 'aiosqlite') if m in sys.modules])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "[]"
