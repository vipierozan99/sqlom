"""Locust case: sqlite backend, flat shape, SQLAlchemy Core via .mappings()."""

from benchmarks.load.locust import CaseUser

CASE = "sqlite-flat-sqlalchemy-async-core-mappings"


class User(CaseUser):
    path = f"/{CASE}"
