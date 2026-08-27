"""Do the write contenders actually write, and do they all write the same thing?

The equivalence gate cannot answer either question for this cell. A write
contender's payload is its parameter-set count (`shapes/write.py` says why), so
an arm that silently rolled its batch back, or applied it to nothing, would
return the same 16 bytes as one that worked — the write-path twin of the blind
spot corrections 8 and 14 came out of.

So the check lives here instead: run each contender once and read the table back.
Both backends, because the two `executemany` paths underneath are different code
(`aiosqlite.Connection.executemany`, `asyncpg.Connection.executemany`) reached
through different transaction rules.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

import benchmarks.micro.contenders  # noqa: F401 -- importing registers every contender
import rowform as rf
from benchmarks.backends import postgres as pg_backend
from benchmarks.backends.sqlite import EphemeralSqlite
from benchmarks.harness import registry
from benchmarks.shapes import write

#: Enough rows to seed quickly; `LIMIT` of them are updated, so the rest stay as
#: seeded and a contender that updated *everything* would fail too.
ROWS = 200
LIMIT = 50


async def _apply(spec, handle: str) -> None:
    """One call of a contender, with its own payload checked on the way past."""
    target, teardown = await spec.factory(registry.ContenderInit(handle=handle, limit=LIMIT))
    try:
        assert json.loads(await target()) == {"updated": LIMIT}
    finally:
        await teardown()


async def _names(url: str) -> dict[int, str]:
    sa_engine = create_async_engine(url)
    try:
        db = rf.Engine(sa_engine)
        rows = await db.fetch_all(
            sa.select(write.users_table.c.id, write.users_table.c.name).order_by(
                write.users_table.c.id
            )
        )
    finally:
        await sa_engine.dispose()
    return dict(rows)


async def assert_wrote_the_batch(spec, handle: str, url: str) -> None:
    before = await _names(url)
    await _apply(spec, handle)
    after = await _names(url)

    updated = {i: write.marker(i) for i in range(1, LIMIT + 1)}
    assert {i: after[i] for i in updated} == updated, (
        f"{spec.name} did not leave the batch applied — an arm whose write is "
        f"discarded returns the same payload as one that worked"
    )
    untouched = {i: name for i, name in before.items() if i > LIMIT}
    assert {i: after[i] for i in untouched} == untouched, (
        f"{spec.name} wrote outside its batch"
    )


@pytest.mark.parametrize(
    "spec", registry.select(backend="sqlite", shape="write"), ids=lambda s: s.slug
)
async def test_every_sqlite_write_arm_applies_its_batch(spec):
    db = EphemeralSqlite.create("flat", ROWS)
    try:
        await assert_wrote_the_batch(spec, db.path, f"sqlite+aiosqlite:///{db.path}")
    finally:
        db.close()


@pytest.mark.parametrize(
    "spec", registry.select(backend="postgres", shape="write"), ids=lambda s: s.slug
)
async def test_every_postgres_write_arm_applies_its_batch(spec, pg_dsn):
    await pg_backend.attach(pg_dsn).seed("flat", ROWS)
    url = pg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1).split("?", 1)[0]
    await assert_wrote_the_batch(spec, pg_dsn, url)


async def test_every_arm_sends_the_same_update():
    """`shapes/write.py` claims the ORM arm's bulk-update spelling and the
    Core/rowform one are the same statement, which is what makes this cell a
    comparison rather than three benchmarks.

    Checked at *execution*, not by compiling: `sa.update(UserORM)` on its own
    compiles to an UPDATE of every column, because the ORM builds the real
    statement from the parameter dicts it is given. Comparing the compiled
    strings would therefore have compared a statement no arm ever sends — which
    is how it was written first, and it read as a mismatch when nothing was
    wrong.
    """
    from sqlalchemy import event
    from sqlalchemy.dialects.sqlite import aiosqlite
    from sqlalchemy.ext.asyncio import AsyncSession

    db = EphemeralSqlite.create("flat", ROWS)
    sa_engine = create_async_engine(f"sqlite+aiosqlite:///{db.path}")
    sent: list[str] = []

    @event.listens_for(sa_engine.sync_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, *_args):
        sent.append(statement)

    try:
        async with sa_engine.begin() as conn:
            await conn.execute(write.update_stmt(), write.update_params(2))
        async with AsyncSession(sa_engine) as session, session.begin():
            await session.execute(write.orm_update(), write.orm_params(2))
    finally:
        await sa_engine.dispose()
        db.close()

    updates = {statement for statement in sent if statement.upper().startswith("UPDATE")}
    assert len(updates) == 1, f"the two spellings sent different SQL: {updates}"
    compiled = rf.CoreQuery(write.update_stmt(), aiosqlite.dialect()).sql
    assert compiled == updates.pop(), "rowform compiles a different UPDATE from the other two"
