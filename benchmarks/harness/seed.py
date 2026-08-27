"""One deterministic seeder for every shape — collapses the 9 near-identical
`seed()`/`seed_database()` functions the old suite had, 8 of which
shared the literal `1 if rng.random() > 0.1 else 0` for `is_active`.

Pure data: this module knows *what* rows and DDL a shape needs, not how to get
them into a particular driver — that stays in `backends/*.py`, which differs
enough per driver (sync sqlite3 for one-shot provisioning vs asyncpg bulk
insert) that forcing one I/O abstraction over both would cost more than it saves.

**DDL is generated, not written.** Each shape's models *are* their tables, so the
`CREATE TABLE` a benchmark seeds is compiled from the same declaration the
contenders query. The old suite kept two hand-written DDL blocks per shape, one
per dialect, and nothing checked them against the models — a column that drifted
would have produced a benchmark measuring a different table than it described.
"""

from __future__ import annotations

import random
from typing import Any

import sqlalchemy as sa

from benchmarks.shapes import flat, join, wide

# Fixed across every run so two runs seed byte-identical data — the whole point
# of a *deterministic* seeder. Never override this to "randomize" a comparison;
# two contenders reading different data is not a fair comparison.
RNG_SEED = 42

#: The *data* shapes — one set of tables and rows each. `create_all_shapes`
#: iterates this, so a workload that reuses another shape's table must not be
#: here or its DDL would run twice against one database.
SHAPES = ("flat", "join", "wide")

#: What `bench micro --shape` accepts: the data shapes, plus the workloads
#: defined over them. `write` updates `flat`'s rows (see `shapes/write.py`), so
#: it is a group of contenders rather than a set of tables.
RUNNABLE_SHAPES = (*SHAPES, "write")

#: Which shape's tables and rows a runnable shape needs provisioned.
_DATA_SHAPE = {"write": "flat"}

_DIALECT_URLS = {"sqlite": "sqlite://", "postgres": "postgresql://"}


def ddl_for(shape: str, dialect: str) -> list[str]:
    """`CREATE` statements for `shape` under `dialect`, in dependency order.

    Compiled from the shape's `MetaData` through SQLAlchemy's own
    `SchemaGenerator` (via a mock engine), so this gets index and foreign-key
    ordering — and the `CREATE TYPE` a postgres enum column needs before its
    table — without restating any of it.
    """
    return _render(shape, dialect, drop=False)


def drop_statements(shape: str, dialect: str) -> list[str]:
    """`DROP` statements for `shape`, dependants first — for re-seeding an
    already-provisioned database idempotently."""
    return [_if_exists(statement) for statement in _render(shape, dialect, drop=True)]


def _render(shape: str, dialect: str, *, drop: bool) -> list[str]:
    try:
        url = _DIALECT_URLS[dialect]
    except KeyError:
        raise ValueError(
            f"unknown dialect {dialect!r}; expected one of {sorted(_DIALECT_URLS)}"
        ) from None

    statements: list[str] = []

    def collect(element: Any, *_: Any, **__: Any) -> None:
        statements.append(str(element.compile(dialect=engine.dialect)).strip())

    engine = sa.create_mock_engine(url, collect)
    metadata = _module_for(shape).metadata
    if drop:
        metadata.drop_all(engine, checkfirst=False)
    else:
        metadata.create_all(engine, checkfirst=False)
    return statements


def _if_exists(statement: str) -> str:
    for prefix in ("DROP TABLE ", "DROP TYPE ", "DROP INDEX "):
        if statement.startswith(prefix) and "IF EXISTS" not in statement:
            return prefix + "IF EXISTS " + statement[len(prefix) :]
    return statement


def table_names(shape: str) -> list[str]:
    """In creation order — callers that drop reverse it themselves."""
    return [table.name for table in _module_for(shape).metadata.sorted_tables]


def flat_rows(rows: int) -> list[tuple[int, str, str, bool]]:
    """The deterministic `users` row tuples, keyed by `RNG_SEED`."""
    return list(flat.generate_rows(random.Random(RNG_SEED), rows))


def wide_rows(rows: int) -> list[tuple[Any, ...]]:
    """The deterministic `w_events` row tuples, keyed by `RNG_SEED`."""
    return list(wide.generate_rows(random.Random(RNG_SEED), rows))


def join_rows(
    authors: int,
) -> tuple[list[tuple[int, str, str, bool]], list[tuple[int, int, str, int, bool]]]:
    """The deterministic `(j_authors, j_posts)` row tuples, keyed by `RNG_SEED`.
    `authors` is the author count — the post count is derived
    (`POSTS_PER_AUTHOR` each), not requested directly, so the shape stays
    proportional as it scales.

    Two dedicated functions rather than one `rows_for(shape, rows)` dispatcher on
    purpose: a shape-keyed return type (list of tuples vs. a pair of lists)
    doesn't narrow through a runtime string check, so a dispatcher forces every
    caller to either silence a type error or duplicate the branch.
    """
    rng = random.Random(RNG_SEED)
    return list(join.generate_authors(rng, authors)), list(join.generate_posts(rng, authors))


def rows_for(shape: str, rows: int) -> list[tuple[Any, Any]]:
    """`[(table, row_tuples), ...]` in insert order, for a backend to load.

    The per-shape functions above stay, because their return *types* differ and
    a dispatcher would erase that; this is the untyped bulk-load view a seeder
    wants, where every shape is "some tables and some tuples".
    """
    if shape == "flat":
        return [(flat.users_table, flat_rows(rows))]
    if shape == "wide":
        return [(wide.events_table, wide_rows(rows))]
    if shape == "join":
        authors, posts = join_rows(rows)
        return [(join.authors_table, authors), (join.posts_table, posts)]
    raise ValueError(f"unknown shape {shape!r}; expected one of {SHAPES}")


def bound_rows(table: Any, rows: list[tuple[Any, ...]], dialect: Any) -> list[tuple[Any, ...]]:
    """Row tuples encoded the way `dialect`'s driver wants them.

    The old seeders hand-wrote this per backend as `int(active)`, which was
    enough while every shape was int/str/str/bool. It is not enough for `wide`:
    sqlite cannot bind a `Decimal`, `UUID` or `datetime` at all, and postgres
    wants an enum's label rather than the member. Both answers already exist as
    SQLAlchemy's bind processors, so this asks for them instead of restating
    them — and gets the right answer per dialect for free.
    """
    import rowform

    query = rowform.CoreQuery(sa.insert(table), dialect)
    fields = [column.name for column in table.columns]
    return [query.bind(dict(zip(fields, row)))[1] for row in rows]


def insert_sql(table: Any, dialect: Any) -> str:
    import rowform

    return rowform.CoreQuery(sa.insert(table), dialect).sql


def data_shape_for(shape: str) -> str:
    """The data shape behind a runnable one — itself, unless it is a workload
    over somebody else's table."""
    return _DATA_SHAPE.get(shape, shape)


def _module_for(shape: str):
    modules = {"flat": flat, "join": join, "wide": wide}
    try:
        return modules[shape]
    except KeyError:
        raise ValueError(f"unknown shape {shape!r}; expected one of {SHAPES}") from None
