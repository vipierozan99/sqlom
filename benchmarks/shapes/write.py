"""The write workload: N rows updated by primary key, one parameter set each.

Not a row *shape* — it reads and writes `flat`'s table, its rows and its models,
re-exported below rather than redeclared. It is a separate `--shape` because the
equivalence gate is per `(backend, shape)` group and a write contender's payload
is a count, not rows: put in `flat`'s group it would fail the gate against every
read there.

**Why UPDATE by primary key, and not INSERT.** `harness/timing.per_iteration`
times one callable N times with no reset between calls, and there is nowhere for
an insert workload to put the cleanup: growing the table under the measurement
prices page splits and index maintenance as API overhead, and truncating inside
the callable puts a `TRUNCATE` inside every timed sample. An UPDATE of the same
N keys to the same N values costs what it costs on iteration 1500 exactly as on
iteration 1, and is idempotent, which is also what lets a test assert the write
actually happened (`tests/test_bench_write_parity.py`).

The consequence is a gap this file does not close: **`copy_in` is still
unmeasured**, because COPY only inserts. Measuring it needs either a per-iteration
reset hook in the harness or a `TRUNCATE` priced into every arm, and both are
decisions to make deliberately rather than on the way past
(`docs/METHODOLOGY.md`).
"""

from __future__ import annotations

import sqlalchemy as sa

from benchmarks.shapes import flat

#: `flat`'s, verbatim — the table this workload writes to is the table the read
#: shapes read from, seeded by the same rows (`harness/seed.data_shape_for`).
metadata = flat.metadata
users_table = flat.users_table
User = flat.User
UserORM = flat.UserORM


def update_stmt() -> sa.Update:
    """`UPDATE users SET name = :new_name WHERE id = :row_id`."""
    return (
        sa.update(users_table)
        .where(users_table.c.id == sa.bindparam("row_id"))
        .values(name=sa.bindparam("new_name"))
    )


def update_params(rows: int) -> list[dict[str, object]]:
    """One parameter set per row, deterministic and idempotent: row `i` always
    gets the same name, so the workload can be re-run — by the equivalence gate,
    by a warm-up, by 1500 timed iterations — without ever changing its own cost.
    """
    return [{"row_id": i, "new_name": marker(i)} for i in range(1, rows + 1)]


def orm_update() -> sa.Update:
    """The ORM's own spelling of the same batch: bulk UPDATE by primary key,
    where the key travels in the parameter sets rather than in a WHERE clause
    (`orm_params`).

    Not the statement above with an ORM entity swapped in, which is what this
    started as: SQLAlchemy refuses a bulk ORM update that carries its own WHERE
    criteria unless the caller also passes `synchronize_session=None`, so that
    arm would have been the ORM API plus a workaround nobody writing ORM code
    needs. Both spellings compile to the same `UPDATE users SET name=? WHERE
    id=?`, which `tests/test_bench_write_parity.py` asserts rather than assumes.
    """
    return sa.update(UserORM)


def orm_params(rows: int) -> list[dict[str, object]]:
    return [{"id": i, "name": marker(i)} for i in range(1, rows + 1)]


def marker(row_id: int) -> str:
    """What a row's `name` reads after this workload has touched it."""
    return f"updated-{row_id}"
