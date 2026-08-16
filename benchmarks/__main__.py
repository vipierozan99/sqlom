"""`python -m benchmarks` — the unified benchmark CLI.

`AsyncTyper` for the root app (so `cli/*` modules can mix async and sync
commands); each subcommand area is its own plain `typer.Typer()` mounted here.

**`load` and `profile` are mounted lazily and separately, and that is
load-bearing.** `benchmarks.cli.load` imports `benchmarks.load.locust`, and
importing `locust` runs `gevent.monkey.patch_all()`, which replaces
`threading.Thread` for the entire process. Mounting eagerly meant `bench micro`
— the timed command — ran every measurement inside a monkey-patched
interpreter, where aiosqlite's per-connection worker threads are greenlets:
~30% slow across the board, the ratio between contenders skewed too, and
nothing in the output to say so. Mounting `load` and `profile` *together* then
re-created the same bug one door over: `bench profile micro` times an
unprofiled baseline, and pulling `cli.load` in alongside `cli.profile` patched
it. `timing.assert_unpatched_threading()` is the backstop; these are the causes
it exists to prevent (it has now caught both).
"""

import importlib
import sys

from async_typer import AsyncTyper

from benchmarks.cli import contenders as contenders_cli
from benchmarks.cli import db as db_cli
from benchmarks.cli import env as env_cli
from benchmarks.cli import micro as micro_cli
from benchmarks.cli import service as service_cli

app = AsyncTyper(help="rowform benchmark suite.")
app.add_typer(env_cli.app, name="env")
app.add_typer(db_cli.app, name="db")
app.add_typer(contenders_cli.app, name="contenders")
app.add_typer(micro_cli.app, name="micro")
app.add_typer(service_cli.app, name="service")

#: The lazily-mounted subcommands. `load` pulls gevent in at import;
#: `profile` imports locust only inside its `load` subcommand, but is mounted
#: lazily anyway so `bench micro` never even resolves it. See the module
#: docstring.
_LAZY_SUBCOMMANDS = ("load", "profile")


def _mount(names: tuple[str, ...]) -> None:
    for name in names:
        app.add_typer(importlib.import_module(f"benchmarks.cli.{name}").app, name=name)


#: Mounted one at a time: mounting them together meant `bench profile micro` —
#: a command that *times* an unprofiled baseline — imported `cli.load` (and so
#: locust, and so the gevent patch) purely because `profile` and `load` shared
#: a mounting list; `assert_unpatched_threading()` refused the run. Read from
#: `argv[1]` rather than scanning the whole line, so an option *value* of
#: "load" cannot quietly reintroduce the patch into a `bench micro` run.
_invoked = sys.argv[1] if len(sys.argv) > 1 else ""
if _invoked in _LAZY_SUBCOMMANDS:
    _mount((_invoked,))
elif _invoked in ("", "--help", "-h"):
    # Help only — nothing is timed on this path, so listing both is safe.
    _mount(_LAZY_SUBCOMMANDS)

if __name__ == "__main__":
    app()
