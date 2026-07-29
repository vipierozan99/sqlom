"""One deterministic seeder for every shape — collapses the 9 near-identical
`seed()`/`seed_database()` functions the old suite had (PLAN.md §1), 8 of which
shared the literal `1 if rng.random() > 0.1 else 0` for `is_active`.

Pure data: this module knows *what* rows and DDL a shape needs, not how to get
them into a particular driver — that stays in `backends/*.py`, which differs
enough per driver (sync sqlite3 for one-shot provisioning vs asyncpg bulk
insert) that forcing one I/O abstraction over both would cost more than it
saves.
"""

from __future__ import annotations

import random

from benchmarks.shapes import flat, join

# Fixed across every run so two runs seed byte-identical data — the whole
# point of a *deterministic* seeder. Never override this to "randomize" a
# comparison; two contenders reading different data is not a fair comparison.
RNG_SEED = 42

SHAPES = ("flat", "join")


def ddl_for(shape: str, dialect: str) -> list[str]:
    """DDL statements for `shape` ("flat"/"join") under `dialect` ("sqlite"/"postgres")."""
    module = _module_for(shape)
    attr = f"DDL_{dialect.upper()}"
    ddl = getattr(module, attr, None)
    if ddl is None:
        raise ValueError(f"shape {shape!r} has no DDL for dialect {dialect!r}")
    return ddl


def table_names(shape: str) -> list[str]:
    if shape == "flat":
        return [flat.TABLE_NAME]
    if shape == "join":
        return [join.AUTHORS_TABLE, join.POSTS_TABLE]
    raise ValueError(f"unknown shape {shape!r}; expected one of {SHAPES}")


def flat_rows(rows: int) -> list[tuple[int, str, str, bool]]:
    """The deterministic `users` row tuples, keyed by `RNG_SEED`."""
    return list(flat.generate_rows(random.Random(RNG_SEED), rows))


def join_rows(
    authors: int,
) -> tuple[list[tuple[int, str, str, bool]], list[tuple[int, int, str, int, bool]]]:
    """The deterministic `(j_authors, j_posts)` row tuples, keyed by
    `RNG_SEED`. `authors` is the author count — the post count is derived
    (`POSTS_PER_AUTHOR` each), not requested directly, so the shape stays
    proportional as it scales.

    Two dedicated functions rather than one `rows_for(shape, rows)` dispatcher
    on purpose: a shape-keyed return type (list of tuples vs. a pair of lists)
    doesn't narrow through a runtime string check, so a dispatcher forces
    every caller to either silence a type error or duplicate the branch.
    """
    rng = random.Random(RNG_SEED)
    return list(join.generate_authors(rng, authors)), list(join.generate_posts(rng, authors))


def drop_statements(shape: str, dialect: str) -> list[str]:
    """DROP TABLE statements for `shape`, in dependency-safe order (posts
    before authors) — for re-seeding an already-provisioned database
    idempotently. Postgres gets one statement with `CASCADE` since it enforces
    the foreign key; sqlite never enforces it here (no `PRAGMA foreign_keys`
    is set), so plain per-table drops are enough.
    """
    names = list(reversed(table_names(shape)))
    if dialect == "postgres":
        return [f"DROP TABLE IF EXISTS {', '.join(names)} CASCADE"]
    return [f"DROP TABLE IF EXISTS {name}" for name in names]


def _module_for(shape: str):
    if shape == "flat":
        return flat
    if shape == "join":
        return join
    raise ValueError(f"unknown shape {shape!r}; expected one of {SHAPES}")
