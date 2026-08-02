"""`python -m benchmarks` — the unified benchmark CLI.

`AsyncTyper` for the root app (so `cli/*` modules can mix async and sync
commands); each subcommand area is its own plain `typer.Typer()` mounted here.

**`load` and `profile` are mounted lazily, and that is load-bearing.** Both reach
`benchmarks.load.locust`, and importing `locust` runs `gevent.monkey.patch_all()`,
which replaces `threading.Thread` for the entire process. Mounting them eagerly
meant `bench micro` — the timed command — ran every measurement inside a
monkey-patched interpreter, where aiosqlite's per-connection worker threads are
greenlets: ~30% slow across the board, the ratio between contenders skewed too,
and nothing in the output to say so. `timing.assert_unpatched_threading()` is the
backstop; this is the cause it exists to prevent.
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

#: The subcommands that pull gevent in with them. See the module docstring.
_GEVENT_SUBCOMMANDS = ("load", "profile")


def _mount_gevent_subcommands() -> None:
    for name in _GEVENT_SUBCOMMANDS:
        app.add_typer(importlib.import_module(f"benchmarks.cli.{name}").app, name=name)


#: Mounted when they are what is being run, or when top-level help has to list
#: them — never for a timed command. Read from `argv[1]` rather than scanning the
#: whole line, so an option *value* of "load" cannot quietly reintroduce the
#: patch into a `bench micro` run.
_invoked = sys.argv[1] if len(sys.argv) > 1 else ""
if _invoked in _GEVENT_SUBCOMMANDS or _invoked in ("", "--help", "-h"):
    _mount_gevent_subcommands()

if __name__ == "__main__":
    app()
