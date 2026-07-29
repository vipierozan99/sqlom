"""Locust case: postgres backend, join shape, SQLAlchemy async Core join, positional row shaping."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-join-sqlalchemy-async-core-positional"


class User(CaseUser):
    path = f"/{CASE}"
