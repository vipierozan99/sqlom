"""`python -m benchmarks` — the unified benchmark CLI.

`AsyncTyper` for the root app (so `cli/*` modules can mix async and sync
commands); each subcommand area is its own plain `typer.Typer()` mounted here.
"""

from async_typer import AsyncTyper

from benchmarks.cli import contenders as contenders_cli
from benchmarks.cli import db as db_cli
from benchmarks.cli import env as env_cli
from benchmarks.cli import load as load_cli
from benchmarks.cli import micro as micro_cli
from benchmarks.cli import profile as profile_cli
from benchmarks.cli import service as service_cli

app = AsyncTyper(help="rowform benchmark suite.")
app.add_typer(env_cli.app, name="env")
app.add_typer(db_cli.app, name="db")
app.add_typer(contenders_cli.app, name="contenders")
app.add_typer(micro_cli.app, name="micro")
app.add_typer(service_cli.app, name="service")
app.add_typer(load_cli.app, name="load")
app.add_typer(profile_cli.app, name="profile")

if __name__ == "__main__":
    app()
