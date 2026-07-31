"""rowform — SQLAlchemy's schema and SQL, compiled hydration, no instance state.

SQLAlchemy Core compiles the statement, owns the schema, and owns the pool.
rowform owns the row path: a generated hydrator fills plain dataclasses with
`object.__new__` plus straight attribute stores, so a read never builds a `Row`, a
`CursorResult`, or an ORM identity.

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import Mapped
    import rowform as rf

    class Base(rf.Base):
        pass

    class User(Base):
        __tablename__ = "users"
        id: Mapped[int] = rf.mapped_column(primary_key=True)
        name: Mapped[str]
        email: Mapped[str | None]

    db = rf.Engine(create_async_engine("sqlite+aiosqlite:///app.db"))
    await db.create_all(Base.metadata)

    users = await db.fetch_all(sa.select(User).where(User.name == "ada"))

`User` is one declaration serving three jobs: `User.__table__` feeds
`create_all()`, `Inspector` and Alembic's `target_metadata`; `sa.select(User)`
builds real SQL; and instances are ordinary dataclasses.

Because the engine is SQLAlchemy's, rowform can also read on a connection an
existing application already holds — `rf.Engine.using(session)` — so adoption is
one query at a time. See docs/PLAN_SQLA_API.md for what that costs and why.
"""

from .compile import compile_hydrator, result_processor
from .drivers import Driver, driver_for
from .engine import Engine, Observer
from .errors import (
    ConfigurationError,
    DeclarationError,
    EngineStateError,
    PlanError,
    RowformError,
    StatementError,
    UnsupportedError,
)
from .model import DEFAULT_TYPE_MAP, Base, ModelMeta, alias, mapped_column, model_for
from .planner import Plan, plan
from .query import CoreQuery
from .transaction import Transaction, active_transaction

#: Read by [tool.hatch.version] in pyproject.toml, so this is the one place the
#: version is written.
__version__ = "0.1.0"

__all__ = [
    "DEFAULT_TYPE_MAP",
    "Base",
    "ConfigurationError",
    "CoreQuery",
    "DeclarationError",
    "Driver",
    "Engine",
    "EngineStateError",
    "ModelMeta",
    "Observer",
    "Plan",
    "PlanError",
    "RowformError",
    "StatementError",
    "Transaction",
    "UnsupportedError",
    "__version__",
    "active_transaction",
    "alias",
    "compile_hydrator",
    "driver_for",
    "mapped_column",
    "model_for",
    "plan",
    "result_processor",
]
