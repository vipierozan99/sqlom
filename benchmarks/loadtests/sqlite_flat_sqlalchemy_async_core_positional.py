"""Locust case: sqlite backend, flat shape, SQLAlchemy Core, positional rows."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-flat-sqlalchemy-async-core-positional"


class User(CaseUser):
    path = f"/{CASE}"
