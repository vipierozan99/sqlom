"""Locust case: postgres backend, flat shape, SQLAlchemy async Core, positional row shaping."""

from benchmarks.load.locust import CaseUser

CASE = "postgres-flat-sqlalchemy-core-positional"


class User(CaseUser):
    path = f"/{CASE}"
