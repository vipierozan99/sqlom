"""Generated shapes, values and projections, with SQLAlchemy Core as the oracle.

`tests/test_sqla_equivalence.py` asserts the same claim — that bypassing `Row`
changes no value — over a fixed schema. That is the shape of test that already
missed this once: an earlier converter table was checked against `int/str/str/bool`
and passed, while getting 7 of 8 columns wrong on a wider row
(docs/METHODOLOGY.md correction 11). A fixed schema can only catch what someone
thought to put in it.

So here the *statement* is generated: which columns, in which order, how many of
them, and what values are in them. Both sides then run the same
`sqlalchemy.Select` — Core through its own `Row` and result processors, rowform
through the generated hydrator — and the two must agree in value and in type.

sqlite only, deliberately. It is the backend where this can go wrong: temporal
types are stored as strings and booleans as integers, so a missing processor shows
up as a `str` where a `datetime` belongs. Postgres decodes most of these in the
driver, which makes it the *easier* case; both backends are covered over fixed
shapes by the equivalence suite.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import enum
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Mapped

import rowform
from rowform import mapped_column


class Shade(enum.Enum):
    DIM = "dim"
    BRIGHT = "bright"


class Base(rowform.Base):
    metadata = sa.MetaData()


class Sample(Base):
    """One column per Python type in `DEFAULT_TYPE_MAP`, each with a nullable twin.

    The nullable twins matter because `None` takes a different path through both
    layers: a processor that would raise on `None` is only exposed by a column
    that can hold one.
    """

    __tablename__ = "prop_sample"

    id: Mapped[int] = mapped_column(primary_key=True)
    an_int: Mapped[int]
    a_str: Mapped[str]
    a_bool: Mapped[bool]
    a_float: Mapped[float]
    a_decimal: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(12, 3))
    a_datetime: Mapped[dt.datetime]
    a_date: Mapped[dt.date]
    a_time: Mapped[dt.time]
    a_uuid: Mapped[uuid.UUID]
    a_json: Mapped[dict]
    a_bytes: Mapped[bytes]
    a_shade: Mapped[Shade] = mapped_column(sa.Enum(Shade, name="prop_shade"))
    maybe_int: Mapped[int | None]
    maybe_str: Mapped[str | None]
    maybe_datetime: Mapped[dt.datetime | None]
    maybe_decimal: Mapped[decimal.Decimal | None] = mapped_column(sa.Numeric(12, 3))
    maybe_uuid: Mapped[uuid.UUID | None]
    maybe_shade: Mapped[Shade | None] = mapped_column(sa.Enum(Shade, name="prop_shade2"))


# sqlite rejects a NUL inside TEXT, and surrogates are not encodable — neither is
# a claim about the row layer.
TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=40,
)

VALUES: dict[str, st.SearchStrategy[Any]] = {
    "an_int": st.integers(min_value=-(2**40), max_value=2**40),
    "a_str": TEXT,
    "a_bool": st.booleans(),
    # NaN and infinity are not about hydration, and NaN would break the equality
    # the whole comparison rests on.
    "a_float": st.floats(allow_nan=False, allow_infinity=False, width=32),
    "a_decimal": st.decimals(
        min_value=decimal.Decimal("-99999.999"),
        max_value=decimal.Decimal("99999.999"),
        places=3,
        allow_nan=False,
        allow_infinity=False,
    ),
    "a_datetime": st.datetimes(
        min_value=dt.datetime(1900, 1, 1), max_value=dt.datetime(2200, 1, 1)
    ),
    "a_date": st.dates(min_value=dt.date(1900, 1, 1), max_value=dt.date(2200, 1, 1)),
    "a_time": st.times(),
    "a_uuid": st.uuids(),
    "a_json": st.dictionaries(TEXT, st.integers(min_value=-1000, max_value=1000), max_size=4),
    "a_bytes": st.binary(max_size=40),
    "a_shade": st.sampled_from(Shade),
}
VALUES |= {
    "maybe_int": st.none() | VALUES["an_int"],
    "maybe_str": st.none() | VALUES["a_str"],
    "maybe_datetime": st.none() | VALUES["a_datetime"],
    "maybe_decimal": st.none() | VALUES["a_decimal"],
    "maybe_uuid": st.none() | VALUES["a_uuid"],
    "maybe_shade": st.none() | VALUES["a_shade"],
}

COLUMN_NAMES = sorted(VALUES)
rows = st.fixed_dictionaries(VALUES)

# Up to six selected columns: past four the static types degrade to `Any`
# by design, but the runtime behaviour must not.
projections = st.lists(st.sampled_from(COLUMN_NAMES), min_size=1, max_size=6, unique=True)


@pytest.fixture(scope="session")
def sample_db(tmp_path_factory):
    """One physical table for the whole module.

    Session-scoped on purpose: a function-scoped fixture under `@given` is reused
    across every example, which Hypothesis flags as a health check because state
    leaks between them. Each example clears the table itself instead.
    """
    path = str(tmp_path_factory.mktemp("prop") / "prop.sqlite3")
    sync = sa.create_engine(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(sync)
    sync.dispose()
    return path


def through_rowform(path: str, values: dict[str, Any], statement: Any) -> Any:
    """Insert with rowform's bind processors, read with its generated hydrator.

    `asyncio.run` per example rather than an async test: Hypothesis drives a
    synchronous callable, and an engine per example is a single sqlite file handle.
    """

    async def go():
        async with rowform.SqliteEngine(path) as db:
            await db.execute(sa.delete(Sample.__table__))
            await db.execute(sa.insert(Sample.__table__).values(id=1, **values))
            return await db.fetch_all(statement)

    return asyncio.run(go())


def through_core(path: str, statement: Any) -> list[Any]:
    """The oracle: the same statement through Core's `Row` and its own processors."""
    sync = sa.create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with sync.connect() as conn:
            return conn.execute(statement).all()
    finally:
        sync.dispose()


