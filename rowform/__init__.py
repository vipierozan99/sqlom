"""rowform — SQLAlchemy's schema and SQL, compiled hydration, no instance state.

SQLAlchemy Core compiles the statement and owns the schema. rowform owns the row
path: a generated hydrator fills plain dataclasses with `object.__new__` plus
straight attribute stores, so a read never builds a `Row`, a `CursorResult`, or an
ORM identity.

    import sqlalchemy as sa
    from sqlalchemy.orm import Mapped
    import rowform

    class Base(rf.Base):
        pass

    class User(Base):
        __tablename__ = "users"
        id: Mapped[int] = rf.mapped_column(primary_key=True)
        name: Mapped[str]
        email: Mapped[str | None]

    engine = rf.SqliteEngine("app.db")
    await engine.connect()
    await engine.create_all(Base.metadata)

    users = await engine.fetch_all(sa.select(User).where(User.name == "ada"))

`User` is one declaration serving three jobs: `User.__table__` feeds
`create_all()`, `Inspector` and Alembic's `target_metadata`; `sa.select(User)`
builds real SQL; and instances are ordinary dataclasses.

See docs/PLAN_CORE_COMPILER.md for why, what it costs, and what it retires.
"""

from .compile import compile_hydrator, result_processor
from .engine import Engine
from .model import DEFAULT_TYPE_MAP, Base, ModelMeta, alias, mapped_column, model_for
from .planner import Plan, plan
from .psycopg_engine import PsycopgEngine
from .query import CoreQuery
from .sqlite_engine import SqliteEngine
from .transaction import Transaction, active_transaction

#: Read by [tool.hatch.version] in pyproject.toml, so this is the one place the
#: version is written.
__version__ = "0.1.0"

__all__ = [
    "DEFAULT_TYPE_MAP",
    "Base",
    "CoreQuery",
    "Engine",
    "ModelMeta",
    "Plan",
    "PsycopgEngine",
    "SqliteEngine",
    "Transaction",
    "__version__",
    "active_transaction",
    "alias",
    "compile_hydrator",
    "mapped_column",
    "model_for",
    "plan",
    "result_processor",
]


def __getattr__(name: str):
    """`AsyncpgEngine` is imported lazily: `sqlalchemy.dialects.postgresql.asyncpg`
    imports the driver, so eagerly exporting it would make asyncpg a hard
    dependency of `import rowform`."""
    if name == "AsyncpgEngine":
        from .asyncpg_engine import AsyncpgEngine

        return AsyncpgEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
