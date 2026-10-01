"""`python -m benchmarks` — the unified benchmark CLI.

`load` is mounted lazily: `benchmarks.cli.load` imports locust, which runs
`gevent.monkey.patch_all()` and replaces `threading.Thread` for the whole
process — aiosqlite's worker threads become greenlets and every timed number
moves ~30%. `timing.assert_unpatched_threading()` is the backstop.
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

_LAZY_SUBCOMMANDS = ("load",)


def _mount(names: tuple[str, ...]) -> None:
    for name in names:
        app.add_typer(importlib.import_module(f"benchmarks.cli.{name}").app, name=name)


# Read from `argv[1]` rather than scanning the whole line, so an option *value*
# of "load" cannot reintroduce the patch into a `bench micro` run.
_invoked = sys.argv[1] if len(sys.argv) > 1 else ""
if _invoked in _LAZY_SUBCOMMANDS:
    _mount((_invoked,))
elif _invoked in ("", "--help", "-h"):
    _mount(_LAZY_SUBCOMMANDS)  # help only; nothing is timed on this path

if __name__ == "__main__":
    app()