def assert_same(got: Any, expected: Any) -> None:
    """Equal value *and* equal type.

    Type is asserted separately because the failure this guards against returns
    something plausible: `'2024-01-01 00:00:00'` compares unequal to a `datetime`,
    but `1` for a `bool` and `'1.5'` for a `Decimal` are the kind of thing that
    slips through an equality-only check.
    """
    assert got == expected, f"{got!r} != {expected!r}"
    assert type(got) is type(expected), f"{type(got)} is not {type(expected)}"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(values=rows, names=projections)
def test_scalar_projections_match_core(sample_db, values, names):
    """Any subset of columns, in any order, at any arity: same values, same types."""
    columns = [getattr(Sample, name) for name in names]
    statement = sa.select(*columns)

    hydrated = through_rowform(sample_db, values, statement)
    (core_row,) = through_core(sample_db, statement)

    assert len(hydrated) == 1
    # One selected column arrives unwrapped; two or more arrive as a tuple, in
    # select order — the rule the whole plan rests on.
    if len(columns) == 1:
        assert_same(hydrated[0], core_row[0])
    else:
        assert len(hydrated[0]) == len(columns)
        for got, expected in zip(hydrated[0], tuple(core_row), strict=True):
            assert_same(got, expected)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(values=rows)
def test_whole_entity_matches_core(sample_db, values):
    """`select(Sample)` hydrates a model whose every field equals the value Core
    produces for the same column."""
    statement = sa.select(Sample)

    (instance,) = through_rowform(sample_db, values, statement)
    (core_row,) = through_core(sample_db, statement)

    assert isinstance(instance, Sample)
    for column, expected in zip(Sample.__table__.columns, tuple(core_row), strict=True):
        assert_same(getattr(instance, column.key), expected)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(values=rows, names=projections)
def test_a_model_beside_scalars_matches_core(sample_db, values, names):
    """The mixed case, where the planner has to decide where the model's run of
    columns ends and the loose ones begin."""
    columns = [getattr(Sample, name) for name in names]
    statement = sa.select(Sample, *columns)

    (row,) = through_rowform(sample_db, values, statement)
    (core_row,) = through_core(sample_db, statement)

    instance, *scalars = row
    assert isinstance(instance, Sample)

    width = len(Sample.__table__.columns)
    for column, expected in zip(Sample.__table__.columns, tuple(core_row)[:width], strict=True):
        assert_same(getattr(instance, column.key), expected)
    for got, expected in zip(scalars, tuple(core_row)[width:], strict=True):
        assert_same(got, expected)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(values=rows, names=projections)
def test_streaming_matches_core_too(sample_db, values, names):
    """`fetch_iter` hydrates chunk by chunk, so it gets the same oracle."""
    columns = [getattr(Sample, name) for name in names]
    statement = sa.select(*columns)

    async def go():
        async with rowform.SqliteEngine(sample_db) as db:
            await db.execute(sa.delete(Sample.__table__))
            await db.execute(sa.insert(Sample.__table__).values(id=1, **values))
            return [row async for row in db.fetch_iter(statement, chunk=1)]

    streamed = asyncio.run(go())
    (core_row,) = through_core(sample_db, statement)

    assert len(streamed) == 1
    if len(columns) == 1:
        assert_same(streamed[0], core_row[0])
    else:
        for got, expected in zip(streamed[0], tuple(core_row), strict=True):
            assert_same(got, expected)
