"""Locust case: sqlite backend, join shape, SQLAlchemy Core, positional rows."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-join-sqlalchemy-core-positional"


class User(CaseUser):
    path = f"/{CASE}"
